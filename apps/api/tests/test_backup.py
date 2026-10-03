"""Backup subsystem: encrypted at rest, keys bundle included, restore drill
into a scratch DB succeeds, unconfigured cloud is a clean no-op."""

import os
import subprocess
from pathlib import Path

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
    assert counts["permissions"] == 40  # P2-M8 added prescriptions.*/controlled_substances.view


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
