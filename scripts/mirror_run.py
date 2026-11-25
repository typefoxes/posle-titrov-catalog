# Created by d.kotina on 25.11.2026
"""Публичное зеркало: прежний Releases seed + bounded новые Wiki blocks → проверенный новый immutable пакет."""

import hashlib
import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

import requests

from scripts.mirror_budget import MAXIMUM_SOURCE_BYTES, MAXIMUM_SOURCE_REQUESTS, MirrorReservation
from scripts.mirror_github import MirrorGitHub
from scripts.wikimedia.common import (DATABASE_ADDRESS, PUBLIC_ROOT, CatalogError, atomic_json,
                                     canonical_bytes, fingerprint, read_json, require, LOCAL_DISK_RESERVE_BYTES)
from scripts.wikimedia.disk_blocks import DiskBlockStore
from scripts.wikimedia.public_delivery import AnonymousPublicAuth, ReleaseRangeTransport
from scripts.wikimedia.public_pack import PublicPackReader, export_public_pack, valid_digest
from scripts.wikimedia.schema_validation import validate_schema

# MARK: - Anonymous Bounded Firebase Reads


class PublicFirebaseSource:
    """Не имеет авторизации, записи, shallow/root/closed reads или произвольного URL."""

    def __init__(self, session=None):
        """Начинает счётчики после durable месячной reservation; ограничивает source фазу20минутами."""
        self.session = session if session is not None else requests.Session()
        self.auth = AnonymousPublicAuth()
        self.received = 0
        self.requests = 0
        self.started = time.monotonic()

    def close(self):
        """Закрывает только анонимную session текущего run."""
        self.session.close()

    def read(self, collection=None, digest=None):
        """До сети резервирует worst response; неопределённый частичный ответ также остаётся учтённым."""
        require(collection in {None, 'releases', 'blocks'} and (collection is None or valid_digest(digest)),
                'forbidden_public_Firebase_path')
        maximum = 257 * 1024 if collection == 'blocks' else 128 * 1024
        require(self.requests < MAXIMUM_SOURCE_REQUESTS and self.received + maximum + 1 <= MAXIMUM_SOURCE_BYTES
                and time.monotonic() - self.started < 1200, 'mirror_source_budget_exhausted')
        path = PUBLIC_ROOT + ('/manifest' if collection is None else '/' + collection + '/' + digest)
        self.requests += 1
        accounted = False
        try:
            response = self.session.get(DATABASE_ADDRESS + path + '.json', auth=self.auth,
                headers={'Accept-Encoding': 'identity'}, stream=True, allow_redirects=False, timeout=(10, 30), verify=True)
            try:
                body = response.raw.read(maximum + 1, decode_content=False)
                self.received += len(body)
                accounted = True
                require(response.status_code == 200 and len(body) <= maximum, 'public_Firebase_http_or_size_error')
                value = json.loads(body)
                require(isinstance(value, dict) and (collection is None or fingerprint(value) == digest),
                        'public_Firebase_object_hash_mismatch')
                return value
            finally:
                response.close()
        except Exception as error:
            if not accounted:
                self.received += maximum + 1
            if isinstance(error, CatalogError):
                raise
            raise CatalogError('public_Firebase_network_or_json_error') from None


# MARK: - Disposable Public Seed


def verified_asset_file(path, descriptor):
    """Доверяет cache-файлу лишь после полного byte count/SHA; имя файла само по себе не является доказательством."""
    if not path.is_file() or path.stat().st_size != descriptor['bytes']:
        return False
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest() == descriptor['sha256']


def restore_public_seed(transport, manifest, cache):
    """При потере cache скачивает прежний публичный pack изGitHub bounded ranges, а не полный RTDB каталог."""
    cache.mkdir(parents=True, exist_ok=True)
    for name in ('pack', 'index'):
        descriptor = manifest[name]
        target = cache / descriptor['name']
        if verified_asset_file(target, descriptor):
            continue
        temporary = target.with_name(target.name + '.partial')
        with temporary.open('wb') as stream:
            for offset in range(0, descriptor['bytes'], 1024**2):
                length = min(1024**2, descriptor['bytes'] - offset)
                stream.write(transport.read_range(descriptor['name'], offset, length, descriptor['bytes']))
        require(verified_asset_file(temporary, descriptor), 'mirror_public_seed_checksum')
        os.replace(temporary, target)
    atomic_json(cache / 'catalog.json', manifest)

    def read_range(name, offset, length, total):
        """Читает один диапазон проверенного immutable local seed без сети и полного materialization вRAM."""
        path = cache / name
        require(path.stat().st_size == total, 'mirror_local_seed_size')
        with path.open('rb') as stream:
            stream.seek(offset)
            return stream.read(length)

    return PublicPackReader(manifest, read_range)


