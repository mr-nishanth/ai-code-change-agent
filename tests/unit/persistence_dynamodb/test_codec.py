"""Attribute encoding (DYNAMODB-DATA-MODEL.md section 3)."""

import uuid
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum

import pytest

from ai_code_change_agent.persistence.dynamodb import codec
from ai_code_change_agent.persistence.dynamodb.errors import (
    CorruptItemError,
    DocumentTooLargeError,
    PersistenceError,
)


class Colour(Enum):
    RED = "RED"


def test_timestamps_are_fixed_width_utc_and_sort_as_strings() -> None:
    whole = datetime(2026, 11, 27, 9, 29, 17, tzinfo=UTC)
    assert codec.encode_timestamp(whole) == "2026-11-27T09:29:17.000000Z"
    ist = timezone(timedelta(hours=5, minutes=30))
    local = datetime(2026, 11, 27, 14, 59, 17, 162000, tzinfo=ist)
    assert codec.encode_timestamp(local) == "2026-11-27T09:29:17.162000Z"
    times = [whole + timedelta(microseconds=n * 999_983) for n in range(50)]
    encoded = [codec.encode_timestamp(t) for t in times]
    assert sorted(encoded) == encoded
    assert {len(e) for e in encoded} == {27}
    assert codec.decode_timestamp(encoded[7]) == times[7]


def test_naive_timestamps_are_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        codec.encode_timestamp(datetime(2026, 1, 1))
    with pytest.raises(ValueError, match="timezone-aware"):
        codec.encode_epoch_seconds(datetime(2026, 1, 1))
    assert codec.encode_epoch_seconds(datetime(1970, 1, 2, tzinfo=UTC)) == 86_400


@pytest.mark.parametrize(
    "value",
    ["2026-11-27T09:29:17Z", "2026-11-27 09:29:17.000000Z", 5, "2026-13-27T09:29:17.000000Z"],
)
def test_malformed_timestamps_are_corrupt(value: object) -> None:
    with pytest.raises(CorruptItemError) as raised:
        codec.decode_timestamp(value, "created_at")
    assert str(raised.value) == "corrupt_item:created_at"


def test_json_is_compact_and_keeps_key_order_and_unicode() -> None:
    document = {"b": [1, 2.5, None], "a": {"name": "Zoë"}}
    text = codec.encode_json(document)
    assert text == '{"b":[1,2.5,null],"a":{"name":"Zoë"}}'
    assert codec.decode_json(text) == document
    with pytest.raises(ValueError):
        codec.encode_json({"x": float("nan")})
    with pytest.raises(CorruptItemError):
        codec.decode_json("{not json", "content")
    with pytest.raises(CorruptItemError):
        codec.decode_json(None, "content")


def test_json_documents_over_300_kb_are_refused_with_a_domain_error() -> None:
    limit = codec.MAX_JSON_DOCUMENT_BYTES
    assert limit == 300 * 1024
    overhead = len('{"x":""}')
    assert len(codec.encode_json({"x": "a" * (limit - overhead)})) == limit
    with pytest.raises(DocumentTooLargeError) as raised:
        codec.encode_json({"x": "a" * (limit - overhead + 1)})
    assert isinstance(raised.value, PersistenceError)
    assert str(raised.value) == "document_too_large"
    # bytes, not characters: two-byte characters count twice
    with pytest.raises(DocumentTooLargeError):
        codec.encode_json({"x": "é" * (limit // 2)})


def test_decimals_are_plain_strings() -> None:
    assert codec.encode_decimal(Decimal("1E-7")) == "0.0000001"
    assert codec.encode_decimal(Decimal("12.3400")) == "12.3400"
    assert codec.decode_decimal("0.0000001") == Decimal("0.0000001")
    for bad in (Decimal("Infinity"), Decimal("NaN")):
        with pytest.raises(ValueError):
            codec.encode_decimal(bad)
    for stored in ("abc", "Infinity", 1):
        with pytest.raises(CorruptItemError):
            codec.decode_decimal(stored, "cost_usd")


def test_numbers_enums_and_uuids_are_validated_on_read() -> None:
    assert codec.decode_int(Decimal("3")) == 3
    assert codec.decode_int(7) == 7
    for bad in (Decimal("3.5"), Decimal("NaN"), True, "3", None):
        with pytest.raises(CorruptItemError):
            codec.decode_int(bad, "row_version")
    assert codec.decode_enum(Colour, "RED") is Colour.RED
    with pytest.raises(CorruptItemError) as raised:
        codec.decode_enum(Colour, "PURPLE", "status")
    assert "PURPLE" not in str(raised.value)
    rid = uuid.uuid4()
    assert codec.encode_uuid(str(rid).upper()) == str(rid)
    assert codec.decode_uuid(str(rid)) == rid
    for bad_id in ("nope", 5):
        with pytest.raises(CorruptItemError):
            codec.decode_uuid(bad_id)
    with pytest.raises(ValueError):
        codec.encode_uuid("nope")
