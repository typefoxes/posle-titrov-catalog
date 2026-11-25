# Created by d.kotina on 24.11.2026
"""Проверяемые локальные пакеты каталога и индексов без публикации или смены охвата."""

import math
import re
import unicodedata
from collections import defaultdict
from collections.abc import Mapping

from scripts.wikimedia.assets import catalog_reference_documents, reference_identifiers, validate_asset
from scripts.wikimedia.blocks import MAXIMUM_RECORDS, decode_document_block, encode_document_block
from scripts.wikimedia.common import (CatalogError, canonical_bytes, fingerprint, require, valid_qid)
from scripts.wikimedia.contract import (external_mappings, known_values, validate_firebase_value,
                                       validate_record)
from scripts.wikimedia.relationship_index import relationship_index, validate_relationship_row

# MARK: - Index Contracts

def normalize_search_title(title):
    """Применяет одинаковую Unicode-нормализацию к названию и поисковому префиксу."""
    normalized = unicodedata.normalize("NFKC", title).lower().replace("ё", "е")
    return " ".join("".join(character if character.isalnum() else " " for character in normalized).split())


def search_titles(record):
    """Возвращает все известные названия без перевода и без догадки об оригинальном языке."""
    values = set(record.get("titles", {}).values())
    for aliases in record.get("aliases", {}).values():
        values.update(aliases.values())
    for value in known_values(record, "titleStatements"):
        if isinstance(value, dict) and isinstance(value.get("text"), str):
            values.add(value["text"])
    return sorted({normalize_search_title(value) for value in values if isinstance(value, str)} - {""})


def validate_index_row(document):
    """Проверяет тип строки индекса и точные QID, запрещая произвольные служебные поля."""
    require(isinstance(document, dict) and isinstance(document.get("key"), str) and document["key"],
            "invalid_index_key")
    kind = document.get("kind")
    require(kind in {"search", "external", "route", "pool"}, "invalid_index_kind")
    expected = {"key", "kind", "qid"} if kind == "search" else {"key", "kind", "values"}
    require(set(document) == expected, "unexpected_index_field")
    if kind == "search":
        require(valid_qid(document["qid"]), "invalid_search_qid")
    else:
        values = document["values"]
        require(isinstance(values, dict) and bool(values), "empty_index_values")
        if kind == "route":
            require(valid_qid(document["key"]) and set(values) <= {"records", "synopses", "related", "references"}
                    and all(re.fullmatch(r"[a-f0-9]{64}", value) for value in values.values()), "invalid_route")
        else:
            require(all(valid_qid(qid) and state is True for qid, state in values.items()), "invalid_index_qids")
            require(kind != "pool" or len(values) <= 500, "pool_limit_exceeded")
    validate_firebase_value(document)


def validate_directory_row(document):
    """Проверяет интервал и полный адрес immutable дочернего блока."""
    require(isinstance(document, dict) and set(document) == {"key", "first", "block"}
            and isinstance(document["first"], str) and isinstance(document["key"], str)
            and document["first"] <= document["key"]
            and re.fullmatch(r"[a-f0-9]{64}", document.get("block", "")), "invalid_directory_row")
    validate_firebase_value(document)


# MARK: - Bounded Trees

def pack_rows(rows, validator, identity_field, blocks, anchored=True):
    """Ставит границы по хэшу ключа, чтобы вставка не сдвигала все последующие блоки каталога."""
    ranges = []
    identifiers = sorted(rows)
    pending, current = [], []
    for key in identifiers:
        current.append(key)
        # Граница зависит от ключа, а не от содержимого или позиции строки; средний интервал — 32 ключа.
        if len(current) == MAXIMUM_RECORDS or (anchored and int(fingerprint(key)[-2:], 16) % 32 == 0):
            pending.append(current)
            current = []
    if current:
        pending.append(current)
    while pending:
        selected = pending.pop(0)
        payload = {key: rows[key] for key in selected}
        try:
            digest, envelope = encode_document_block(payload, validator, identity_field)
        except CatalogError as error:
            if str(error) not in {"decoded_block_too_large", "encoded_block_too_large"} or len(selected) == 1:
                raise
            middle = len(selected) // 2
            pending[0:0] = [selected[:middle], selected[middle:]]
            continue
        require(decode_document_block(digest, envelope, validator, identity_field) == payload,
                "collection_roundtrip_mismatch")
        require(digest not in blocks or blocks[digest] == envelope, "block_hash_collision")
        blocks[digest] = envelope
        ranges.append({"key": selected[-1], "first": selected[0], "block": digest})
    return ranges


