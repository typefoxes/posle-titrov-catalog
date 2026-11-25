# Created by d.kotina on 25.11.2026
"""Публичный пакет адресных envelopes: закрытое состояние не экспортируется, полный download клиенту не нужен."""

import hashlib
import json
import os
import shutil
import struct
import uuid
from collections import OrderedDict
from pathlib import Path

from scripts.wikimedia.assets import validate_asset
from scripts.wikimedia.blocks import MAXIMUM_ENVELOPE_BYTES
from scripts.wikimedia.bundle import validate_index_row
from scripts.wikimedia.common import (HASH_PATTERN, LOCAL_DISK_RESERVE_BYTES, atomic_json,
                                     canonical_bytes, fingerprint, require)
from scripts.wikimedia.contract import validate_record
from scripts.wikimedia.relationship_index import validate_relationship_row
from scripts.wikimedia.schema_validation import validate_schema
from scripts.wikimedia.tree_reader import iter_verified_tree

# MARK: - Public Format

INDEX_ROW = struct.Struct(">32sQI")
DIRECTORY_ROW = struct.Struct(">I32s")
DIRECTORY_GROUPS = 1024
DIRECTORY_BUCKETS = 65536 // DIRECTORY_GROUPS
DIRECTORY_BYTES = DIRECTORY_BUCKETS * DIRECTORY_ROW.size
MAXIMUM_BLOCKS = 500_000
MAXIMUM_ASSET_BYTES = 2_000_000_000
MAXIMUM_INDEX_PAGE_BYTES = 1024 * 1024
PUBLIC_COLLECTIONS = frozenset({"records", "synopses", "related", "references", "search",
                                "external", "pools", "routes", "relationships"})


def validate_public_catalog(catalog):
    """Проверяет прежний release-контракт без переноса checkpoint, receipts, очередей или секретов."""
    require(isinstance(catalog, dict) and isinstance(catalog.get("collections"), dict)
            and set(catalog["collections"]) == PUBLIC_COLLECTIONS,
            "public_pack_requires_nine_collections")
    validate_schema("release", {"schemaVersion": 1, "catalog": catalog,
        "sourceSnapshot": catalog.get("sourceSnapshot"), "checkpoint": catalog.get("sourceSnapshot"),
        "readbackVerified": True})


def public_inventory(catalog, store):
    """Независимо обходит девять публичных DAG и запрещает даже один лишний объект в export store."""
    validate_public_catalog(catalog)
    visited = set()

    def public_block(digest):
        """Открывает только envelope, достижимый из публичного catalog descriptor."""
        require(len(visited) < MAXIMUM_BLOCKS or digest in visited, "public_pack_block_limit")
        visited.add(digest)
        return store.get(digest)

    for name, descriptor in catalog["collections"].items():
        data = name in {"records", "synopses", "related", "references"}
        validator = (validate_record if name == "records" else validate_asset if data
                     else validate_relationship_row if name == "relationships" else validate_index_row)
        for _key, _document in iter_verified_tree(public_block, descriptor, validator, "qid" if data else "key"):
            pass
    require(visited == {row[0] for row in store.connection.execute("SELECT digest FROM blocks")},
            "public_pack_inventory_contains_unreachable_objects")
    require(bool(visited), "public_pack_empty")
    return sorted(visited)


# MARK: - Deterministic Export

def write_public_assets(store, digests, directory):
    """Пишет исходные canonical envelopes и отсортированные 44-байтовые index rows без нового codec."""
    directories, group_counts, pack_offset = [], [], 0
    pack_hash, index_hash = hashlib.sha256(), hashlib.sha256()
    with (directory / "catalog.pack").open("xb") as pack_stream, (directory / "catalog.index").open("xb") as index_stream:
        cursor = iter(digests)
        digest = next(cursor, None)
        directory_rows, group_count = bytearray(), 0
        for prefix in range(65536):
            rows = bytearray()
            while digest is not None and int(digest[:4], 16) == prefix:
                body = canonical_bytes(store.get(digest))
                require(0 < len(body) <= MAXIMUM_ENVELOPE_BYTES, "public_pack_envelope_size")
                rows.extend(INDEX_ROW.pack(bytes.fromhex(digest), pack_offset, len(body)))
                pack_stream.write(body)
                pack_hash.update(body)
                pack_offset += len(body)
                require(pack_offset <= MAXIMUM_ASSET_BYTES, "public_pack_asset_size")
                digest = next(cursor, None)
            require(len(rows) <= MAXIMUM_INDEX_PAGE_BYTES, "public_pack_index_page_size")
            entries = len(rows) // INDEX_ROW.size
            directory_rows.extend(DIRECTORY_ROW.pack(entries, hashlib.sha256(rows).digest()))
            group_count += entries
            index_stream.write(rows)
            index_hash.update(rows)
            if prefix % DIRECTORY_BUCKETS == DIRECTORY_BUCKETS - 1:
                directories.append(bytes(directory_rows))
                group_counts.append(group_count)
                directory_rows, group_count = bytearray(), 0
        require(digest is None, "public_pack_unsorted_inventory")
        for directory_body in directories:
            index_stream.write(directory_body)
            index_hash.update(directory_body)
        for stream in (pack_stream, index_stream):
            stream.flush()
            os.fsync(stream.fileno())
    return {"pack": {"name": "catalog.pack", "bytes": pack_offset, "sha256": pack_hash.hexdigest()},
            "index": {"name": "catalog.index", "bytes": len(digests) * INDEX_ROW.size + DIRECTORY_GROUPS * DIRECTORY_BYTES,
                      "sha256": index_hash.hexdigest(), "rowBytes": INDEX_ROW.size,
                      "directoryRowBytes": DIRECTORY_ROW.size, "groupCounts": group_counts,
                      "directorySHA256": [hashlib.sha256(body).hexdigest() for body in directories]}}


