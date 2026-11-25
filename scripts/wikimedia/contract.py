# Created by d.kotina on 24.11.2026
"""Lossless QID, происхождение и проекция структурированных метаданных без подмены сюжета."""

import re
from urllib.parse import quote

from scripts.wikimedia.common import ROOT_CLASSES, SCHEMA_VERSION, canonical_bytes, require, valid_qid, valid_wikibase_identifier

# MARK: - Properties

PROPERTY_FIELDS = {
    "P31": "instanceOf", "P1476": "titleStatements", "P577": "publicationDates",
    "P2047": "duration", "P136": "genres", "P495": "countries", "P364": "languages",
    "P57": "directors", "P58": "screenwriters", "P161": "cast", "P449": "networks",
    "P580": "startDates", "P582": "endDates", "P179": "series", "P361": "partOf",
    "P527": "parts", "P1113": "episodeCount", "P2437": "seasonCount",
    "P1545": "ordinal", "P4908": "season", "P18": "imageCandidates", "P154": "logoCandidates",
    "P345": "imdb", "P2603": "kinopoisk", "P4947": "tmdbMovie", "P4983": "tmdbShow",
    "P4835": "tvdb",
}
EXTERNAL_PROPERTIES = {"P345": r"tt[0-9]{7,10}", "P2603": r"[1-9][0-9]*",
                       "P4947": r"[1-9][0-9]*", "P4983": r"[1-9][0-9]*",
                       "P4835": r"[1-9][0-9]*"}
EXTERNAL_WORK_TYPES = {"P4947": "movie", "P4983": "show", "P4835": "show"}


# MARK: - Source Values

def ordered_map(values, prefix):
    """Заменяет массив устойчивой картой без случайных SHA-ключей у каждого значения."""
    unique = {canonical_bytes(value): value for value in values}
    return {prefix + format(number, "03d"): unique[key] for number, key in enumerate(sorted(unique))}

def source_value(snak):
    """Сохраняет unknown/no-value отдельно, а известное значение — с точностью источника."""
    snak_type = snak.get("snaktype")
    require(snak_type in ("value", "somevalue", "novalue"), "invalid_snak_type")
    if snak_type != "value":
        return {"state": snak_type}
    data_value = snak.get("datavalue")
    require(isinstance(data_value, dict) and "value" in data_value, "missing_data_value")
    value_type = data_value.get("type")
    require(value_type in ("string", "wikibase-entityid", "time", "quantity", "monolingualtext"),
            "unsupported_data_value")
    value = data_value["value"]
    if value_type == "wikibase-entityid":
        require(isinstance(value, dict) and valid_wikibase_identifier(value.get("id")), "invalid_entity_value")
        value = {"id": value["id"]}
    elif value_type == "time":
        require(isinstance(value, dict) and isinstance(value.get("time"), str)
                and type(value.get("precision")) is int and 0 <= value["precision"] <= 14
                and type(value.get("timezone")) is int
                and isinstance(value.get("calendarmodel"), str), "invalid_source_date")
        value = {name: value[name] for name in
                 ("time", "precision", "timezone", "before", "after", "calendarmodel") if name in value}
    elif value_type == "quantity":
        require(isinstance(value, dict) and isinstance(value.get("amount"), str)
                and isinstance(value.get("unit"), str), "invalid_quantity")
        value = {name: value[name] for name in ("amount", "unit", "upperBound", "lowerBound") if name in value}
    elif value_type == "monolingualtext":
        require(isinstance(value, dict) and isinstance(value.get("text"), str)
                and isinstance(value.get("language"), str), "invalid_original_title")
        value = {"text": value["text"], "language": value["language"]}
    else:
        require(isinstance(value, str), "invalid_string_value")
    return {"state": "value", "type": value_type, "value": value}


def statement_projection(statement):
    """Сохраняет rank, значение и qualifiers; ссылка на ревизию позволяет проверить references."""
    require(statement.get("rank") in ("normal", "preferred", "deprecated"), "invalid_rank")
    result = {"rank": statement["rank"], "value": source_value(statement.get("mainsnak", {}))}
    qualifiers = {}
    for property_id, snaks in statement.get("qualifiers", {}).items():
        require(re.fullmatch(r"P[1-9][0-9]*", property_id) is not None, "invalid_property_id")
        projected = [source_value(snak) for snak in snaks]
        if projected:
            qualifiers[property_id] = ordered_map(projected, "v")
    if qualifiers:
        result["qualifiers"] = qualifiers
    return result


def known_values(record, field):
    """Возвращает только подтверждённые недепрекатные значения выбранного поля."""
    statements = list(record.get("facts", {}).get(field, {}).values())
    active = [statement for statement in statements if statement["rank"] != "deprecated"]
    preferred = [statement for statement in active if statement["rank"] == "preferred"]
    return [statement["value"]["value"] for statement in preferred or active
            if statement["value"]["state"] == "value"]


# MARK: - Record

def entity_source(entity, retrieved_at):
    """Проверяет QID/ревизию и возвращает единое происхождение структурированных данных."""
    qid = entity.get("id")
    require(valid_qid(qid) and entity.get("type") == "item" and "missing" not in entity,
            "invalid_or_missing_entity")
    require(type(entity.get("lastrevid")) is int and entity["lastrevid"] > 0, "missing_revision")
    require(isinstance(retrieved_at, str) and bool(retrieved_at), "missing_retrieval_time")
    return {"qid": qid, "revision": entity["lastrevid"], "retrievedAt": retrieved_at,
            "url": "https://www.wikidata.org/wiki/Special:EntityData/" + qid + ".json?revision="
                   + str(entity["lastrevid"]), "license": "CC0-1.0"}