def build_tree(rows, validator, identity_field, blocks):
    """Создаёт bounded дерево диапазонов; manifest содержит только root, а не весь список QID."""
    if not rows:
        return {"count": 0, "depth": 0}
    ranges = pack_rows(rows, validator, identity_field, blocks)
    leaves, depth = list(ranges), 0
    while len(ranges) > 1:
        depth += 1
        require(depth <= 8, "index_tree_too_deep")
        documents = {item["key"]: item for item in ranges}
        next_ranges = pack_rows(documents, validate_directory_row, "key", blocks, anchored=False)
        # Первый ключ дочернего диапазона отличается от ключа строки-каталога (верхней границы).
        for item in next_ranges:
            item["first"] = documents[item["first"]]["first"]
        ranges = next_ranges
    return {"count": len(rows), "depth": depth, "root": ranges[0]["block"],
            "first": ranges[0]["first"], "last": ranges[0]["key"], "leafRanges": leaves}


def query_tree(blocks, descriptor, start, end, validator, identity_field="key", maximum_rows=60):
    """Читает только пересекающиеся диапазоны, ограничивая строки и проверяя каждый hash."""
    require(isinstance(start, str) and isinstance(end, str) and start <= end
            and type(maximum_rows) is int and 1 <= maximum_rows <= 500, "invalid_tree_query")
    if not descriptor.get("count"):
        return {}
    pending = [(descriptor["root"], descriptor["depth"])]
    visited, result = set(), {}
    while pending and len(result) < maximum_rows:
        digest, depth = pending.pop(0)
        require((digest, depth) not in visited and 0 <= depth <= 8, "invalid_tree_cycle")
        visited.add((digest, depth))
        require(digest in blocks, "missing_index_block")
        rows = decode_document_block(digest, blocks[digest], validate_directory_row if depth else validator,
                                     "key" if depth else identity_field)
        if depth:
            matches = [(row["block"], depth - 1) for _key, row in sorted(rows.items())
                       if row["first"] <= end and row["key"] >= start]
            pending[0:0] = matches
        else:
            for key, row in sorted(rows.items()):
                if start <= key <= end:
                    result[key] = row
                    if len(result) == maximum_rows:
                        break
    return result


# MARK: - Collections and Indices

def index_rows(records):
    """Строит префиксный поиск и точные внешние соответствия без дублирования карточек."""
    search, external = {}, defaultdict(dict)
    for qid, record in sorted(records.items()):
        for title in search_titles(record):
            # UTF-8 hex сохраняет порядок и не вводит запрещённые RTDB-символы.
            prefix = "n" + title.encode("utf-8").hex()
            key = prefix + ":" + qid
            # Это decoded ключ внутри bounded envelope; RTDB хранит только SHA и base64, не этот map.
            search[key] = {"kind": "search", "key": key, "qid": qid}
        for identifier in external_mappings(record):
            external[identifier][qid] = True
    return search, {key: {"kind": "external", "key": key, "values": values}
                    for key, values in sorted(external.items())}


