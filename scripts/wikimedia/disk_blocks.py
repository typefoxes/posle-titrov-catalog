# Created by d.kotina on 24.11.2026
"""Потоковая сборка полного immutable каталога с ограниченной памятью и дисковым дедуп store."""

import json
import os
import re
import sqlite3
import shutil
from pathlib import Path

from scripts.wikimedia.blocks import (MAXIMUM_RECORDS, decode_document_block,
                                      encode_document_block)
from scripts.wikimedia.bundle import validate_directory_row
from scripts.wikimedia.common import (CatalogError, atomic_json, canonical_bytes,
                                     fingerprint, read_json, require, LOCAL_DISK_RESERVE_BYTES)

# MARK: - Content-addressed Store

class DiskBlockStore:
    """Хранит один envelope на SHA, отдельно учитывая фактические серверные ключи и размеры."""

    def __init__(self, directory):
        """Открывает только собственный каталог сборки и его индекс, не пользовательские базы."""
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.directory / "inventory.sqlite")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS blocks (digest TEXT PRIMARY KEY,bytes INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS routes (qid TEXT NOT NULL,collection TEXT NOT NULL,digest TEXT NOT NULL,
                PRIMARY KEY(qid,collection));
            CREATE TABLE IF NOT EXISTS trees (name TEXT PRIMARY KEY,payload BLOB NOT NULL);
        """)

    def close(self):
        """Закрывает собственный индекс без удаления построенных блоков."""
        self.connection.close()

    def path(self, digest):
        """Разрешает только полный lowercase SHA, исключая произвольный путь записи."""
        require(isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest), "invalid_disk_block_digest")
        return self.directory / "blocks" / digest[:2] / (digest + ".json")

    def put(self, digest, envelope):
        """Сохраняет проверенный объект атомарно, не перезаписывая другой контент под тем же адресом."""
        require(fingerprint(envelope) == digest, "disk_block_hash_mismatch")
        require(shutil.disk_usage(self.directory).free >= LOCAL_DISK_RESERVE_BYTES + 16 * 1024**2,
                "local_disk_reserve_reached")
        target = self.path(digest)
        if target.exists():
            require(fingerprint(read_json(target)) == digest, "disk_block_collision_or_corruption")
        else:
            atomic_json(target, envelope)
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO blocks VALUES (?,?)", (digest, len(canonical_bytes(envelope))))

    def get(self, digest):
        """Читает один envelope и сверяет полный SHA перед передачей bounded decoder."""
        value = read_json(self.path(digest))
        require(fingerprint(value) == digest, "disk_block_corrupted")
        return value

    def inventory(self):
        """Возвращает фактический размер уникальных JSON envelopes и их Firebase-ключей."""
        count, bodies = self.connection.execute("SELECT COUNT(*),COALESCE(SUM(bytes),0) FROM blocks").fetchone()
        return {"uniqueBlocks": count, "envelopeBytes": bodies,
                "firebaseBlocksSubtreeBytes": bodies + count * 68 + 1 if count else 2}


# MARK: - Streaming Leaf Packing

def store_partition(rows, validator, identity_field, store, collection=None):
    """Делит переполненную порцию и сохраняет все её строки без обрезания полей."""
    pending, ranges = [rows], []
    while pending:
        selected = pending.pop(0)
        try:
            digest, envelope = encode_document_block(selected, validator, identity_field)
        except CatalogError as error:
            if str(error) not in {"decoded_block_too_large", "encoded_block_too_large"} or len(selected) <= 1:
                raise
            keys = list(selected)
            middle = len(keys) // 2
            pending[0:0] = [{key: selected[key] for key in keys[:middle]},
                            {key: selected[key] for key in keys[middle:]}]
            continue
        require(decode_document_block(digest, envelope, validator, identity_field) == selected, "disk_block_roundtrip_failed")
        store.put(digest, envelope)
        keys = list(selected)
        if identity_field == "qid" and collection in {"records", "synopses", "related", "references"}:
            with store.connection:
                store.connection.executemany("INSERT OR REPLACE INTO routes VALUES (?,?,?)",
                                             [(key, collection, digest) for key in keys])
        ranges.append({"key": keys[-1], "first": keys[0], "block": digest})
    return ranges


def build_streamed_tree(iterator, validator, identity_field, store, collection=None):
    """Строит тот же anchored формат из отсортированного cursor, не загружая весь каталог в память."""
    current, ranges, previous, count = {}, [], None, 0
    for key, document in iterator:
        require(isinstance(key, str) and (previous is None or previous < key)
                and document.get(identity_field) == key, "unsorted_or_duplicate_streamed_key")
        previous = key
        current[key] = document
        count += 1
        if len(current) == MAXIMUM_RECORDS or int(fingerprint(key)[-2:], 16) % 32 == 0:
            ranges.extend(store_partition(current, validator, identity_field, store, collection))
            current = {}
    if current:
        ranges.extend(store_partition(current, validator, identity_field, store, collection))
    if not count:
        return {"count": 0, "depth": 0}
    depth = 0
    while len(ranges) > 1:
        depth += 1
        require(depth <= 8, "disk_tree_too_deep")
        parents = []
        for offset in range(0, len(ranges), MAXIMUM_RECORDS):
            group = ranges[offset:offset + MAXIMUM_RECORDS]
            documents = {row["key"]: row for row in group}
            generated = store_partition(documents, validate_directory_row, "key", store)
            for row in generated:
                row["first"] = documents[row["first"]]["first"]
            parents.extend(generated)
        ranges = parents
    descriptor = {"count": count, "depth": depth, "root": ranges[0]["block"],
                  "first": ranges[0]["first"], "last": ranges[0]["key"]}
    if collection is not None:
        with store.connection:
            store.connection.execute("INSERT OR REPLACE INTO trees VALUES (?,?)", (collection, canonical_bytes(descriptor)))
    return descriptor


# MARK: - Input Snapshot

def immutable_sqlite_snapshot(source_path, destination_path):
    """Создаёт новый самодостаточный snapshot через backup API, не копируя живой sqlite без WAL."""
    source_path, destination_path = Path(source_path).resolve(), Path(destination_path)
    require(source_path.is_file() and not destination_path.exists(), "invalid_full_snapshot_destination")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination_path.with_name(destination_path.name + ".partial")
    require(not temporary.exists(), "snapshot_partial_already_exists")
    source = sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)
    destination = sqlite3.connect(temporary)
    try:
        expected = source.execute("PRAGMA page_count").fetchone()[0] * source.execute("PRAGMA page_size").fetchone()[0]
        require(shutil.disk_usage(destination_path.parent).free >= LOCAL_DISK_RESERVE_BYTES + expected + 16 * 1024**2,
                "snapshot_disk_reserve_reached")
        source.backup(destination)
        require(destination.execute("PRAGMA quick_check").fetchone() == ("ok",), "full_snapshot_integrity_failed")
        destination.execute("PRAGMA journal_mode=DELETE")
    finally:
        source.close()
        destination.close()
    with temporary.open("rb") as stream:
        os.fsync(stream.fileno())
    os.link(temporary, destination_path)
    # Удаляется только собственное временное имя уже проверенного snapshot, не данные источника.
    temporary.unlink()
    descriptor = os.open(destination_path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def json_cursor(connection, query):
    """Отдаёт по одному каноническому документу из snapshot без materialization полного набора."""
    for key, payload in connection.execute(query):
        yield key, json.loads(payload)
