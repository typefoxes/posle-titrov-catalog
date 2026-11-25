# Created by d.kotina on 25.11.2026
"""Анонимное адресное чтение публичных Releases: закреплённый tag, bounded206 и обычная проверкаTLS."""

import json
import re
import time
from urllib.parse import urljoin, urlparse

import requests

from scripts.wikimedia.common import CatalogError, USER_AGENT, fingerprint, require
from scripts.wikimedia.public_pack import (MAXIMUM_ASSET_BYTES, MAXIMUM_INDEX_PAGE_BYTES,
                                          validate_pack_manifest, verified_range_body)

# MARK: - Fixed Public Destination

PUBLIC_REPOSITORY = "typefoxes/posle-titrov-catalog"
PUBLIC_RELEASE_BASE = "https://github.com/" + PUBLIC_REPOSITORY + "/releases/"
TAG_PATTERN = re.compile(r"wiki-[a-f0-9]{64}\Z")


class AnonymousPublicAuth(requests.auth.AuthBase):
    """Запрещает неявную передачу netrc Authorization или session cookies в публичный каталог."""

    def __call__(self, request):
        """Удаляет чувствительные заголовки уже после подготовки запроса requests."""
        request.headers.pop("Authorization", None)
        request.headers.pop("Cookie", None)
        return request


def public_destination(url, name):
    """Принимает только собственный release path или штатный HTTPS asset CDN без сменыTLS/сети."""
    parsed = urlparse(url)
    require(parsed.scheme == "https" and parsed.port in {None, 443} and parsed.username is None
            and parsed.password is None and not parsed.fragment, "invalid_public_delivery_destination")
    if parsed.hostname == "release-assets.githubusercontent.com":
        return None
    require(parsed.hostname == "github.com" and not parsed.query, "public_delivery_redirect_host")
    if url == PUBLIC_RELEASE_BASE + "latest/download/" + name:
        return None
    prefix = "/" + PUBLIC_REPOSITORY + "/releases/download/"
    require(parsed.path.startswith(prefix), "public_delivery_redirect_repository")
    remainder = parsed.path[len(prefix):].split("/")
    require(len(remainder) == 2 and TAG_PATTERN.fullmatch(remainder[0]) is not None
            and remainder[1] == name, "public_delivery_redirect_asset")
    return remainder[0]


# MARK: - Anonymous Transport

class ReleaseRangeTransport:
    """Не имеет Firebase fallback, токенов или записи; принимает только три имени публичного пакета."""

    def __init__(self, session=None):
        """Создаёт отдельную session и счётчики, оставляя DNS и проверку сертификата штатными."""
        self.session = session if session is not None else requests.Session()
        self.auth = AnonymousPublicAuth()
        self.tag = None
        self.asset_addresses = {}
        self.requests = 0
        self.received_bytes = 0

    def close(self):
        """Закрывает только собственную session без изменения настроек пользователя."""
        self.session.close()

    def _read(self, name, offset=None, length=None, total=None):
        """Следует максимум четырём разрешённым redirect;200на range отклоняет до чтения большого тела."""
        require(name in {"catalog.json", "catalog.index", "catalog.pack"}, "invalid_public_delivery_asset")
        ranged = offset is not None
        require(ranged or name == "catalog.json", "public_delivery_whole_asset_forbidden")
        if ranged:
            require(self.tag is not None and all(type(value) is int for value in (offset, length, total))
                    and 0 <= offset < total <= MAXIMUM_ASSET_BYTES and 0 < length <= MAXIMUM_INDEX_PAGE_BYTES
                    and offset + length <= total, "invalid_public_delivery_range")
        original_address = PUBLIC_RELEASE_BASE + ("download/" + self.tag if self.tag else "latest/download") + "/" + name
        cached = self.asset_addresses.get(name)
        refresh_cached = bool(ranged and cached and cached[1] > time.monotonic())
        address = cached[0] if refresh_cached else original_address
        headers = {"Accept-Encoding": "identity", "User-Agent": USER_AGENT}
        if ranged:
            headers["Range"] = f"bytes={offset}-{offset + length - 1}"
        observed_tag = self.tag
        try:
            for _redirect in range(5):
                destination_tag = public_destination(address, name)
                if destination_tag:
                    require(observed_tag in {None, destination_tag}, "public_delivery_release_changed")
                    observed_tag = destination_tag
                self.requests += 1
                response = self.session.get(address, headers=headers, auth=self.auth, allow_redirects=False,
                                            stream=True, timeout=(10, 30), verify=True)
                try:
                    if response.status_code in {401, 403} and refresh_cached:
                        self.asset_addresses.pop(name, None)
                        address, refresh_cached = original_address, False
                        continue
                    if response.status_code in {301, 302, 303, 307, 308}:
                        address = urljoin(address, response.headers.get("Location", ""))
                        continue
                    require(response.status_code == (206 if ranged else 200), "public_delivery_http_status")
                    maximum = length if ranged else 128 * 1024
                    body = response.raw.read(maximum + 1, decode_content=False)
                    self.received_bytes += len(body)
                    require(len(body) <= maximum and observed_tag is not None, "public_delivery_body_limit_or_unpinned_tag")
                    if ranged:
                        body = verified_range_body(response.status_code, response.headers, body, offset, length, total)
                        if urlparse(address).hostname == "release-assets.githubusercontent.com":
                            self.asset_addresses[name] = (address, time.monotonic() + 60)
                    return body, observed_tag
                finally:
                    response.close()
            require(False, "public_delivery_redirect_limit")
        except CatalogError:
            raise
        except Exception:
            raise CatalogError("public_delivery_network_error") from None

    def read_manifest(self):
        """Закрепляет tag по SHA всего descriptor: новый формат пакета не требует подделывать source revision."""
        body, tag = self._read("catalog.json")
        try:
            manifest = json.loads(body)
        except (ValueError, UnicodeError):
            raise CatalogError("public_delivery_manifest_json") from None
        validate_pack_manifest(manifest)
        require(tag == "wiki-" + fingerprint(manifest), "public_delivery_manifest_tag_mismatch")
        self.tag = tag
        return manifest

    def read_range(self, name, offset, length, total):
        """Предоставляет PublicPackReader только точный проверенный byte range одной закреплённой версии."""
        body, tag = self._read(name, offset, length, total)
        require(tag == self.tag, "public_delivery_pinned_release_changed")
        return body