def recommendation_keys(record):
    """Не предлагает карточки без настоящего названия; mixed входит во все подтверждённые категории."""
    titles = record.get("titles", {})
    original = [value.get("text") for value in known_values(record, "titleStatements") if isinstance(value, dict)]
    if not any(isinstance(value, str) and value.strip() for value in [titles.get("ru"), titles.get("en"), *original]):
        return set()
    kinds = set(record.get("classifications", {})) if record["kind"] == "mixed" else {record["kind"]}
    keys = {"kind:" + kind for kind in kinds}
    keys.update("genre:" + genre["id"] + ":" + kind for kind in kinds
                for genre in known_values(record, "genres") if isinstance(genre, dict) and valid_qid(genre.get("id")))
    return keys


def recommendation_priority(record, key, has_synopsis=False):
    """Предпочитает известное русское название и сюжет; оставшийся порядок воспроизводим и не объявляется популярностью."""
    titles = record.get("titles", {})
    language = 0 if titles.get("ru", "").strip() else (1 if titles.get("en", "").strip() else 2)
    return str(language * 2 + int(not has_synopsis)) + fingerprint({"pool": key, "qid": record["qid"]})


def recommendation_rows(records, synopsis_qids=()):
    """Создаёт воспроизводимые пулы до500 названных произведений без обучения на личных данных."""
    candidates = defaultdict(set)
    for qid, record in records.items():
        for key in recommendation_keys(record):
            candidates[key].add(qid)
    return {key: {"kind": "pool", "key": key, "values": {qid: True for qid in sorted(
            sorted(identifiers, key=lambda qid: recommendation_priority(records[qid], key, qid in synopsis_qids))[:500])}}
            for key, identifiers in sorted(candidates.items())}


def prepare_bundle(records, assets):
    """Собирает проверенный локальный пакет с раздельными лицензиями и общей таблицей маршрутов."""
    require(assets.get("inputSHA256") == fingerprint(records)
            and assets.get("completeForRequestedSample") is True, "incomplete_or_foreign_assets")
    require(set(assets.get("collections", {})) == {"synopses", "related", "references"}, "missing_asset_collection")
    prepared = assets["collections"]
    require(set(prepared["synopses"]) <= set(records), "orphan_synopsis")
    for document in prepared["related"].values():
        require(set(document.get("works", {})) <= {qid for qid, item in records.items() if item["kind"] != "film"},
                "orphan_related_parent")
    require(set(prepared["references"]) == set(reference_identifiers(catalog_reference_documents(records, prepared["related"]))),
            "dictionary_coverage_mismatch")
    blocks, descriptors, locations = {}, {}, defaultdict(dict)
    collections = {"records": records, **assets["collections"]}
    for name, documents in collections.items():
        if name != "records":
            allowed_kinds = {"season", "episode"} if name == "related" else {"synopsis" if name == "synopses" else "reference"}
            require(all(document.get("kind") in allowed_kinds for document in documents.values()), "wrong_asset_collection")
        descriptor = build_tree(documents, validate_record if name == "records" else validate_asset, "qid", blocks)
        for leaf in descriptor.pop("leafRanges", []):
            rows = decode_document_block(leaf["block"], blocks[leaf["block"]],
                                         validate_record if name == "records" else validate_asset)
            for qid in rows:
                locations[qid][name] = leaf["block"]
        descriptors[name] = descriptor
    search, external = index_rows(records)
    indices = {"search": search, "external": external, "pools": recommendation_rows(records, assets["collections"]["synopses"]),
               "relationships": relationship_index(prepared["related"]),
               "routes": {qid: {"key": qid, "kind": "route", "values": values}
                          for qid, values in sorted(locations.items())}}
    for name, rows in indices.items():
        descriptor = build_tree(rows, validate_relationship_row if name == "relationships" else validate_index_row, "key", blocks)
        descriptor.pop("leafRanges", None)
        descriptors[name] = descriptor
    manifest = {"schemaVersion": 1, "encoding": "zlib-base64", "collections": descriptors,
                "inputSHA256": fingerprint(records), "fullScopeVerified": False,
                "publicationAllowed": False, "media": "no_verified_posters"}
    validate_firebase_value(manifest)
    inventory = {digest: len(canonical_bytes(envelope)) for digest, envelope in blocks.items()}
    for descriptor in descriptors.values():
        require(not descriptor["count"] or descriptor["root"] in blocks, "missing_collection_root")
    return {"manifest": manifest, "manifestSHA256": fingerprint(manifest), "blocks": blocks,
            "inventory": inventory, "totalBlockBytes": sum(inventory.values()),
            "canonicalFirebaseSubtreeBytes": len(canonical_bytes({"manifest": manifest, "blocks": blocks}))}