class PublicPendingCache:
    """Хранит только проверяемые public envelopes; дополнительная SQLite не нужна для возобновления GET."""

    def __init__(self, directory):
        """Открывает отдельный disposable каталог без токенов, private checkpoint и индекса доверия."""
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def path(self, digest):
        """Ограничивает любой доступ полным SHA и собственным публичным cache-каталогом."""
        require(valid_digest(digest), 'mirror_pending_invalid_digest')
        return self.directory / 'blocks' / digest[:2] / (digest + '.json')

    def get(self, digest):
        """Проверяет размер и SHA каждого hit; повреждённый файл или symlink переносит в карантин до GET."""
        path = self.path(digest)
        if not path.exists() and not path.is_symlink():
            return None
        try:
            require(not path.is_symlink() and path.stat().st_size <= 257 * 1024 + 1, 'mirror_pending_invalid_file')
            value = read_json(path)
            require(isinstance(value, dict) and fingerprint(value) == digest, 'mirror_pending_hash_mismatch')
            return value
        except (CatalogError, FileNotFoundError, ValueError):
            quarantine = path.with_name(path.name + '.corrupt-' + uuid.uuid4().hex)
            os.replace(path, quarantine)
            return None

    def put(self, digest, value):
        """Записывает атомарно только полный проверенный public ответ в пределах прежнего дискового резерва."""
        require(isinstance(value, dict) and fingerprint(value) == digest
                and len(canonical_bytes(value)) <= 257 * 1024, 'mirror_pending_invalid_value')
        require(shutil.disk_usage(self.directory).free >= LOCAL_DISK_RESERVE_BYTES + 16 * 1024**2,
                'local_disk_reserve_reached')
        existing = self.get(digest)
        if existing is not None:
            require(existing == value, 'mirror_pending_hash_collision')
            return
        atomic_json(self.path(digest), value)

    def clear(self):
        """Удаляет только генерируемый delta cache после подтверждённого пакета; ошибка очистки не отменяет публикацию."""
        try:
            shutil.rmtree(self.directory)
            return True
        except OSError:
            return False


class MirrorBlockStore(DiskBlockStore):
    """Новый inventory включает только достигнутые public DAG objects, без orphan blocks прежнего seed."""

    def __init__(self, directory, seed, source, pending_directory=None):
        """Получает заменяемые read-only зависимости; закрытый state не доступен ни одному пути."""
        super().__init__(directory)
        self.seed = seed
        self.source = source
        self.new_source_blocks = 0
        self.reused_pending_blocks = 0
        self.pending_directory = Path(pending_directory) if pending_directory is not None else Path(directory) / 'pending'
        self.pending = PublicPendingCache(self.pending_directory)

    def clear_completed_pending(self):
        """Удаляет только генерируемый delta cache после readback опубликованного пакета и установки его verified seed."""
        return self.pending.clear()

    def get(self, digest):
        """Берёт старый SHA из verified pack; только новый отсутствующий адрес читает из публичного Firebase."""
        present = self.connection.execute('SELECT 1 FROM blocks WHERE digest=?', (digest,)).fetchone()
        if present:
            return super().get(digest)
        if digest in self.seed:
            value = self.seed.get(digest)
        else:
            value = self.pending.get(digest)
            if value is None:
                value = self.source.read('blocks', digest)
                self.pending.put(digest, value)
                self.new_source_blocks += 1
            else:
                self.reused_pending_blocks += 1
        self.put(digest, value)
        return value


def install_public_cache(package, manifest, cache):
    """Сохраняет уже собранные public assets без повторного сетевого download; cache по-прежнему можно потерять."""
    cache.mkdir(parents=True, exist_ok=True)
    for name in ('pack', 'index'):
        descriptor = manifest[name]
        target = cache / descriptor['name']
        temporary = target.with_name(target.name + '.partial')
        shutil.copyfile(package / descriptor['name'], temporary)
        require(verified_asset_file(temporary, descriptor), 'mirror_new_cache_checksum')
        os.replace(temporary, target)
    atomic_json(cache / 'catalog.json', manifest)


# MARK: - Complete Mirror Run


