# Created by d.kotina on 24.11.2026
"""Измеряемые неизменяемые блоки с ограниченной распаковкой и полной проверкой SHA256."""

import base64
import binascii
import hashlib
import json
import zlib

from scripts.wikimedia.common import canonical_bytes, fingerprint, require
from scripts.wikimedia.contract import validate_record

# MARK: - Limits

MAXIMUM_DECODED_BYTES = 1024 * 1024
MAXIMUM_ENVELOPE_BYTES = 256 * 1024
MAXIMUM_RECORDS = 64


# MARK: - Codec

def encode_block(records):
    """Упаковывает небольшой QID-map; хэш относится к точному envelope, а не к сетевому gzip."""
    return encode_document_block(records, validate_record)


def encode_document_block(records, validator, identity_field="qid"):
    """Применяет тот же bounded codec к явно проверенным записям дополнительных коллекций."""
    require(isinstance(records, dict) and 0 < len(records) <= MAXIMUM_RECORDS, "invalid_block_count")
    for qid, record in records.items():
        validator(record)
        require(qid == record[identity_field], "block_qid_mismatch")
    decoded = canonical_bytes(records)
    require(len(decoded) <= MAXIMUM_DECODED_BYTES, "decoded_block_too_large")
    compressed = zlib.compress(decoded, level=9)
    envelope = {"schemaVersion": 1, "encoding": "zlib-base64", "recordCount": len(records),
                "decodedBytes": len(decoded), "decodedSHA256": hashlib.sha256(decoded).hexdigest(),
                "data": base64.b64encode(compressed).decode("ascii")}
    require(len(canonical_bytes(envelope)) <= MAXIMUM_ENVELOPE_BYTES, "encoded_block_too_large")
    return fingerprint(envelope), envelope


def decode_block(expected_hash, envelope):
    """Проверяет envelope до bounded zlib, затем размер/хэш/JSON и каждую карточку."""
    return decode_document_block(expected_hash, envelope, validate_record)


def decode_document_block(expected_hash, envelope, validator, identity_field="qid"):
    """Распаковывает отдельную типизированную коллекцию без ослабления границ codec."""
    require(len(canonical_bytes(envelope)) <= MAXIMUM_ENVELOPE_BYTES
            and fingerprint(envelope) == expected_hash, "block_checksum_mismatch")
    require(set(envelope) == {"schemaVersion", "encoding", "recordCount", "decodedBytes", "decodedSHA256", "data"}
            and type(envelope.get("schemaVersion")) is int and envelope["schemaVersion"] == 1 and envelope.get("encoding") == "zlib-base64"
            and type(envelope.get("decodedBytes")) is int
            and 0 < envelope["decodedBytes"] <= MAXIMUM_DECODED_BYTES
            and type(envelope.get("recordCount")) is int
            and 0 < envelope["recordCount"] <= MAXIMUM_RECORDS, "invalid_block_header")
    try:
        compressed = base64.b64decode(envelope["data"], validate=True)
        decoder = zlib.decompressobj()
        decoded = decoder.decompress(compressed, envelope["decodedBytes"] + 1)
        require(len(decoded) == envelope["decodedBytes"] and decoder.eof
                and not decoder.unconsumed_tail and not decoder.unused_data, "invalid_compressed_block")
        require(hashlib.sha256(decoded).hexdigest() == envelope.get("decodedSHA256"), "decoded_checksum_mismatch")
        records = json.loads(decoded)
    except (ValueError, KeyError, TypeError, binascii.Error, zlib.error):
        require(False, "invalid_compressed_block")
    require(isinstance(records, dict) and len(records) == envelope["recordCount"], "block_count_mismatch")
    for qid, record in records.items():
        validator(record)
        require(qid == record[identity_field], "block_qid_mismatch")
    require(canonical_bytes(records) == decoded, "noncanonical_block")
    return records


# MARK: - Measurement

def block_measurements(records):
    """Сравнивает raw и упакованный JSON по одинаковому набору, включая envelope/base64 overhead."""
    identifiers = sorted(records)
    result = {"rawRecordBytes": len(canonical_bytes(records)), "encodedBlockBytes": 0,
              "blockCount": 0, "maximumEncodedBlockBytes": 0, "roundTripVerified": True}
    for offset in range(0, len(identifiers), MAXIMUM_RECORDS):
        selected = {qid: records[qid] for qid in identifiers[offset:offset + MAXIMUM_RECORDS]}
        digest, envelope = encode_block(selected)
        require(decode_block(digest, envelope) == selected, "block_roundtrip_failed")
        encoded_bytes = len(canonical_bytes(envelope))
        result["encodedBlockBytes"] += encoded_bytes
        result["maximumEncodedBlockBytes"] = max(result["maximumEncodedBlockBytes"], encoded_bytes)
        result["blockCount"] += 1
    result["encodedBytesPerRecord"] = round(result["encodedBlockBytes"] / len(records), 2)
    result["publicationAllowed"] = False
    result["limitations"] = "sample block packing; actual full-scope shards, search, lookup, episode/text/media and state are not measured"
    return result
