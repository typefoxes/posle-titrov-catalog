# Created by d.kotina on 24.11.2026
"""Публичный адресный индекс show/season → related QID, не требующий выгрузки всех эпизодов на телефон."""

import re

from scripts.wikimedia.common import require, valid_qid
from scripts.wikimedia.contract import validate_firebase_value
from scripts.wikimedia.related_links import related_parent_candidates

# MARK: - Relationship Index Contract

def validate_relationship_row(document):
    """Проверяет точные owner/QID/role и детерминированный range key; номера не являются идентичностью."""
    require(isinstance(document, dict) and set(document) == {"key", "kind", "scope", "owner", "role", "qid"}
            and document.get("kind") == "relationship" and document.get("scope") in {"work", "season"}
            and valid_qid(document.get("owner")) and valid_qid(document.get("qid"))
            and document.get("role") in {"season", "episode", "episodeOrder"}, "invalid_relationship_row")
    require(document["scope"] != "season" or document["role"] != "season", "invalid_season_relationship_role")
    prefix = "w" if document["scope"] == "work" else "s"
    suffix = document["qid"]
    if document["role"] == "episodeOrder":
        components = document["key"].split(":")
        require(len(components) == 5 and (components[3] == "u" or (re.fullmatch(r"n[0-9]{10}", components[3])
                and int(components[3][1:]) <= 2_147_483_647)), "invalid_episode_order_key")
        suffix = components[3] + ":" + suffix
    require(document["key"] == ":".join((prefix, document["owner"], document["role"], suffix)),
            "relationship_key_mismatch")
    validate_firebase_value(document)


def relationship_row(scope, owner, role, qid):
    """Создаёт одну bounded строку для keyset paging по родителю; набор values не растёт вместе с сериалом."""
    prefix = "w" if scope == "work" else "s"
    result = {"key": ":".join((prefix, owner, role, qid)), "kind": "relationship", "scope": scope,
              "owner": owner, "role": role, "qid": qid}
    validate_relationship_row(result)
    return result


def confirmed_ordinal(document, owner):
    """Читает единственный точный P1545 выбранного родителя с той же политикой rank, что нативный клиент."""
    numbers = set()
    for field in ("series", "partOf", "season"):
        statements = [value for value in document.get("facts", {}).get(field, {}).values()
                      if value["rank"] in {"normal", "preferred"}]
        preferred = [value for value in statements if value["rank"] == "preferred"]
        for statement in preferred or statements:
            value = statement["value"]
            reference = value.get("value")
            if value["state"] != "value" or not isinstance(reference, dict) or reference.get("id") != owner:
                continue
            for qualifier in statement.get("qualifiers", {}).get("P1545", {}).values():
                text = qualifier.get("value")
                if qualifier["state"] == "value" and isinstance(text, str) and re.fullmatch(r"[0-9]+", text):
                    normalized = text.lstrip("0") or "0"
                    if len(normalized) <= 10:
                        number = int(normalized)
                        if number <= 2_147_483_647:
                            numbers.add(number)
    return next(iter(numbers)) if len(numbers) == 1 else None


def ordered_episode_row(scope, owner, document):
    """Добавляет адресный порядок страниц; настоящий episode QID остаётся отдельной неизменной идентичностью."""
    number = confirmed_ordinal(document, owner)
    order = "u" if number is None else "n" + format(number, "010d")
    prefix = "w" if scope == "work" else "s"
    result = {"key": ":".join((prefix, owner, "episodeOrder", order, document["qid"])),
              "kind": "relationship", "scope": scope, "owner": owner, "role": "episodeOrder", "qid": document["qid"]}
    validate_relationship_row(result)
    return result


def document_relationship_rows(document, get_related):
    """Индексирует проверенные works и исходные parent links только к сезонам с общим подтверждённым show."""
    for work in sorted(document["works"]):
        yield relationship_row("work", work, document["kind"], document["qid"])
        if document["kind"] == "episode":
            yield ordered_episode_row("work", work, document)
    if document["kind"] == "episode":
        for parent in sorted(related_parent_candidates(document)):
            season = get_related(parent)
            if season is not None and season["kind"] == "season" and set(document["works"]) & set(season["works"]):
                yield relationship_row("season", parent, "episode", document["qid"])
                yield ordered_episode_row("season", parent, document)


def relationship_index(documents):
    """Строит небольшую in-memory версию того же индекса для проверок codec и направленной выборки."""
    return {row["key"]: row for document in documents.values()
            for row in document_relationship_rows(document, documents.get)}
