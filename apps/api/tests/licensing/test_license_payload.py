"""Container/payload contract tests (P4 §2) — every rejection path of
parse_license_file, the canonical-JSON rules, and the numeric activation rank."""

import base64
from datetime import UTC, datetime, timedelta

import pytest

from pharmaos_api.errors import ErrorCode
from pharmaos_api.licensing.canonical import (
    DuplicateKeyError,
    canonical_json_bytes,
    loads_strict,
)
from pharmaos_api.licensing.errors import LicensingError
from pharmaos_api.licensing.payload import (
    LicensePayloadV1,
    parse_license_file,
    sign_payload,
)
from tests.licensing.helpers import TEST_HWID, make_license, make_vendor_key


def _now() -> datetime:
    return datetime.now(UTC)


def test_canonical_json_contract() -> None:
    # sorted keys, compact separators, UTF-8 preserved (ensure_ascii=False)
    assert canonical_json_bytes({"b": 2, "a": "عربي"}) == '{"a":"عربي","b":2}'.encode()


def test_duplicate_keys_rejected() -> None:
    with pytest.raises(DuplicateKeyError):
        loads_strict(b'{"a": 1, "a": 2}')


def test_container_roundtrip() -> None:
    key = make_vendor_key()
    raw = make_license(key, license_id="LIC-2026-000001")
    container = parse_license_file(
        raw, public_key=key.public_key(), accepted_kids=frozenset({"testkid01"})
    )
    assert container.payload.license_id == "LIC-2026-000001"
    assert container.payload.hwid == TEST_HWID
    assert container.payload.schema_version == 1


def test_tampered_payload_rejected() -> None:
    key = make_vendor_key()
    raw = make_license(key, license_id="LIC-2026-000001")
    document = loads_strict(raw)
    document["payload"]["customer"] = "صيدلية مزوّرة"
    forged = canonical_json_bytes(document)
    with pytest.raises(LicensingError) as excinfo:
        parse_license_file(
            forged, public_key=key.public_key(), accepted_kids=frozenset({"testkid01"})
        )
    assert excinfo.value.code == ErrorCode.LICENSE_INVALID_SIGNATURE
    assert excinfo.value.reason == "bad_signature"


def test_wrong_key_rejected() -> None:
    signer = make_vendor_key()
    verifier = make_vendor_key()
    raw = make_license(signer, license_id="LIC-2026-000002")
    with pytest.raises(LicensingError) as excinfo:
        parse_license_file(
            raw, public_key=verifier.public_key(), accepted_kids=frozenset({"testkid01"})
        )
    assert excinfo.value.reason == "bad_signature"


def test_extra_top_level_field_rejected() -> None:
    key = make_vendor_key()
    raw = make_license(key, license_id="LIC-2026-000003")
    document = loads_strict(raw)
    document["extra"] = True
    with pytest.raises(LicensingError) as excinfo:
        parse_license_file(
            canonical_json_bytes(document),
            public_key=key.public_key(),
            accepted_kids=frozenset({"testkid01"}),
        )
    assert excinfo.value.reason == "unknown_fields"


def test_missing_field_rejected() -> None:
    key = make_vendor_key()
    raw = make_license(key, license_id="LIC-2026-000004")
    document = loads_strict(raw)
    del document["payload"]["hwid"]
    with pytest.raises(LicensingError) as excinfo:
        parse_license_file(
            canonical_json_bytes(document),
            public_key=key.public_key(),
            accepted_kids=frozenset({"testkid01"}),
        )
    assert excinfo.value.reason == "schema_mismatch"


def test_wrong_format_rejected() -> None:
    key = make_vendor_key()
    raw = make_license(key, license_id="LIC-2026-000005")
    document = loads_strict(raw)
    document["format"] = "other-license-v1"
    with pytest.raises(LicensingError) as excinfo:
        parse_license_file(
            canonical_json_bytes(document),
            public_key=key.public_key(),
            accepted_kids=frozenset({"testkid01"}),
        )
    assert excinfo.value.reason == "schema_mismatch"


