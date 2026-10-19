"""Backup subsystem: encrypted at rest, keys bundle included, restore drill
into a scratch DB succeeds, unconfigured cloud is a clean no-op."""

import os
import subprocess
from pathlib import Path

import asyncpg
import pytest

from pharmaos_api.services import backup_service


@pytest.fixture
def drill_db_url() -> str:
    """A disposable database for the restore drill."""
    base = os.environ["DATABASE_URL"]  # ...:5433/pharmaos_test
    admin_url = base.rsplit("/", 1)[0] + "/postgres"
    drill_url = base.rsplit("/", 1)[0] + "/pharmaos_drill"
    subprocess.run(
        ["psql", admin_url, "-qc", "DROP DATABASE IF EXISTS pharmaos_drill;"], check=True
    )
    subprocess.run(["psql", admin_url, "-qc", "CREATE DATABASE pharmaos_drill;"], check=True)
    return drill_url


async def test_backup_roundtrip_and_restore_drill(
    tmp_path: Path, seeded_user: dict, drill_db_url: str
) -> None:
    backup_file = backup_service.create_backup(tmp_path)
    assert backup_file.suffix == backup_service.BACKUP_SUFFIX.lstrip(".") or str(
        backup_file
    ).endswith(backup_service.BACKUP_SUFFIX)

    raw = backup_file.read_bytes()
    # Encrypted at rest: no pg_dump magic ("PGDMP") and no obvious plaintext.
    assert b"PGDMP" not in raw
    assert seeded_user["username"].encode() not in raw
    assert b"jwt_private_key_pem" not in raw

    members = backup_service.decrypt_backup(backup_file)
    assert {"db.dump", "keys.json", "meta.json"} <= set(members)
    assert members["db.dump"].startswith(b"PGDMP")

    counts = backup_service.restore_drill(backup_file, drill_database_url=drill_db_url)
    assert counts["users"] >= 1  # the seeded user made it through the drill
    assert counts["permissions"] == 44  # P3-M6 alerts.* + P3-M7 notifications.*

    # The drill is a recovery test, not only a decrypt test: every business
    # count used by the release runbook must match the source snapshot.
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        source = {
            "users": int(await conn.fetchval("SELECT COUNT(*) FROM users")),
            "medications": int(await conn.fetchval("SELECT COUNT(*) FROM medications")),
            "invoices": int(await conn.fetchval("SELECT COUNT(*) FROM invoices")),
            "audit_logs": int(await conn.fetchval("SELECT COUNT(*) FROM audit_logs")),
        }
    finally:
        await conn.close()
    assert {key: counts[key] for key in source} == source


def test_tampered_backup_rejected(tmp_path: Path) -> None:
    backup_file = backup_service.create_backup(tmp_path)
    blob = bytearray(backup_file.read_bytes())
    blob[-1] ^= 0x01
    backup_file.write_bytes(bytes(blob))
    with pytest.raises(Exception, match=".*"):
        backup_service.decrypt_backup(backup_file)


def test_cloud_upload_noop_when_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)
    monkeypatch.delenv("BACKUP_CLOUD_BUCKET", raising=False)
    backup_file = backup_service.create_backup(tmp_path)
    assert backup_service.upload_to_cloud(backup_file) is False


async def test_backup_checksum_sidecar_written_and_verifies(tmp_path: Path) -> None:
    import hashlib

    backup_file = backup_service.create_backup(tmp_path)
    sidecar = backup_file.with_name(backup_file.name + backup_service.CHECKSUM_SUFFIX)
    assert sidecar.is_file()
    expected = hashlib.sha256(backup_file.read_bytes()).hexdigest()
    assert sidecar.read_text(encoding="ascii").startswith(expected)
    assert backup_service.verify_checksum(backup_file) is True
    # corruption flips the verdict
    backup_file.write_bytes(backup_file.read_bytes()[:-1] + b"\x00")
    assert backup_service.verify_checksum(backup_file) is False


def test_retention_keeps_newest_and_removes_sidecars(tmp_path: Path) -> None:
    names = [f"pharmaos_2026010{i}T000000Z{backup_service.BACKUP_SUFFIX}" for i in range(1, 6)]
    for i, name in enumerate(names):
        path = tmp_path / name
        path.write_bytes(b"x" * (10 + i))
        path.with_name(path.name + backup_service.CHECKSUM_SUFFIX).write_text("deadbeef\n")
        os.utime(path, (1_700_000_000 + i * 3600, 1_700_000_000 + i * 3600))

    deleted = backup_service.prune_old_backups(tmp_path, keep=2)

    remaining = sorted(p.name for p in tmp_path.glob(f"*{backup_service.BACKUP_SUFFIX}"))
    assert remaining == names[-2:]
    assert [p.name for p in deleted] == names[:3]
    # sidecars of the pruned copies are gone too; kept ones remain
    assert not (tmp_path / (names[0] + backup_service.CHECKSUM_SUFFIX)).exists()
    assert not (tmp_path / (names[2] + backup_service.CHECKSUM_SUFFIX)).exists()
    assert (tmp_path / (names[3] + backup_service.CHECKSUM_SUFFIX)).is_file()


def test_retention_refuses_to_keep_zero(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        backup_service.prune_old_backups(tmp_path, keep=0)


def test_secondary_copy_is_atomic_and_checksum_verified(tmp_path: Path) -> None:
    source = tmp_path / "primary" / f"one{backup_service.BACKUP_SUFFIX}"
    secondary = tmp_path / "secondary"
    source.parent.mkdir()
    source.write_bytes(b"fictional backup bytes")

    copied = backup_service.copy_to_secondary(source, secondary)

    assert copied.read_bytes() == source.read_bytes()
    assert backup_service.verify_checksum(copied) is True
    assert not list(secondary.glob("*.tmp"))