def export_public_pack(catalog, store, destination):
    """Создаёт новый автономный public-only пакет после полного DAG audit, не меняя Firebase или source files."""
    destination = Path(destination)
    require(not destination.exists(), "public_pack_destination_exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    digests = public_inventory(catalog, store)
    expected_bytes = store.inventory()["envelopeBytes"] + len(digests) * INDEX_ROW.size + DIRECTORY_GROUPS * DIRECTORY_BYTES
    require(expected_bytes <= MAXIMUM_ASSET_BYTES
            and shutil.disk_usage(destination.parent).free >= LOCAL_DISK_RESERVE_BYTES + expected_bytes + 1024**2,
            "public_pack_local_disk_reserve")
    temporary = destination.with_name(destination.name + ".partial-" + uuid.uuid4().hex)
    temporary.mkdir()
    assets = write_public_assets(store, digests, temporary)
    manifest = {"schemaVersion": 1, "format": "wikimedia-public-ranges-v3", "catalog": catalog,
                "catalogSHA256": fingerprint(catalog), "blockCount": len(digests), **assets}
    validate_pack_manifest(manifest)
    atomic_json(temporary / "catalog.json", manifest)
    os.rename(temporary, destination)
    return manifest


# MARK: - Bounded Client Contract

def valid_digest(value):
    """Принимает только полный канонический SHA без путей, URL и приведения типов."""
    return isinstance(value, str) and HASH_PATTERN.fullmatch(value) is not None


def validate_pack_manifest(manifest):
    """Проверяет metadata до range-запросов, включая непрерывные offsets и общие размеры всех страниц."""
    require(isinstance(manifest, dict) and set(manifest) == {"schemaVersion", "format", "catalog", "catalogSHA256",
            "blockCount", "pack", "index"} and manifest["schemaVersion"] == 1
            and type(manifest["schemaVersion"]) is int and manifest["format"] == "wikimedia-public-ranges-v3",
            "invalid_public_pack_manifest")
    validate_public_catalog(manifest["catalog"])
    require(manifest["catalogSHA256"] == fingerprint(manifest["catalog"])
            and type(manifest["blockCount"]) is int and 0 < manifest["blockCount"] <= MAXIMUM_BLOCKS,
            "public_pack_catalog_identity")
    pack, index = manifest["pack"], manifest["index"]
    require(isinstance(pack, dict) and set(pack) == {"name", "bytes", "sha256"}
            and pack["name"] == "catalog.pack" and type(pack["bytes"]) is int
            and 0 < pack["bytes"] <= MAXIMUM_ASSET_BYTES and valid_digest(pack["sha256"]), "invalid_public_pack_asset")
    require(isinstance(index, dict) and set(index) == {"name", "bytes", "sha256", "rowBytes", "directoryRowBytes",
            "groupCounts", "directorySHA256"}
            and index["name"] == "catalog.index" and type(index["bytes"]) is int
            and index["bytes"] == manifest["blockCount"] * INDEX_ROW.size + DIRECTORY_GROUPS * DIRECTORY_BYTES
            and type(index["rowBytes"]) is int and index["rowBytes"] == INDEX_ROW.size
            and type(index["directoryRowBytes"]) is int and index["directoryRowBytes"] == DIRECTORY_ROW.size
            and valid_digest(index["sha256"]) and isinstance(index["groupCounts"], list)
            and len(index["groupCounts"]) == DIRECTORY_GROUPS
            and all(type(count) is int and 0 <= count <= MAXIMUM_BLOCKS for count in index["groupCounts"])
            and sum(index["groupCounts"]) == manifest["blockCount"]
            and isinstance(index["directorySHA256"], list) and len(index["directorySHA256"]) == DIRECTORY_GROUPS
            and all(valid_digest(digest) for digest in index["directorySHA256"]), "invalid_public_pack_index")


def verified_range_body(status, headers, body, offset, length, total):
    """Отклоняет200, другой диапазон, сжатие или усечённый ответ, не принимая full asset вместо одного блока."""
    normalized = {name.lower(): value for name, value in headers.items()}
    require(status == 206 and normalized.get("content-range") == f"bytes {offset}-{offset + length - 1}/{total}"
            and normalized.get("content-encoding", "identity") == "identity" and len(body) == length
            and normalized.get("content-length", str(length)) == str(length), "invalid_public_pack_range_response")
    return body


class PublicPackReader:
    """Получает только нужную SHA-prefix страницу и envelope; не запрашивает полный pack/index или private state."""

    def __init__(self, manifest, read_range):
        """Закрепляет catalog и transport callback(name, offset, length, total) с обязательной206-проверкой."""
        validate_pack_manifest(manifest)
        self.manifest = manifest
        self.read_range = read_range
        self.directories = OrderedDict()
        self.pages = OrderedDict()

    def directory_page(self, group):
        """Читает2304байта hash-directory для64маленьких страниц; LRU держит максимум16директорий."""
        if group in self.directories:
            self.directories.move_to_end(group)
            return self.directories[group]
        index = self.manifest["index"]
        offset = self.manifest["blockCount"] * INDEX_ROW.size + group * DIRECTORY_BYTES
        body = self.read_range("catalog.index", offset, DIRECTORY_BYTES, index["bytes"])
        require(len(body) == DIRECTORY_BYTES and hashlib.sha256(body).hexdigest() == index["directorySHA256"][group],
                "public_pack_directory_checksum")
        rows = list(DIRECTORY_ROW.iter_unpack(body))
        require(sum(row[0] for row in rows) == index["groupCounts"][group]
                and all(count * INDEX_ROW.size <= MAXIMUM_INDEX_PAGE_BYTES for count, _digest in rows),
                "public_pack_directory_counts")
        self.directories[group] = rows
        if len(self.directories) > 16:
            self.directories.popitem(last=False)
        return rows

    def index_page(self, prefix):
        """Читает страницу по первым двум байтам SHA; в полном каталоге обычно это несколько44-байтовых rows."""
        if prefix in self.pages:
            self.pages.move_to_end(prefix)
            return self.pages[prefix]
        group, bucket = divmod(int(prefix, 16), DIRECTORY_BUCKETS)
        directory = self.directory_page(group)
        count, expected_hash = directory[bucket]
        index = self.manifest["index"]
        preceding = sum(index["groupCounts"][:group]) + sum(row[0] for row in directory[:bucket])
        body = self.read_range("catalog.index", preceding * INDEX_ROW.size, count * INDEX_ROW.size, index["bytes"]) if count else b""
        require(len(body) == count * INDEX_ROW.size and hashlib.sha256(body).digest() == expected_hash,
                "public_pack_index_checksum")
        previous, offset = None, None
        for digest, start, length in INDEX_ROW.iter_unpack(body):
            require(digest.hex().startswith(prefix) and (previous is None or previous < digest)
                    and (offset is None or start == offset) and 0 < length <= MAXIMUM_ENVELOPE_BYTES
                    and start + length <= self.manifest["pack"]["bytes"], "public_pack_index_row")
            previous, offset = digest, start + length
        self.pages[prefix] = body
        if len(self.pages) > 128:
            self.pages.popitem(last=False)
        return body

    def locate(self, digest):
        """Ищет полный SHA в одной проверенной странице, не скачивая тело при проверке membership."""
        require(valid_digest(digest), "invalid_public_pack_digest")
        page = self.index_page(digest[:4])
        wanted, lower, upper = bytes.fromhex(digest), 0, len(page) // INDEX_ROW.size
        while lower < upper:
            middle = (lower + upper) // 2
            found, offset, length = INDEX_ROW.unpack_from(page, middle * INDEX_ROW.size)
            if found < wanted:
                lower = middle + 1
            elif found > wanted:
                upper = middle
            else:
                return offset, length
        return None

    def __contains__(self, digest):
        """Совместимо с прежним tree reader проверяет адрес без загрузки envelope."""
        return self.locate(digest) is not None

    def __getitem__(self, digest):
        """Передаёт прежним bounded readers один SHA-verified envelope по полному адресу."""
        return self.get(digest)

    def get(self, digest):
        """Сверяет исходный envelope перед прежним bounded zlib decoder, сохраняя все прежние SHA."""
        location = self.locate(digest)
        require(location is not None, "public_pack_block_missing")
        offset, length = location
        body = self.read_range("catalog.pack", offset, length, self.manifest["pack"]["bytes"])
        require(len(body) == length and hashlib.sha256(body).hexdigest() == digest, "public_pack_block_checksum")
        try:
            envelope = json.loads(body)
        except (ValueError, UnicodeError):
            require(False, "public_pack_block_json")
        require(canonical_bytes(envelope) == body, "public_pack_noncanonical_block")
        return envelope
