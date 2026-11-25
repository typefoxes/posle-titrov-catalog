# Created by d.kotina on 24.11.2026
"""Независимая JSON Schema проверка зафиксированных public/checkpoint контрактов без загрузки схем из сети."""

from functools import lru_cache
from pathlib import Path

from scripts.wikimedia.common import CatalogError, read_json, require

# MARK: - Offline Schema Registry

SCHEMA_NAMES = frozenset({"record", "asset", "block", "bundle", "index-row", "release", "pointer", "checkpoint", "relationship"})


@lru_cache(maxsize=9)
def schema_validator(name):
    """Компилирует одну известную схему; все references разрешаются только из локального реестра."""
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource
    require(name in SCHEMA_NAMES, "unknown_catalog_schema")
    directory = Path(__file__).resolve().parents[2] / "schemas"
    registry = Registry()
    schemas = {}
    for identifier in sorted(SCHEMA_NAMES):
        filename = "wikimedia-" + identifier + "-v1.schema.json"
        schema = read_json(directory / filename)
        Draft202012Validator.check_schema(schema)
        schemas[identifier] = schema
        resource = Resource.from_contents(schema)
        registry = registry.with_resource(schema["$id"], resource)
        registry = registry.with_resource("https://posle-titrov.local/schemas/" + filename, resource)
    return Draft202012Validator(schemas[name], registry=registry)


def validate_schema(name, document):
    """Отклоняет неизвестные поля/неполные roots, выводя только безопасное имя контракта."""
    from jsonschema.exceptions import ValidationError
    try:
        schema_validator(name).validate(document)
    except ValidationError:
        raise CatalogError("json_schema_invalid_" + name) from None