# MARK: - Serialized Client Reads

class SerializedReadTrace(Mapping):
    """Считает уникальные JSON-блоки локального сценария, не выдавая его за сетевой биллинг."""

    def __init__(self, blocks):
        """Сохраняет ссылки на готовые immutable объекты и пустой локальный кэш чтения."""
        self.blocks = blocks
        self.read_hashes = set()

    def __getitem__(self, digest):
        """Учитывает первый доступ к блоку как один адресный GET в модели клиента."""
        self.read_hashes.add(digest)
        return self.blocks[digest]

    def __iter__(self):
        """Передаёт перебор ключей исходному mapping без копирования envelopes в RAM."""
        return iter(self.blocks)

    def __len__(self):
        """Возвращает число адресных объектов исходного mapping."""
        return len(self.blocks)

    def __contains__(self, digest):
        """Проверяет наличие адреса, не считая проверку отдельным сетевым чтением."""
        return digest in self.blocks


def serialized_search_read(bundle, query):
    """Проверяет путь prefix → до 15 QID → routes → карточки и считает фактические байты JSON."""
    normalized = normalize_search_title(query)
    require(len(normalized) >= 2 and len(normalized.encode("utf-8")) <= 300, "invalid_search_query")
    blocks = SerializedReadTrace(bundle["blocks"])
    descriptors = bundle["manifest"]["collections"]
    prefix = "n" + normalized.encode("utf-8").hex()
    hits = query_tree(blocks, descriptors["search"], prefix, prefix + "\uf8ff", validate_index_row)
    identifiers = list(dict.fromkeys(row["qid"] for row in hits.values()))[:15]
    for qid in identifiers:
        route = query_tree(blocks, descriptors["routes"], qid, qid, validate_index_row)
        require(set(route) == {qid} and "records" in route[qid]["values"], "missing_search_route")
        digest = route[qid]["values"]["records"]
        records = decode_document_block(digest, blocks[digest], validate_record)
        require(qid in records and any(title.startswith(normalized) for title in search_titles(records[qid])),
                "stale_search_index")
    payload_bytes = len(canonical_bytes(bundle["manifest"])) + sum(bundle["inventory"][digest] for digest in blocks.read_hashes)
    return {"query": query, "cards": len(identifiers), "uniqueBlockReads": len(blocks.read_hashes),
            "manifestReads": 1, "serializedJSONBytes": payload_bytes,
            "realNetworkOrBillingMeasured": False, "detailsReferencesAndImagesIncluded": False}


# MARK: - Capacity

