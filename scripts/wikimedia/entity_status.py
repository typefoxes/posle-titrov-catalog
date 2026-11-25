# Created by d.kotina on 24.11.2026
"""Подтверждённые missing/redirect без выдуманной исходной revision или автоматического слияния ID."""

from scripts.wikimedia.common import require, valid_qid

# MARK: - Source Availability

def unavailable_entity_proof(qid, entity, retrieved_at):
    """Различает исходный QID и target redirect; возвращает только необходимое подтверждение ответа API."""
    require(valid_qid(qid) and isinstance(entity, dict)
            and isinstance(retrieved_at, str) and bool(retrieved_at), "invalid_entity_identity")
    if entity.get("id") == qid:
        require("redirects" not in entity, "invalid_entity_identity")
        if "missing" not in entity:
            return None
        return {"qid": qid, "status": "missing", "retrievedAt": retrieved_at,
                "endpoint": "https://www.wikidata.org/w/api.php", "entity": {"id": qid, "missing": entity["missing"]}}
    redirect = entity.get("redirects")
    require(isinstance(redirect, dict) and redirect == {"from": qid, "to": entity.get("id")}
            and valid_qid(entity.get("id")) and "missing" not in entity
            and type(entity.get("lastrevid")) is int and entity["lastrevid"] > 0, "invalid_entity_identity")
    return {"qid": qid, "status": "redirect", "target": entity["id"], "targetRevision": entity["lastrevid"],
            "retrievedAt": retrieved_at, "endpoint": "https://www.wikidata.org/w/api.php",
            "entity": {name: entity[name] for name in ("id", "lastrevid", "redirects")}}


def validate_unavailable_proof(proof):
    """Проверяет missing/redirect и API-подтверждённый выход из зафиксированной class closure."""
    require(isinstance(proof, dict) and valid_qid(proof.get("qid")), "invalid_unavailable_proof")
    if proof.get("status") != "outside_captured_scope":
        require(unavailable_entity_proof(proof["qid"], proof.get("entity"), proof.get("retrievedAt")) == proof,
                "source_missing_proof_mismatch")
        return
    from scripts.wikimedia.assets import validate_asset
    from scripts.wikimedia.common import HASH_PATTERN
    required = {"qid", "status", "source", "classClosureSHA256"}
    require(required <= set(proof) and set(proof) <= required | {"instanceOf"}
            and isinstance(proof.get("classClosureSHA256"), str)
            and HASH_PATTERN.fullmatch(proof["classClosureSHA256"]), "invalid_outside_scope_proof")
    validate_asset({"schemaVersion": 1, "qid": proof["qid"], "kind": "reference", "source": proof["source"]})
    if "instanceOf" in proof:
        require(isinstance(proof["instanceOf"], dict) and proof["instanceOf"], "invalid_outside_scope_classes")
