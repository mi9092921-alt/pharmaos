"""Cross-vector tests (P4 §2): the vendor CLI and the API verifier are two
implementations of ONE contract — these tests prove byte-identity and that a
CLI-issued file verifies through the API's parse path."""

import base64
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pharmaos_license_cli.canonical import canonical_json_bytes as cli_canonical
from pharmaos_license_cli.canonical import iso_z as cli_iso_z
from pharmaos_license_cli.keys import (
    KeyFileError,
    load_key_file,
    save_key_file,
)
from pharmaos_license_cli.ledger import append_entry, expiring_soon, issued_numbers, read_entries
from pharmaos_license_cli.payload import LicensePayloadV1 as CliPayload
from pharmaos_license_cli.payload import next_license_id, now_utc

api_payload = pytest.importorskip(
    "pharmaos_api.licensing.payload", reason="cross-vectors need the API package installed"
)


def _now() -> datetime:
    return datetime.now(UTC)


def _cli_payload(license_id: str = "LIC-2026-000042") -> CliPayload:
    now = _now()
    return CliPayload(
        schema_version=1,
        license_id=license_id,
        customer="صيدلية الاختبار",
        hwid="PHAR-AAAA-BBBB-CCCC-DDDD",
        issued_at=now,
        valid_until=now + timedelta(days=365),
        kind="subscription",
        features=["pos", "inventory"],
    )


def test_canonical_bytes_identical() -> None:
    cli_model = _cli_payload()
    api_model = api_payload.LicensePayloadV1.model_validate(cli_model.to_canonical_dict())
    assert cli_canonical(cli_model.to_canonical_dict()) == api_payload.canonical_json_bytes(
        api_model.to_canonical_dict()
    )


def test_iso_z_identical() -> None:
    from pharmaos_api.licensing.canonical import iso_z as api_iso_z

    moment = datetime(2026, 10, 5, 12, 30, 45, tzinfo=UTC)
    assert cli_iso_z(moment) == api_iso_z(moment) == "2026-10-05T12:30:45Z"


def test_cli_issued_file_verifies_through_api() -> None:
    private_key = Ed25519PrivateKey.generate()
    payload = _cli_payload()
    canonical = cli_canonical(payload.to_canonical_dict())
    signature = private_key.sign(canonical)
    document = {
        "format": "pharmaos-license-v1",
        "kid": "testkid01",
        "payload": payload.to_canonical_dict(),
        "signature": base64.b64encode(signature).decode("ascii"),
    }
    raw = cli_canonical(document)
    container = api_payload.parse_license_file(
        raw, public_key=private_key.public_key(), accepted_kids=frozenset({"testkid01"})
    )
    assert container.payload.license_id == "LIC-2026-000042"


def test_api_signed_file_parses_in_cli() -> None:
    private_key = Ed25519PrivateKey.generate()
    now = _now()
    api_model = api_payload.LicensePayloadV1(
        schema_version=1,
        license_id="LIC-2026-000043",
        customer="x",
        hwid="PHAR-AAAA-BBBB-CCCC-DDDD",
        issued_at=now,
        valid_until=now + timedelta(days=1),
        kind="subscription",
    )
    raw = api_payload.sign_payload(api_model, private_key=private_key, kid="testkid01")
    document = json.loads(raw)
    payload = CliPayload.model_validate(document["payload"])
    signature = base64.b64decode(document["signature"], validate=True)
    private_key.public_key().verify(signature, cli_canonical(payload.to_canonical_dict()))


# --- PLKEY1 key file -------------------------------------------------------------


def test_key_file_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "k.key"
    seed = os.urandom(32)
    save_key_file(path, kid="abcd1234", private_seed=seed, passphrase=b"long-passphrase")
    kid, loaded = load_key_file(path, passphrase=b"long-passphrase")
    assert kid == "abcd1234"
    assert loaded == seed


def test_key_file_wrong_passphrase(tmp_path: Path) -> None:
    path = tmp_path / "k.key"
    save_key_file(path, kid="abcd1234", private_seed=os.urandom(32), passphrase=b"long-passphrase")
    with pytest.raises(KeyFileError):
        load_key_file(path, passphrase=b"wrong-passphrase")


def test_key_file_magic(tmp_path: Path) -> None:
    path = tmp_path / "k.key"
    path.write_bytes(b"NOTKEY1" + os.urandom(64))
    with pytest.raises(KeyFileError):
        load_key_file(path, passphrase=b"x")


# --- ledger ------------------------------------------------------------------------


def test_ledger_roundtrip_and_numbering(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    entry = {
        "license_id": "LIC-2026-000001",
        "customer": "c",
        "hwid": "PHAR-AAAA-BBBB-CCCC-DDDD",
        "kind": "annual",
        "issued_at": "2026-10-05T00:00:00Z",
        "valid_until": "2027-10-05T00:00:00Z",
        "features": [],
        "file": "x.license",
        "kid": "abcd1234",
        "created_at": "2026-10-05T00:00:00Z",
    }
    append_entry(ledger, entry)
    assert issued_numbers(read_entries(ledger)) == [1]
    append_entry(ledger, {**entry, "license_id": "LIC-2026-000002"})
    # the next id continues from the largest issued number
    assert next_license_id(now_utc(), issued_numbers(read_entries(ledger))) == "LIC-2026-000003"


def test_expiring_soon_filters() -> None:
    now = _now()
    entries = [
        {"license_id": "LIC-2026-000001", "valid_until": (now + timedelta(days=5)).isoformat()},
        {"license_id": "LIC-2026-000002", "valid_until": (now + timedelta(days=90)).isoformat()},
    ]
    soon = expiring_soon(entries, 30)
    assert [e["license_id"] for e in soon] == ["LIC-2026-000001"]
