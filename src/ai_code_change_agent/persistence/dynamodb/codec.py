"""Attribute encoding for the application table (DYNAMODB-DATA-MODEL.md section 3).

- Timestamps: UTC ISO-8601 with fixed microsecond width (`2026-11-27T09:29:17.162000Z`), so they
  sort as strings. Naive datetimes are refused.
- JSON documents: compact JSON strings (not maps, which would turn numbers into Decimal and could
  change the bytes behind a content hash). Documents over 300 KB are refused with
  `DocumentTooLargeError` before DynamoDB sees them (the item limit is 400 KB).
- Money: decimal strings in plain notation (never floats, never exponents).
- Enums and numbers are validated on read; a malformed stored value raises `CorruptItemError`, whose
  message names the attribute only, never the value."""

import json
import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from ai_code_change_agent.persistence.dynamodb.errors import CorruptItemError, DocumentTooLargeError

MAX_JSON_DOCUMENT_BYTES = 300 * 1024
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")


def encode_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def decode_timestamp(value: object, attribute: str = "timestamp") -> datetime:
    if not isinstance(value, str) or not _TIMESTAMP.match(value):
        raise CorruptItemError(attribute)
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError:
        raise CorruptItemError(attribute) from None


def encode_epoch_seconds(value: datetime) -> int:
    """For TTL attributes (`expires_at`, N)."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return int(value.timestamp())


def encode_json(document: Any) -> str:
    """Compact JSON (no whitespace, keys in the given order, UTF-8 kept)."""
    text = json.dumps(document, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    if len(text.encode("utf-8")) > MAX_JSON_DOCUMENT_BYTES:
        raise DocumentTooLargeError()
    return text


def decode_json(value: object, attribute: str = "document") -> Any:
    if not isinstance(value, str):
        raise CorruptItemError(attribute)
    try:
        return json.loads(value)
    except ValueError:
        raise CorruptItemError(attribute) from None


def encode_decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("decimal values must be finite")
    return format(value, "f")


def decode_decimal(value: object, attribute: str = "decimal") -> Decimal:
    if not isinstance(value, str):
        raise CorruptItemError(attribute)
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise CorruptItemError(attribute) from None
    if not result.is_finite():
        raise CorruptItemError(attribute)
    return result


def decode_int(value: object, attribute: str = "number") -> int:
    """boto3 returns DynamoDB numbers as Decimal; only integral values are accepted."""
    if isinstance(value, bool) or not isinstance(value, Decimal | int):
        raise CorruptItemError(attribute)
    if isinstance(value, Decimal) and (not value.is_finite() or value != value.to_integral_value()):
        raise CorruptItemError(attribute)
    return int(value)


def decode_enum[E: Enum](enum_type: type[E], value: object, attribute: str = "enum") -> E:
    try:
        return enum_type(value)
    except ValueError:
        raise CorruptItemError(attribute) from None


def encode_uuid(value: uuid.UUID | str) -> str:
    """Lowercase canonical form; a string must already be a UUID."""
    return str(value if isinstance(value, uuid.UUID) else uuid.UUID(value))


def decode_uuid(value: object, attribute: str = "id") -> uuid.UUID:
    if not isinstance(value, str):
        raise CorruptItemError(attribute)
    try:
        return uuid.UUID(value)
    except ValueError:
        raise CorruptItemError(attribute) from None
