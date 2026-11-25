# Created by d.kotina on 24.11.2026
"""Публичный адресный индекс show/season → related QID, не требующий выгрузки всех эпизодов на телефон."""

from scripts.wikimedia.common import require, valid_qid
from scripts.wikimedia.contract import validate_firebase_value
from scripts.wikimedia.related_links import related_parent_candidates

# MARK: - Relationship Index Contract

def validate_relationship_row(document):
    """Проверяет точные owner/QID/role и детерминированный range key; номера не являются идентичностью."""
    require(isinstance(document, dict) and set(document) == {"key", "kind", "scope", "owner", "role", "qid"}
            and document.get("kind") == "relationship" and document.get("scope") in {"work", "season"}
            and valid_qid(document.get("owner")) and valid_qid(document.get("qid"))
            and document.get("role") in {"season", "episode"}, "invalid_relationship_row")
    require(document["scope"] != "season" or document["role"] == "episode", "invalid_season_relationship_role")
    prefix = "w" if document["scope"] == "work" else "s"
    require(document["key"] == ":".join((prefix, document["owner"], document["role"], document["qid"])),
            "relationship_key_mismatch")
    validate_firebase_value(document)


def relationship_row(scope, owner, role, qid):
    """Создаёт одну bounded строку для keyset paging по родителю; набор values не растёт вместе с сериалом."""
    prefix = "w" if scope == "work" else "s"
    result = {"key": ":".join((prefix, owner, role, qid)), "kind": "relationship", "scope": scope,
              "owner": owner, "role": role, "qid": qid}
    validate_relationship_row(result)
    return result


def document_relationship_rows(document, get_related):
    """Индексирует проверенные works и исходные parent links только к сезонам с общим подтверждённым show."""
    for work in sorted(document["works"]):
        yield relationship_row("work", work, document["kind"], document["qid"])
    if document["kind"] == "episode":
        for parent in sorted(related_parent_candidates(document)):
            season = get_related(parent)
            if season is not None and season["kind"] == "season" and set(document["works"]) & set(season["works"]):
                yield relationship_row("season", parent, "episode", document["qid"])


def relationship_index(documents):
    """Строит небольшую in-memory версию того же индекса для проверок codec и направленной выборки."""
    return {row["key"]: row for document in documents.values()
            for row in document_relationship_rows(document, documents.get)}
