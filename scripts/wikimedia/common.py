# Created by d.kotina on 24.11.2026
"""Общие ограничения, канонический JSON и надёжная локальная запись."""

import hashlib
import json
import os
import re
import tempfile
import urllib.request
from pathlib import Path

# MARK: - Configuration

SCHEMA_VERSION = 1
PUBLIC_ROOT = "/catalog/wikimedia/v1"
STATE_ROOT = "/catalog-wikimedia-state/v1"
RESTORE_ROOT = "/catalog-wikimedia-validation/v1"
ROOT_CLASSES = {"film": "Q11424", "series": "Q5398426", "miniseries": "Q1259759"}
QID_PATTERN = re.compile(r"Q[1-9][0-9]{0,19}\Z")
WIKIBASE_IDENTIFIER_PATTERN = re.compile(r"(?:[QP][1-9][0-9]{0,19}|L[1-9][0-9]{0,19}(?:-[FS][1-9][0-9]{0,19})?)\Z")
HASH_PATTERN = re.compile(r"[a-f0-9]{64}\Z")
USER_AGENT = "PosleTitrovCatalog/0.1 (https://github.com/typefoxes/posle-titrov-automation)"
DATABASE_ADDRESS = "https://posletitrov-ios-default-rtdb.europe-west1.firebasedatabase.app"
LOCAL_DISK_RESERVE_BYTES = 4 * 1024**3


# MARK: - Redirect Policy

class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Не пересылает заголовки или учётные данные на другой HTTP адрес."""

    def redirect_request(self, request, file_pointer, code, message, headers, new_address):
        """Отклоняет любой redirect до повторной отправки запроса."""
        return None


# MARK: - Validation

class CatalogError(Exception):
    """Передаёт только безопасный код ошибки без сетевых ответов и секретов."""


def require(condition, code):
    """Останавливает операцию при нарушении проверяемого контракта."""
    if not condition:
        raise CatalogError(code)


def valid_qid(value):
    """Проверяет канонический QID без преобразования в числовой ID другого провайдера."""
    return isinstance(value, str) and QID_PATTERN.fullmatch(value) is not None


def valid_wikibase_identifier(value):
    """Сохраняет item/property/lexeme/form/sense ID в qualifiers, не превращая их все в каталог QID."""
    return isinstance(value, str) and WIKIBASE_IDENTIFIER_PATTERN.fullmatch(value) is not None


def canonical_bytes(value):
    """Кодирует JSON детерминированно, запрещая NaN и неоднозначные числа."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def fingerprint(value):
    """Вычисляет полный SHA256 канонического документа."""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


# MARK: - Local Persistence

def atomic_json(path, value):
    """Записывает документ через fsync и атомарную замену только целевого файла."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical_bytes(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path):
    """Читает существующий локальный документ, не заменяя повреждённый пустым."""
    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream)