def normalize_entity(entity, kind, retrieved_at, classifications=None):
    """Заменяет только завершённую сущность; короткое description остаётся описанием сущности."""
    qid = entity.get("id")
    require(valid_qid(qid) and entity.get("type") == "item" and "missing" not in entity,
            "invalid_or_missing_entity")
    require(kind in ("film", "series", "miniseries", "mixed"), "invalid_work_kind")
    require(type(entity.get("lastrevid")) is int and entity["lastrevid"] > 0, "missing_revision")
    for field in ("labels", "descriptions", "aliases", "claims", "sitelinks"):
        require(isinstance(entity.get(field), dict), "partial_entity")
    source = entity_source(entity, retrieved_at)
    record = {"schemaVersion": SCHEMA_VERSION, "qid": qid, "kind": kind, "source": source}
    if kind == "mixed":
        require(isinstance(classifications, set) and "film" in classifications
                and bool(classifications & {"series", "miniseries"}) and classifications <= set(ROOT_CLASSES),
                "mixed_classifications_required")
        record["classifications"] = {name: True for name in sorted(classifications)}
    else:
        require(classifications is None, "unexpected_classifications")
    for source_field, result_field in (("labels", "titles"), ("descriptions", "entityDescriptions")):
        localized = {language: item["value"] for language, item in entity[source_field].items()
                     if language in ("ru", "en") and isinstance(item.get("value"), str) and item["value"]}
        if localized:
            record[result_field] = localized
    aliases = {}
    for language in ("ru", "en"):
        values = ordered_map([item["value"] for item in entity["aliases"].get(language, [])
                              if isinstance(item.get("value"), str) and item["value"]], "a")
        if values:
            aliases[language] = values
    if aliases:
        record["aliases"] = aliases
    facts = {}
    for property_id, field in PROPERTY_FIELDS.items():
        statements = entity["claims"].get(property_id, [])
        projected = [statement_projection(statement) for statement in statements]
        if projected:
            facts[field] = ordered_map(projected, "s")
    if facts:
        record["facts"] = facts
    sitelinks = {}
    for language in ("ru", "en"):
        title = entity["sitelinks"].get(language + "wiki", {}).get("title")
        if isinstance(title, str) and title:
            sitelinks[language] = {"title": title, "url": "https://" + language
                                   + ".wikipedia.org/wiki/" + quote(title.replace(" ", "_"), safe="")}
    if sitelinks:
        record["wikipediaCandidates"] = sitelinks
    validate_record(record)
    return record


def validate_record(record):
    """Проверяет идентичность, происхождение и отсутствие неподтверждённого сюжета/постера."""
    require(isinstance(record, dict) and type(record.get("schemaVersion")) is int
            and record["schemaVersion"] == SCHEMA_VERSION,
            "invalid_schema_version")
    require(valid_qid(record.get("qid")), "invalid_qid")
    require(record.get("kind") in {*ROOT_CLASSES, "mixed"}, "invalid_work_kind")
    classifications = record.get("classifications")
    if record["kind"] == "mixed":
        require(isinstance(classifications, dict) and "film" in classifications
                and bool(set(classifications) & {"series", "miniseries"}) and set(classifications) <= set(ROOT_CLASSES)
                and all(value is True for value in classifications.values()), "mixed_classifications_required")
    else:
        require(classifications is None, "unexpected_classifications")
    require(record.get("source", {}).get("qid") == record["qid"]
            and type(record["source"].get("revision")) is int, "invalid_record_source")
    require(set(record) <= {"schemaVersion", "qid", "kind", "source", "titles", "entityDescriptions",
                           "aliases", "facts", "wikipediaCandidates", "classifications"}, "unexpected_record_field")
    validate_firebase_value(record)


def validate_firebase_value(value):
    """Запрещает пустые структуры и небезопасные ключи, которые RTDB изменит при readback."""
    if isinstance(value, dict):
        require(bool(value), "empty_firebase_object")
        for name, child in value.items():
            require(isinstance(name, str) and name and not re.search(r"[.$#\[\]/\x00-\x1f\x7f]", name),
                    "invalid_firebase_key")
            require(len(name.encode("utf-8")) <= 768, "firebase_key_too_large")
            validate_firebase_value(child)
    elif isinstance(value, list) or value is None:
        raise_value_error()
    else:
        require(type(value) in (str, bool, int, float), "invalid_firebase_value")


def raise_value_error():
    """Отклоняет null и массивы: формат использует явные sparse maps для устойчивого хэширования."""
    require(False, "unstable_firebase_value")


def external_mappings(record):
    """Создаёт только точные типизированные ключи; несколько QID остаются неоднозначными."""
    mappings = {}
    if record["kind"] == "mixed":
        return mappings
    work_type = "movie" if record["kind"] == "film" else "show"
    for property_id, pattern in EXTERNAL_PROPERTIES.items():
        if property_id in EXTERNAL_WORK_TYPES and EXTERNAL_WORK_TYPES[property_id] != work_type:
            continue
        field = PROPERTY_FIELDS[property_id]
        for value in known_values(record, field):
            if isinstance(value, str) and re.fullmatch(pattern, value):
                mappings[field + ":" + work_type + ":" + value] = record["qid"]
    return mappings
