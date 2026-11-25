# Created by d.kotina on 24.11.2026
"""Keyset discovery по прямым классам с транзакционным checkpoint и повтором страницы."""

import fcntl
import json
import sqlite3
from pathlib import Path

from scripts.wikimedia.common import ROOT_CLASSES, canonical_bytes, fingerprint, require, valid_qid

# MARK: - SPARQL

def binding_qids(payload, variable="item"):
    """Проверяет всю выдачу SPARQL до продвижения позиции; ID остаются строками."""
    require(variable in payload.get("head", {}).get("vars", [])
            and isinstance(payload.get("results", {}).get("bindings"), list), "invalid_sparql_page")
    identifiers = []
    for binding in payload["results"]["bindings"]:
        entry = binding.get(variable, {})
        value = entry.get("value", "")
        require(entry.get("type") == "uri" and value.startswith("http://www.wikidata.org/entity/"),
                "invalid_sparql_identifier")
        qid = value.removeprefix("http://www.wikidata.org/entity/")
        require(valid_qid(qid), "invalid_sparql_identifier")
        identifiers.append(qid)
    require(len(identifiers) == len(set(identifiers)), "duplicate_sparql_identifier")
    return identifiers


def discover_classes(client, maximum_classes=2000):
    """Получает ограниченную полную иерархию каждого корня, отклоняя достижение лимита."""
    classes = {}
    for kind, root in ROOT_CLASSES.items():
        query = ("SELECT DISTINCT ?item WHERE { ?item wdt:P279* wd:" + root
                 + " } ORDER BY STR(?item) LIMIT " + str(maximum_classes))
        identifiers = binding_qids(client.sparql(query))
        require(root in identifiers and len(identifiers) < maximum_classes, "incomplete_class_closure")
        for qid in identifiers:
            classes.setdefault(qid, []).append(kind)
    return {qid: sorted(kinds) for qid, kinds in sorted(classes.items())}


def discovery_query(class_qid, cursor, page_size):
    """Строит keyset по строковому URI без OFFSET и без одного запроса на весь каталог."""
    require(valid_qid(class_qid) and (not cursor or valid_qid(cursor)), "invalid_discovery_cursor")
    require(type(page_size) is int and 1 <= page_size <= 500, "invalid_page_size")
    condition = ""
    if cursor:
        condition = ' FILTER(STR(?item) > "http://www.wikidata.org/entity/' + cursor + '")'
    return ("SELECT DISTINCT ?item WHERE { ?item wdt:P31 wd:" + class_qid + condition
            + " } ORDER BY STR(?item) LIMIT " + str(page_size))


# MARK: - Durable Checkpoint

