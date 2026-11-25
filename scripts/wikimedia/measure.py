# Created by d.kotina on 24.11.2026
"""Воспроизводимые небольшие выборки, покрытие полей и консервативный Spark-бюджет."""

import gzip
import statistics
from datetime import datetime, timezone
from pathlib import Path

from scripts.wikimedia.common import (ROOT_CLASSES, CatalogError, atomic_json, canonical_bytes,
                                     fingerprint, read_json, require, valid_qid)
from scripts.wikimedia.contract import external_mappings, known_values, normalize_entity
from scripts.wikimedia.discovery import binding_qids

# MARK: - Sample Design

def sample_queries():
    """Объявляет strata до измерения; это направленная выборка, а не случайный опрос всего каталога."""
    queries = {}
    for kind, root in ROOT_CLASSES.items():
        base = "?item wdt:P31 wd:" + root + " . "
        for label, condition in (("early", ""), ("middle", 'FILTER(STR(?item) > "http://www.wikidata.org/entity/Q4")'),
                                 ("late", 'FILTER(STR(?item) > "http://www.wikidata.org/entity/Q8")')):
            queries[kind + "-" + label] = (kind, "SELECT DISTINCT ?item WHERE { " + base + condition
                                           + " } ORDER BY STR(?item) LIMIT 5")
        queries[kind + "-russia"] = (kind, "SELECT DISTINCT ?item WHERE { " + base
            + " VALUES ?country { wd:Q159 wd:Q15180 } ?item wdt:P495 ?country . } ORDER BY STR(?item) LIMIT 5")
    queries["film-announced"] = ("film", "SELECT DISTINCT ?item WHERE { ?item wdt:P31 wd:Q11424; wdt:P577 ?date ."
        + ' FILTER(?date >= "2026-11-24T00:00:00Z"^^xsd:dateTime) } ORDER BY STR(?item) LIMIT 5')
    queries["series-ended"] = ("series", "SELECT DISTINCT ?item WHERE { ?item wdt:P31 wd:Q5398426; wdt:P582 ?date ."
        + " } ORDER BY STR(?item) LIMIT 5")
    queries["series-unknown-start"] = ("series", "SELECT DISTINCT ?item WHERE { ?item wdt:P31 wd:Q5398426 ."
        + " FILTER NOT EXISTS { ?item wdt:P580 ?date } } ORDER BY STR(?item) LIMIT 5")
    for label, root in {"short": "Q24862", "documentary": "Q93204", "animated": "Q202866",
                        "television": "Q506240"}.items():
        queries["film-" + label] = ("film", "SELECT DISTINCT ?item WHERE { ?item wdt:P31 wd:" + root
                                   + " } ORDER BY STR(?item) LIMIT 5")
    queries["film-same-title"] = ("film", 'SELECT DISTINCT ?item WHERE { ?item rdfs:label "Dune"@en; '
        + "wdt:P31/wdt:P279* wd:Q11424 } ORDER BY STR(?item) LIMIT 5")
    queries["film-known"] = ("film", "SELECT DISTINCT ?item WHERE { VALUES ?item { wd:Q151599 wd:Q83495 wd:Q60834962 }"
        + " ?item wdt:P31/wdt:P279* wd:Q11424 } ORDER BY STR(?item) LIMIT 5")
    queries["series-known-russian"] = ("series", 'SELECT DISTINCT ?item WHERE { ?item wdt:P2603 "1100777"; '
        + "wdt:P31/wdt:P279* wd:Q5398426 } ORDER BY STR(?item) LIMIT 5")
    queries["series-known-international"] = ("series", "SELECT DISTINCT ?item WHERE { VALUES ?item { wd:Q1079 wd:Q23572 }"
        + " ?item wdt:P31/wdt:P279* wd:Q5398426 } ORDER BY STR(?item) LIMIT 5")
    return queries


def cached_sparql(client, directory, label, query):
    """Кэширует полный ответ по хэшу запроса; повтор не тратит сеть и не теряет происхождение."""
    path = Path(directory) / (label + "-" + fingerprint(query)[:16] + ".json")
    if path.exists():
        return read_json(path)
    payload = client.sparql(query)
    atomic_json(path, payload)
    return payload


