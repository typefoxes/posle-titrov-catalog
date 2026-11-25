# Created by d.kotina on 24.11.2026
"""Точные API relationships по прежним четырём паттернам, без поиска родителя по названию или IMDb."""

from scripts.wikimedia.assets import validate_asset
from scripts.wikimedia.common import require, valid_qid
from scripts.wikimedia.contract import known_values

# MARK: - Exact Parent IDs

def known_link_ids(metadata, fields):
    """Берёт только truthy item ID из выбранных source свойств с сохранённым rank."""
    return {value["id"] for field in fields for value in known_values(metadata, field)
            if isinstance(value, dict) and valid_qid(value.get("id"))}


def parent_link_ids(metadata):
    """Возвращает прямые P179/P361 связи родителя, независимо от того, назван ли он сезоном в source."""
    return known_link_ids(metadata, ("series", "partOf"))


def related_parent_candidates(metadata):
    """Ограничивает обход direct/P4908/одним промежуточным родителем, как в заявленном inventory."""
    result = parent_link_ids(metadata)
    if metadata["kind"] == "episode":
        result |= known_link_ids(metadata, ("season",))
    return result


def confirmed_work_ids(metadata, known_shows, parent_links):
    """Сопоставляет только точные ID известных series/miniseries и один подтверждённый parent hop."""
    require(metadata.get("kind") in {"season", "episode"} and isinstance(known_shows, (set, frozenset)),
            "invalid_relation_projection_input")
    direct = parent_link_ids(metadata)
    works = direct & known_shows
    if metadata["kind"] == "episode":
        for parent in related_parent_candidates(metadata):
            works |= set(parent_links.get(parent, ())) & known_shows
    return works


# MARK: - Verified Related Projection

def confirmed_related_document(metadata, works, previous=None):
    """Разрешает публичную проекцию лишь после подтверждения works и без перепаковки равной source revision."""
    require(bool(works) and all(valid_qid(qid) for qid in works), "related_work_links_unverified")
    result = {**metadata, "source": dict(metadata["source"]), "works": {qid: True for qid in sorted(works)}}
    if previous is not None:
        require(result["source"]["revision"] >= previous["source"]["revision"], "related_source_revision_regressed")
        if result["source"]["revision"] == previous["source"]["revision"]:
            result["source"]["retrievedAt"] = previous["source"]["retrievedAt"]
            expected = {**previous, "works": result["works"], "kind": result["kind"],
                        "relationQuerySHA256": result["relationQuerySHA256"]}
            require(expected == result, "related_same_revision_payload_changed")
    validate_asset(result)
    return result