class DiscoveryCheckpoint:
    """Сохраняет страницу ID и её cursor одной SQLite-транзакцией, отдельно от публикации."""

    def __init__(self, path):
        """Открывает локальное состояние под эксклюзивной неблокирующей арендой процесса."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lease = (path.parent / (path.name + ".lock")).open("a+b")
        try:
            fcntl.flock(self.lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lease.close()
            require(False, "checkpoint_busy")
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS shards (
                qid TEXT PRIMARY KEY, kinds TEXT NOT NULL, cursor TEXT NOT NULL, complete INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS candidates (qid TEXT PRIMARY KEY, kinds TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS records (qid TEXT PRIMARY KEY, revision INTEGER NOT NULL, payload BLOB NOT NULL);
        """)

    def close(self):
        """Закрывает соединение и освобождает только свою аренду checkpoint."""
        self.connection.close()
        self.lease.close()

    def initialize(self, classes):
        """Фиксирует версию классов; изменение охвата требует новой сверки, а не сброса cursor."""
        require(bool(classes) and all(valid_qid(qid) for qid in classes), "invalid_class_closure")
        scope_hash = fingerprint({"roots": ROOT_CLASSES, "classes": classes, "schemaVersion": 1})
        current = self.connection.execute("SELECT value FROM metadata WHERE name='scope'").fetchone()
        require(current is None or current[0] == scope_hash, "discovery_scope_changed")
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO metadata VALUES ('scope',?)", (scope_hash,))
            for qid, kinds in classes.items():
                self.connection.execute("INSERT OR IGNORE INTO shards VALUES (?,?,?,0)",
                                        (qid, json.dumps(kinds), ""))

    def next_shard(self):
        """Возвращает первую незавершённую часть без изменения её состояния."""
        return self.connection.execute("SELECT qid,cursor,kinds FROM shards WHERE complete=0 ORDER BY qid LIMIT 1").fetchone()

    def commit_page(self, class_qid, expected_cursor, identifiers, page_size):
        """Атомарно добавляет все ID и cursor; повтор страницы безопасен, старый writer отклоняется."""
        require(len(identifiers) <= page_size and identifiers == sorted(set(identifiers))
                and all(valid_qid(qid) and qid > expected_cursor for qid in identifiers),
                "invalid_discovery_order")
        with self.connection:
            shard = self.connection.execute("SELECT cursor,kinds,complete FROM shards WHERE qid=?", (class_qid,)).fetchone()
            require(shard is not None and shard[0] == expected_cursor and not shard[2], "checkpoint_conflict")
            shard_kinds = set(json.loads(shard[1]))
            for qid in identifiers:
                previous = self.connection.execute("SELECT kinds FROM candidates WHERE qid=?", (qid,)).fetchone()
                kinds = sorted(shard_kinds | (set(json.loads(previous[0])) if previous else set()))
                self.connection.execute("INSERT OR REPLACE INTO candidates VALUES (?,?)", (qid, json.dumps(kinds)))
            cursor = identifiers[-1] if identifiers else expected_cursor
            self.connection.execute("UPDATE shards SET cursor=?,complete=? WHERE qid=?",
                                    (cursor, int(len(identifiers) < page_size), class_qid))

    def save_record(self, record):
        """Сохраняет завершённую карточку; сбой нормализации не стирает предыдущую ревизию."""
        payload = canonical_bytes(record)
        with self.connection:
            previous = self.connection.execute("SELECT revision FROM records WHERE qid=?", (record["qid"],)).fetchone()
            require(previous is None or record["source"]["revision"] >= previous[0], "source_revision_regressed")
            if previous is not None and record["source"]["revision"] == previous[0]:
                return
            self.connection.execute("INSERT OR REPLACE INTO records VALUES (?,?,?)",
                                    (record["qid"], record["source"]["revision"], payload))

    def pending_identifiers(self, limit=50):
        """Возвращает небольшой пакет ещё не нормализованных ID без копирования всей очереди в память."""
        require(type(limit) is int and 1 <= limit <= 50, "invalid_entity_batch")
        return [row[0] for row in self.connection.execute("""SELECT candidates.qid FROM candidates
            LEFT JOIN records ON candidates.qid=records.qid WHERE records.qid IS NULL
            ORDER BY candidates.qid LIMIT ?""", (limit,))]

    def summary(self):
        """Отделяет завершённость обхода от количества карточек и границы публикации."""
        shard_count, complete_count = self.connection.execute("SELECT COUNT(*),COALESCE(SUM(complete),0) FROM shards").fetchone()
        return {"shards": shard_count, "completedShards": complete_count,
                "candidates": self.connection.execute("SELECT COUNT(*) FROM candidates").fetchone()[0],
                "records": self.connection.execute("SELECT COUNT(*) FROM records").fetchone()[0],
                "discoveryComplete": bool(shard_count and shard_count == complete_count),
                "publishedWatermark": None}


def resume_discovery(client, checkpoint, maximum_pages=4, page_size=100):
    """Продолжает ограниченное число страниц; при сетевой ошибке сохраняется последняя транзакция."""
    completed_pages = 0
    for _page in range(maximum_pages):
        shard = checkpoint.next_shard()
        if shard is None:
            break
        class_qid, cursor, _kinds = shard
        identifiers = binding_qids(client.sparql(discovery_query(class_qid, cursor, page_size)))
        checkpoint.commit_page(class_qid, cursor, identifiers, page_size)
        completed_pages += 1
    return {**checkpoint.summary(), "pagesThisRun": completed_pages}