def fetch_entities(client, identifiers, allow_missing=False):
    """Получает до 50 сущностей; слишком большой ответ разбивает, сохраняя общий бюджет попыток."""
    require(0 < len(identifiers) <= 50 and all(valid_qid(qid) for qid in identifiers), "invalid_entity_batch")
    try:
        payload = client.action({"action": "wbgetentities", "ids": "|".join(identifiers),
            "props": "info|labels|descriptions|aliases|claims|sitelinks", "languages": "ru|en",
            "sitefilter": "ruwiki|enwiki"})
    except CatalogError as error:
        if str(error) != "wikimedia_response_too_large" or len(identifiers) == 1:
            raise
        middle = len(identifiers) // 2
        return {**fetch_entities(client, identifiers[:middle], allow_missing),
                **fetch_entities(client, identifiers[middle:], allow_missing)}
    entities = payload.get("entities")
    require(isinstance(entities, dict) and set(entities) == set(identifiers), "incomplete_entity_batch")
    for qid in identifiers:
        if allow_missing:
            from scripts.wikimedia.entity_status import unavailable_entity_proof
            unavailable_entity_proof(qid, entities[qid], "identity-validation")
            continue
        require(isinstance(entities[qid], dict) and entities[qid].get("id") == qid,
                "invalid_entity_identity")
        require(allow_missing or "missing" not in entities[qid], "missing_sample_entity")
    return entities