def mirror_once(github, directory, cache, force_rebuild=False):
    """Не публикует partial/source-mixed пакет; source и latest должны совпасть с закреплённой базой перед публикацией."""
    reservation = MirrorReservation(github)
    reservation.reserve()
    source, transport = PublicFirebaseSource(), ReleaseRangeTransport()
    try:
        latest = github.latest_release()
        require(isinstance(latest, dict), 'initial_public_Releases_seed_required')
        previous_manifest = transport.read_manifest()
        require(transport.tag == latest['tag_name'], 'mirror_latest_changed_before_seed')
        pointer = source.read()
        validate_schema('pointer', pointer)
        release = source.read('releases', pointer['release'])
        validate_schema('release', release)
        require(release['sourceSnapshot'] == pointer['sourceSnapshot'] and release['readbackVerified'] is True
                and release['catalog']['sourceSnapshot'] == pointer['sourceSnapshot'],
                'mirror_source_release_pair_mismatch')
        catalog = release['catalog']
        if fingerprint(catalog) == previous_manifest['catalogSHA256'] and not force_rebuild:
            reader = PublicPackReader(previous_manifest, transport.read_range)
            reader.get(catalog['collections']['records']['root'])
            return {'complete': True, 'changed': False, 'catalogSHA256': fingerprint(catalog), 'sourceRequests': source.requests}
        seed = restore_public_seed(transport, previous_manifest, cache)
        objects = MirrorBlockStore(directory / 'objects', seed, source, cache / 'pending')
        try:
            manifest = export_public_pack(catalog, objects, directory / 'package')
            # Это полный новый public DAG, а не частичный source checkpoint или новая дата старого каталога.
            def confirm_source():
                """После upload/readback всех assets ещё раз сверяет закреплённый источник перед открытием draft."""
                after = source.read()
                require(after['release'] == pointer['release'] and after['sourceSnapshot'] == pointer['sourceSnapshot'],
                        'mirror_source_release_changed_before_publish')

            result = github.publish_package(directory / 'package', fingerprint(manifest), latest['tag_name'],
                                            os.environ['GITHUB_SHA'], confirm_source)
            probe = ReleaseRangeTransport()
            try:
                require(probe.read_manifest() == manifest, 'mirror_anonymous_published_manifest_mismatch')
                reader = PublicPackReader(manifest, probe.read_range)
                digest = catalog['collections']['records']['root']
                require(reader.get(digest) == objects.get(digest), 'mirror_anonymous_range_readback_mismatch')
            finally:
                probe.close()
            # Кэш может пропасть: он ускоритель, authoritative seed остаётся в проверенном публичном Release.
            install_public_cache(directory / 'package', manifest, cache)
            cleared_pending = objects.clear_completed_pending()
            return {'complete': True, 'changed': fingerprint(manifest) != fingerprint(previous_manifest),
                'completePackageRebuiltAndServerDigestsVerified': True, **result, 'catalogSHA256': fingerprint(catalog),
                'newFirebaseBlocks': objects.new_source_blocks, 'sourceRequests': source.requests,
                'reusedPendingPublicBlocks': objects.reused_pending_blocks, 'pendingCacheCleared': cleared_pending,
                'sourceReceivedPayloadBytes': source.received, 'clientFallbackToFirebaseEnabled': False}
        finally:
            objects.close()
    finally:
        try:
            settlement = reservation.settle(source.received, source.requests)
            atomic_json(directory / 'transfer-settlement.json', settlement)
        finally:
            source.close()
            transport.close()


def main():
    """Запускается только в собственном public Actions repository со встроенным token; ошибки не печатают responses/credentials."""
    require(os.environ.get('GITHUB_REPOSITORY') == 'typefoxes/posle-titrov-catalog', 'mirror_requires_own_public_Actions_repository')
    directory = Path('artifacts/mirror/run-' + os.environ['GITHUB_RUN_ID'] + '-' + os.environ.get('GITHUB_RUN_ATTEMPT', '1'))
    directory.mkdir(parents=True, exist_ok=False)
    github = MirrorGitHub(os.environ.get('GITHUB_TOKEN'))
    try:
        result = mirror_once(github, directory, Path('artifacts/mirror/cache'),
                             force_rebuild=os.environ.get('MIRROR_REBUILD', 'false') == 'true')
        atomic_json(directory / 'result.json', result)
        print(json.dumps(result))
    except CatalogError as error:
        result = {'complete': False, 'previousVerifiedPackagesRetained': True, 'activePackageNeedsReadback': True, 'error': str(error)}
        atomic_json(directory / 'result.json', result)
        print(json.dumps(result))
        return 1
    finally:
        github.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
