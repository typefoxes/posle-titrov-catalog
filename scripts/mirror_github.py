# Created by d.kotina on 25.11.2026
"""Узкий GitHub API для собственного публичного зеркала; встроенный token не покидает api/uploads hosts."""

import base64
import hashlib
import json
import re
from email.utils import parsedate_to_datetime

import requests

from scripts.wikimedia.common import CatalogError, canonical_bytes, require
from scripts.wikimedia.public_delivery import PUBLIC_REPOSITORY, TAG_PATTERN

# MARK: - Fixed Repository API


class MirrorGitHub:
    """Читает Releases/ledger и пишет только месячный ledger, draft assets и готовый release этого repo."""

    def __init__(self, token, session=None):
        """Принимает ephemeral GITHUB_TOKEN текущего public repository; новый PAT или Firebase credential не нужен."""
        require(isinstance(token, str) and bool(token), 'mirror_GITHUB_TOKEN_required')
        self.session = session if session is not None else requests.Session()
        self.headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                        'X-GitHub-Api-Version': '2026-03-10', 'Accept-Encoding': 'identity'}
        self.deletable_asset_id = None

    def close(self):
        """Освобождает собственную session без сохранения token в файлах."""
        self.session.close()

    def request(self, method, path, value=None, upload=None):
        """Разрешает только собственные contents/budget и releases routes; любые redirect с token запрещены."""
        ledger = re.fullmatch(r'/contents/budget/[0-9]{4}-(?:0[1-9]|1[0-2])\.json', path)
        release = re.fullmatch(r'/releases(?:/latest|/tags/wiki-[a-f0-9]{64}|/[1-9][0-9]*(?:/assets)?)?', path)
        allowed = (ledger and method in {'GET', 'PUT'}) or (release and method in {'GET', 'POST', 'PATCH'})
        failed_asset = method == 'DELETE' and self.deletable_asset_id is not None and path == '/releases/assets/' + str(self.deletable_asset_id)
        require(allowed or failed_asset, 'forbidden_mirror_GitHub_request')
        require(upload is None or (method == 'POST' and re.fullmatch(r'/releases/[1-9][0-9]*/assets', path)),
                'forbidden_mirror_upload_route')
        host = 'uploads.github.com' if upload is not None else 'api.github.com'
        address = 'https://' + host + '/repos/' + PUBLIC_REPOSITORY + path
        headers = dict(self.headers)
        body, parameters = (canonical_bytes(value) if value is not None else None), None
        if upload is not None:
            require(upload.name in {'catalog.json', 'catalog.index', 'catalog.pack'}, 'forbidden_mirror_upload_file')
            parameters = {'name': upload.name}
            headers['Content-Type'] = 'application/octet-stream'
            headers['Content-Length'] = str(upload.stat().st_size)
            body = upload.open('rb')
        else:
            headers['Content-Type'] = 'application/json'
        try:
            response = self.session.request(method, address, params=parameters, data=body, headers=headers,
                                           allow_redirects=False, stream=True, timeout=(10, 120), verify=True)
            try:
                require(response.status_code in {200, 201, 404} or (failed_asset and response.status_code == 204), 'mirror_GitHub_http_error')
                payload = response.raw.read(512 * 1024 + 1, decode_content=False)
                require(len(payload) <= 512 * 1024, 'mirror_GitHub_response_limit')
                server_time = parsedate_to_datetime(response.headers['Date'])
                return (None if response.status_code in {204, 404} else json.loads(payload)), server_time
            finally:
                response.close()
        except CatalogError:
            raise
        except Exception:
            raise CatalogError('mirror_GitHub_network_or_json_error') from None
        finally:
            if upload is not None:
                body.close()

    def ledger(self, month):
        """Читает ledger только фиксированного UTC месяца; возвращает blob SHA для conditional update."""
        value, server_time = self.request('GET', '/contents/budget/' + month + '.json')
        if value is None:
            return None, None, server_time
        require(value.get('type') == 'file' and value.get('encoding') == 'base64'
                and value.get('size', 65537) <= 65536, 'invalid_mirror_ledger_file')
        try:
            body = base64.b64decode(value['content'])
            require(len(body) <= 65536, 'mirror_ledger_size')
            ledger = json.loads(body)
        except (ValueError, KeyError, TypeError):
            raise CatalogError('invalid_mirror_ledger_body') from None
        return ledger, value['sha'], server_time

    def save_ledger(self, month, ledger, expected_sha):
        """PUT с прежним blob SHA не перезаписывает параллельный учёт; отсутствие SHA создаёт только новый файл."""
        body = canonical_bytes(ledger)
        require(len(body) <= 65536, 'mirror_ledger_size')
        value = {'message': 'Account bounded Wikimedia mirror transfer', 'branch': 'main',
                 'content': base64.b64encode(body).decode('ascii')}
        if expected_sha is not None:
            value['sha'] = expected_sha
        result, _server_time = self.request('PUT', '/contents/budget/' + month + '.json', value)
        require(result is not None and result.get('content', {}).get('sha'), 'mirror_ledger_commit_not_confirmed')
        return result['content']['sha']

    def latest_release(self):
        """Получает последний публичный пакет;404 не считается готовым первоначальным seed."""
        return self.request('GET', '/releases/latest')[0]

    def delete_failed_draft_asset(self, release_id, asset_id, tag):
        """Удаляет только пустой starter после502 в собственном draft; ни один uploaded/public asset не заменяется."""
        release, _server_time = self.request('GET', '/releases/' + str(release_id))
        require(isinstance(release, dict) and release.get('draft') is True and release.get('tag_name') == tag,
                'failed_asset_requires_matching_draft')
        asset = next((value for value in release.get('assets', []) if value.get('id') == asset_id), None)
        require(isinstance(asset, dict) and asset.get('state') == 'starter' and asset.get('size') == 0
                and asset.get('digest') in {None, ''} and asset.get('name') in {'catalog.pack', 'catalog.index', 'catalog.json'},
                'only_empty_failed_starter_can_be_removed')
        self.deletable_asset_id = asset_id
        try:
            self.request('DELETE', '/releases/assets/' + str(asset_id))
        finally:
            self.deletable_asset_id = None
        after, _server_time = self.request('GET', '/releases/' + str(release_id))
        require(isinstance(after, dict) and all(value.get('id') != asset_id for value in after.get('assets', [])),
                'failed_draft_asset_delete_not_confirmed')

    def publish_package(self, directory, package_sha, expected_latest_tag, source_commit, before_publish):
        """Возобновляет свой draft и открывает его лишь после server SHA всех трёх assets и сравнения latest."""
        tag = 'wiki-' + package_sha
        require(TAG_PATTERN.fullmatch(tag), 'invalid_mirror_package_tag')
        release, _server_time = self.request('GET', '/releases/tags/' + tag)
        if release is None:
            release, _server_time = self.request('POST', '/releases', {'tag_name': tag, 'target_commitish': source_commit,
                'name': 'Wikimedia ' + package_sha[:12], 'draft': True, 'prerelease': False,
                'body': 'Public Wiki metadata only. Sources, revisions, attribution and licenses are retained in documents. No personal data or importer checkpoint.'})
        require(isinstance(release, dict) and release.get('tag_name') == tag, 'mirror_release_identity')
        assets = {asset['name']: asset for asset in release.get('assets', [])}
        for name in ('catalog.pack', 'catalog.index', 'catalog.json'):
            path = directory / name
            with path.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            asset = assets.get(name)
            if asset is not None and asset.get('state') == 'starter':
                self.delete_failed_draft_asset(release['id'], asset['id'], tag)
                asset = None
            if asset is None:
                require(release.get('draft') is True, 'immutable_public_release_asset_missing')
                asset, _server_time = self.request('POST', '/releases/' + str(release['id']) + '/assets', upload=path)
            require(asset is not None and asset.get('name') == name and asset.get('state') == 'uploaded'
                    and asset.get('size') == path.stat().st_size and asset.get('digest') == 'sha256:' + digest,
                    'mirror_asset_server_digest_mismatch')
        before_publish()
        latest = self.latest_release()
        require((latest or {}).get('tag_name') in {expected_latest_tag, tag}, 'mirror_latest_changed_before_publish')
        if release.get('draft'):
            release, _server_time = self.request('PATCH', '/releases/' + str(release['id']), {'draft': False, 'make_latest': 'true'})
        require(release.get('draft') is False and (self.latest_release() or {}).get('tag_name') == tag, 'mirror_release_not_public_latest')
        return {'tag': tag, 'releaseURL': release['html_url'], 'allThreeServerAssetDigestsVerified': True}