def test_unknown_kid_rejected() -> None:
    key = make_vendor_key()
    raw = make_license(key, license_id="LIC-2026-000006")
    with pytest.raises(LicensingError) as excinfo:
        parse_license_file(raw, public_key=key.public_key(), accepted_kids=frozenset({"otherkid"}))
    assert excinfo.value.reason == "schema_mismatch"


def test_short_signature_rejected() -> None:
    key = make_vendor_key()
    raw = make_license(key, license_id="LIC-2026-000007")
    document = loads_strict(raw)
    signature = base64.b64decode(document["signature"], validate=True)
    document["signature"] = base64.b64encode(signature[:63]).decode("ascii")
    with pytest.raises(LicensingError) as excinfo:
        parse_license_file(
            canonical_json_bytes(document),
            public_key=key.public_key(),
            accepted_kids=frozenset({"testkid01"}),
        )
    assert excinfo.value.reason == "bad_signature"


def test_naive_timestamp_rejected() -> None:
    now = _now()
    with pytest.raises(Exception):  # noqa: B017  # pydantic.ValidationError expected
        LicensePayloadV1(
            schema_version=1,
            license_id="LIC-2026-000008",
            customer="x",
            hwid=TEST_HWID,
            issued_at=datetime(2026, 1, 1),  # naive
            valid_until=now + timedelta(days=1),
            kind="subscription",
        )


def test_valid_until_before_issued_rejected() -> None:
    now = _now()
    with pytest.raises(Exception):  # noqa: B017
        LicensePayloadV1(
            schema_version=1,
            license_id="LIC-2026-000009",
            customer="x",
            hwid=TEST_HWID,
            issued_at=now,
            valid_until=now - timedelta(days=1),
            kind="subscription",
        )


def test_unknown_feature_rejected() -> None:
    now = _now()
    with pytest.raises(Exception):  # noqa: B017
        LicensePayloadV1(
            schema_version=1,
            license_id="LIC-2026-000010",
            customer="x",
            hwid=TEST_HWID,
            issued_at=now,
            valid_until=now + timedelta(days=1),
            kind="subscription",
            features=["cloud_sync"],  # not in the v1 closed registry
        )


def test_file_too_large_rejected() -> None:
    key = make_vendor_key()
    raw = make_license(key, license_id="LIC-2026-000011") + b" " * (64 * 1024 + 1)
    with pytest.raises(LicensingError) as excinfo:
        parse_license_file(raw, public_key=key.public_key(), accepted_kids=frozenset({"testkid01"}))
    assert excinfo.value.reason == "file_too_large"


def test_activation_rank_is_numeric() -> None:
    now = _now()
    low = LicensePayloadV1(
        schema_version=1,
        license_id="LIC-2026-100000",
        customer="x",
        hwid=TEST_HWID,
        issued_at=now,
        valid_until=now + timedelta(days=1),
        kind="subscription",
    )
    high = LicensePayloadV1(
        schema_version=1,
        license_id="LIC-2026-99999",
        customer="x",
        hwid=TEST_HWID,
        issued_at=now,
        valid_until=now + timedelta(days=1),
        kind="subscription",
    )
    # numeric compare: 99999 < 100000 — the string compare would say otherwise
    assert low.activation_rank() > high.activation_rank()


def test_sign_payload_roundtrip() -> None:
    key = make_vendor_key()
    now = _now()
    payload = LicensePayloadV1(
        schema_version=1,
        license_id="LIC-2026-000012",
        customer="x",
        hwid=TEST_HWID,
        issued_at=now,
        valid_until=now + timedelta(days=1),
        kind="subscription",
    )
    raw = sign_payload(payload, private_key=key, kid="testkid01")
    container = parse_license_file(
        raw, public_key=key.public_key(), accepted_kids=frozenset({"testkid01"})
    )
    assert container.payload.license_id == "LIC-2026-000012"