def collect_sample(client, directory):
    """Сохраняет выдачи strata и raw entities до нормализации; ошибки не становятся нулями покрытия."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    strata, candidates = {}, {}
    for label, (kind, query) in sample_queries().items():
        try:
            identifiers = binding_qids(cached_sparql(client, directory / "queries", label, query))
            strata[label] = {"kind": kind, "qids": identifiers, "query": query, "completeResponse": True}
            for qid in identifiers:
                previous = candidates.get(qid)
                # Мини-сериал имеет приоритет над сериалом; пересечение с фильмом остаётся ошибкой.
                require(previous is None or previous == kind or {previous, kind} <= {"series", "miniseries"},
                        "ambiguous_sample_kind")
                candidates[qid] = "miniseries" if "miniseries" in (previous, kind) else kind
        except CatalogError as error:
            strata[label] = {"kind": kind, "query": query, "error": str(error), "completeResponse": False}
    atomic_json(directory / "strata.json", strata)
    pending = [qid for qid in sorted(candidates) if not (directory / "raw" / (qid + ".json")).exists()]
    for offset in range(0, len(pending), 10):
        entities = fetch_entities(client, pending[offset:offset + 10])
        for qid, entity in entities.items():
            atomic_json(directory / "raw" / (qid + ".json"), entity)
    records, failures = {}, {}
    for qid, kind in candidates.items():
        try:
            raw_path = directory / "raw" / (qid + ".json")
            timestamp = datetime.fromtimestamp(raw_path.stat().st_mtime, timezone.utc).isoformat()
            records[qid] = normalize_entity(read_json(raw_path), kind, timestamp)
        except CatalogError as error:
            failures[qid] = str(error)
    atomic_json(directory / "records.json", records)
    atomic_json(directory / "normalization-errors.json", failures)
    return records, strata, failures


# MARK: - Coverage

def record_coverage(record):
    """Различает заполненность структурированных данных и непроверенные кандидаты текста/изображения."""
    return {"russianTitle": bool(record.get("titles", {}).get("ru")),
            "titleStatement": bool(known_values(record, "titleStatements")),
            "russianEntityDescription": bool(record.get("entityDescriptions", {}).get("ru")),
            "russianWikipediaCandidate": bool(record.get("wikipediaCandidates", {}).get("ru")),
            "imageCandidate": bool(known_values(record, "imageCandidates")),
            "duration": bool(known_values(record, "duration")),
            "genres": bool(known_values(record, "genres")),
            "imdb": any(identifier.startswith("imdb:") for identifier in external_mappings(record)),
            "publicationDate": bool(known_values(record, "publicationDates")),
            "seasonCount": bool(known_values(record, "seasonCount")),
            "episodeCount": bool(known_values(record, "episodeCount")),
            "parts": bool(known_values(record, "parts")),
            "endDate": bool(known_values(record, "endDates"))}


def coverage_report(records):
    """Считает числитель и знаменатель отдельно по типам, без экстраполяции полноты мира."""
    result = {}
    for kind in ROOT_CLASSES:
        selected = [record for record in records.values() if record["kind"] == kind]
        fields = {}
        for record in selected:
            for field, present in record_coverage(record).items():
                fields[field] = fields.get(field, 0) + int(present)
        result[kind] = {"records": len(selected), "present": fields}
    return result


def measure_storage(records, legacy_bytes=0, known_minimum_records=0):
    """Оценивает несжатый JSON, индексы, две версии и запас; публикация по выборке запрещена."""
    sizes = sorted(len(canonical_bytes(record)) for record in records.values())
    require(bool(sizes), "empty_measurement_sample")
    mappings = {}
    for record in records.values():
        for identifier, qid in external_mappings(record).items():
            mappings.setdefault(identifier, {})[qid] = True
    # Индекс содержит QID-набор, поэтому неоднозначный внешний ID не сливает записи.
    index_bytes = len(canonical_bytes(mappings))
    catalog_bytes = len(canonical_bytes(records))
    mean_bytes = statistics.mean(sizes)
    projected = int(known_minimum_records * (mean_bytes + index_bytes / len(records)) * 2 * 1.25 + legacy_bytes)
    hard_storage_limit = 750_000_000
    return {"sampleRecords": len(records), "recordsBytes": catalog_bytes,
            "recordsGzipBytes": len(gzip.compress(canonical_bytes(records), mtime=0)),
            "externalIndexBytes": index_bytes, "meanRecordBytes": round(mean_bytes, 2),
            "medianRecordBytes": statistics.median(sizes), "maximumRecordBytes": max(sizes),
            "knownMinimumRecords": known_minimum_records,
            "projectedTwoFullVersionsWith25PercentOverheadBytes": projected,
            "legacyBytes": legacy_bytes, "storageStopBytes": hard_storage_limit,
            "projectionExceedsStorageStop": projected > hard_storage_limit,
            "capacityVerified": False, "publicationAllowed": False,
            "projectionLimitations": "direct-class lower bound; directed sample; excludes full search, episode graph, texts, media and actual RTDB overhead"}


def measure_recent_changes(client, directory, start, end):
    """Измеряет короткий фиксированный интервал глобальной ленты с continuation и дедупликацией."""
    from datetime import datetime

    require(0 < (datetime.fromisoformat(end.replace("Z", "+00:00"))
                 - datetime.fromisoformat(start.replace("Z", "+00:00")) ).total_seconds() <= 60,
            "change_probe_interval_too_large")
    continuation, events, pages = {}, {}, 0
    seen_continuations = set()
    response_bytes = 0
    while pages < 8:
        payload = client.action({"action": "query", "list": "recentchanges", "rcnamespace": 0,
            "rcstart": start, "rcend": end, "rcdir": "newer", "rclimit": 500,
            "rcprop": "title|ids|timestamp|loginfo", "rctype": "edit|new|log", **continuation})
        atomic_json(Path(directory) / ("recent-changes-" + str(pages) + ".json"), payload)
        changes = payload.get("query", {}).get("recentchanges")
        require(isinstance(changes, list), "invalid_change_page")
        for event in changes:
            require(type(event.get("rcid")) is int and isinstance(event.get("timestamp"), str),
                    "invalid_change_event")
            events[event["rcid"]] = event
        response_bytes += len(canonical_bytes(payload))
        pages += 1
        continuation = payload.get("continue", {})
        if not continuation:
            break
        require(fingerprint(continuation) not in seen_continuations, "repeated_change_continuation")
        seen_continuations.add(fingerprint(continuation))
    interval = (datetime.fromisoformat(end.replace("Z", "+00:00"))
                - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds()
    return {"start": start, "end": end, "pages": pages, "uniqueEvents": len(events),
            "canonicalResponseBytes": response_bytes, "intervalComplete": not continuation,
            "continuation": continuation, "dailyEventProjection": round(len(events) * 86400 / interval),
            "dailyResponseByteProjection": round(response_bytes * 86400 / interval),
            "projectionIsMeasuredIntervalOnly": True, "publishedWatermarkAdvanced": False}