def bundle_capacity(bundle, assets, full_work_count, full_show_upper_bound, baseline_bytes,
                    checkpoint_measurement, compact_checkpoint=None):
    """Учитывает все подготовленные части и явно выделяет неизвестные полноразмерные расходы."""
    require(type(full_work_count) is int and full_work_count > 0 and type(full_show_upper_bound) is int
            and full_show_upper_bound > 0 and baseline_bytes >= 0, "invalid_capacity_scope")
    manifest, blocks = bundle["manifest"], bundle["blocks"]
    works = manifest["collections"]["records"]["count"]
    require(works > 0, "empty_capacity_sample")
    components = {}
    for name, descriptor in manifest["collections"].items():
        pending = [(descriptor.get("root"), descriptor["depth"])] if descriptor["count"] else []
        visited = set()
        while pending:
            digest, depth = pending.pop()
            require(digest not in visited, "duplicate_tree_block")
            visited.add(digest)
            if depth:
                rows = decode_document_block(digest, blocks[digest], validate_directory_row, "key")
                pending.extend((row["block"], depth - 1) for row in rows.values())
        measured = sum(bundle["inventory"][digest] + len(digest) + 4 for digest in visited)
        denominator, population = works, full_work_count
        if name in {"related", "relationships"} and descriptor["count"]:
            denominator = assets.get("coverage", {}).get("requestedShows", 0)
            require(denominator > 0, "sample_show_count_required")
            population = full_show_upper_bound
        components[name] = {"sampleRows": descriptor["count"], "sampleStoredBytes": measured,
            "projectedFullStoredBytes": math.ceil(measured * population / denominator),
            "sampleDenominator": denominator, "populationScenario": population,
            "projectionOnly": True}
    state_bytes = checkpoint_measurement["storedEnvelopeBytes"]
    state_records = checkpoint_measurement["records"]
    require(state_records > 0, "empty_state_measurement")
    # Две закрытые recovery-копии состояния; это кандидат, а не уже реализованный remote checkpoint.
    projected_state = math.ceil(state_bytes * full_work_count / state_records) * 2
    naive_projected_state = projected_state
    if compact_checkpoint is not None:
        require(compact_checkpoint["candidates"] > 0
                and compact_checkpoint["requiresSharedImmutableRecordBlocks"] is True, "invalid_compact_checkpoint")
        projected_state = 2 * (compact_checkpoint["shardTreeBytes"] + compact_checkpoint["headBytes"]
            + math.ceil(compact_checkpoint["candidateTreeBytes"] * full_work_count / compact_checkpoint["candidates"]))
    projected_catalog = sum(component["projectedFullStoredBytes"] for component in components.values())
    projected_total = baseline_bytes + math.ceil((projected_catalog * 2 + projected_state) * 1.25)
    maximum_block = max(bundle["inventory"].values(), default=0)
    return {"schemaVersion": 1, "sampleWorks": works, "fullWorkCount": full_work_count,
        "fullShowCountUpperBound": full_show_upper_bound, "components": components,
        "sampleCanonicalFirebaseSubtreeBytes": bundle["canonicalFirebaseSubtreeBytes"],
        "manifestBytes": len(canonical_bytes(manifest)), "maximumBlockBytes": maximum_block,
        "checkpoint": checkpoint_measurement, "projectedTwoStateCopiesBytes": projected_state,
        "naiveFullSQLiteTwoStateCopiesBytes": naive_projected_state,
        "compactCheckpoint": compact_checkpoint or {"measured": False},
        "baselineAndValidationBytes": baseline_bytes, "projectedTwoCatalogVersionsBytes": projected_catalog * 2,
        "projectedTotalWith25PercentReserveBytes": projected_total, "storageStopBytes": 750_000_000,
        "projectionExceedsStop": projected_total > 750_000_000,
        "trafficScenarios": [{"searchesPerDay": searches,
            "assumedUncachedBlockReadsPerSearch": 6,
            "monthlyBytesWith25PercentTransportReserve": math.ceil(searches * 30 * 6 * maximum_block * 1.25),
            "billingMeasured": False} for searches in (100, 1000, 5000)],
        "bootstrapMinimumEntityRequests": math.ceil(full_work_count / 50),
        "sourceResources": assets.get("initialResources", assets["resources"]),
        "repeatResources": assets["resources"], "actionsRunnerMeasured": False,
        "capacityVerified": False, "publicationAllowed": False,
        "openMeasurements": ["actual_full_catalog_and_unique_references", "full_episode_graph",
            "verified_media_metadata", "remote_checkpoint_strategy_and_traffic", "actual_RTDB_storage_overhead",
            "Linux_Actions_runtime_and_daily_changes"],
        "limitations": "Directed sample; linear component/state projections are planning scenarios, not measured full scope. Shared references must not be assumed to grow linearly; global deduplication/cardinalities remain open. Related entities use the sample show denominator and full show-count upper bound, not an episode-coverage claim. Client traffic is an explicit uncached six-block scenario; details and updater traffic are additional."}
