"""The device cluster-level restore (decision 11) — full lifecycle.

Scenario: a live cluster with real migrations, a user and an ENCRYPTED
customer field is backed up, then destroyed; the safe restore path builds a
staging cluster with the runtime contract, verifies schema + counts + a real
encrypted-field decryption, and only then promotes (with a rollback point).

Requires PostgreSQL server binaries (initdb/pg_ctl) — CI installs them; dev
machines without PG self-skip. Sync test: restore_to_cluster drives asyncio.
"""

import asyncio
import io
import json
import os
import shutil
import tarfile
from pathlib import Path

import asyncpg
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from pharmaos_api import pg_bin
from pharmaos_api.migrations_runner import run_migrations
from pharmaos_api.security import crypto, keystore
from pharmaos_api.services import backup_service

_DB_PASSWORD = "R3store!Test#42"
_LIVE_PORT = 55432


def _pg_server_available() -> bool:
    if shutil.which("initdb"):
        return True
    env = os.environ.get("PG_BIN_DIR")
    return bool(env) and (Path(env) / "initdb.exe").is_file()


pytestmark = pytest.mark.skipif(
    not _pg_server_available(),
    reason="needs PostgreSQL server binaries (initdb/pg_ctl on PATH or PG_BIN_DIR)",
)


def _dsn(db: str) -> str:
    return backup_service._dsn("pharmaos", _DB_PASSWORD, _LIVE_PORT, db)


def _run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


async def _seed_live(dsn: str) -> None:
    """Real schema + one user + one encrypted customer (the decrypt gate)."""
    conn = await asyncpg.connect(dsn)
    try:
        role_id = await conn.fetchval(
            "INSERT INTO roles (code, name_ar, is_system) "
            "VALUES ('super_admin', 'مالك النظام', TRUE) "
            "ON CONFLICT (code) DO UPDATE SET name_ar = EXCLUDED.name_ar RETURNING id"
        )
        await conn.execute(
            "INSERT INTO users (username, full_name, password_hash, role_id) "
            "VALUES ('restore-admin', 'مدير الاستعادة', 'x', $1)",
            role_id,
        )
        await conn.execute(
            "INSERT INTO customers (name, national_id_encrypted, is_active) "
            "VALUES ('عميل الاستعادة', $1, TRUE)",
            crypto.encrypt_field("29801011234567", context="customers.national_id"),
        )
    finally:
        await conn.close()


async def _fetch_proof(dsn: str) -> tuple[int, str | None]:
    conn = await asyncpg.connect(dsn)
    try:
        users = int(await conn.fetchval("SELECT COUNT(*) FROM users"))
        encrypted = await conn.fetchval(
            "SELECT national_id_encrypted FROM customers "
            "WHERE national_id_encrypted IS NOT NULL LIMIT 1"
        )
        plaintext = (
            crypto.decrypt_field(bytes(encrypted), context="customers.national_id")
            if encrypted is not None
            else None
        )
        return users, plaintext
    finally:
        await conn.close()


def test_full_cluster_restore_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pharmaos_api.services.backup_service import (
        _init_staging,
        _pg_ctl,
        _pg_ctl_start,
        _wait_ready,
    )

    # The keystore DB password the first-run wizard would have stored —
    # create_backup bundles it (keys.json) and initdb uses it (SCRAM).
    monkeypatch.setattr(keystore, "get_db_password", lambda: _DB_PASSWORD)

    live_dir = tmp_path / "pgdata"
    live_dir.mkdir()
    try:
        _init_staging(live_dir, db_user="pharmaos", db_password=_DB_PASSWORD, port=_LIVE_PORT)
        _pg_ctl_start(live_dir, tmp_path / "pg-live.log")
        _wait_ready(_LIVE_PORT)

        async def _create_live_db() -> None:
            conn = await asyncpg.connect(_dsn("postgres"))
            try:
                await conn.execute('CREATE DATABASE "pharmaos"')
            finally:
                await conn.close()

        _run(_create_live_db())
        run_migrations(_dsn("pharmaos"))
        _run(_seed_live(_dsn("pharmaos")))

        backup_file = backup_service.create_backup(tmp_path, database_url=_dsn("pharmaos"))
        exported_key = keystore.get_backup_key()
        assert exported_key is not None

        # Simulate a clean replacement device: only the owner's offline key is
        # available before restore. The encrypted backup must repopulate the
        # database password and application keys during promotion.
        for name in (
            keystore.BACKUP_KEY_NAME,
            keystore.BACKUP_KEY_IMPORTED_FLAG,
            keystore.BACKUP_KEY_EXPORTED_FLAG,
            keystore.DB_PASSWORD_NAME,
            keystore.JWT_PRIVATE_KEY_NAME,
            keystore.JWT_PUBLIC_KEY_NAME,
            keystore.ENCRYPTION_KEY_NAME,
            keystore.CLOCK_HMAC_KEY_NAME,
        ):
            keystore.delete_secret(name)
        backup_service.import_backup_key(exported_key.hex())
        assert keystore.backup_key_imported() is True

        # Disaster: the live cluster is destroyed (crash / dead disk).
        _pg_ctl(live_dir, "stop", "-m", "fast")
        shutil.rmtree(live_dir)
        live_dir.mkdir()  # the path exists but holds nothing — the restore owns it

        report = backup_service.restore_to_cluster(
            backup_file,
            pgdata_dir=live_dir,
            live_port=_LIVE_PORT,
        )

        verification = report["verification"]  # type: ignore[index]
        assert verification["users"] == 1  # type: ignore[index]
        assert verification["permissions"] == 44  # type: ignore[index]
        assert verification["encrypted_field_verified"] is True  # type: ignore[index]

        # The promoted cluster serves on the live port with the restored data,
        # and the encrypted field decrypts with the RESTORED key.
        users, national_id = _run(_fetch_proof(_dsn("pharmaos")))
        assert users == 1
        assert national_id == "29801011234567"

        # Rollback point removed after confirmed success.
        assert not (tmp_path / "pgdata.previous").exists()
        # Keystore now carries the restored secrets (same values in this test).
        assert keystore.get_db_password() == _DB_PASSWORD
    finally:
        backup_service._stop_cluster(live_dir, tolerate_not_running=True)


def _write_fake_backup(path: Path, key: bytes, meta: dict[str, object]) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        meta_bytes = json.dumps(meta).encode()
        info = tarfile.TarInfo("meta.json")
        info.size = len(meta_bytes)
        tar.addfile(info, io.BytesIO(meta_bytes))
    nonce = os.urandom(12)
    path.write_bytes(nonce + AESGCM(key).encrypt(nonce, buf.getvalue(), b"pharmaos.backup.v1"))


def test_wrong_major_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Compatibility gate (decision 9): a backup taken on a different PG major
    is rejected BEFORE anything touches a cluster."""
    key = os.urandom(32)
    monkeypatch.setattr(keystore, "get_backup_key", lambda: key)
    monkeypatch.setattr(keystore, "backup_key_imported", lambda: True)
    monkeypatch.setattr(pg_bin, "version_major", lambda _p: 17)
    monkeypatch.setattr(pg_bin, "resolve", lambda _t: Path("/usr/bin/true"))

    fake = tmp_path / f"old{backup_service.BACKUP_SUFFIX}"
    _write_fake_backup(
        fake,
        key,
        {"format": backup_service.BACKUP_FORMAT, "postgres_major": 16},
    )

    with pytest.raises(backup_service.BackupIncompatibleError, match="PostgreSQL 16"):
        backup_service.restore_to_cluster(fake, pgdata_dir=tmp_path / "pgdata")


def test_restore_never_generates_backup_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decision 10: with NO backup key in the keystore, restore fails loudly
    (pointing at import-key) instead of generating one that could never work."""
    monkeypatch.setattr(keystore, "get_backup_key", lambda: None)
    monkeypatch.setattr(pg_bin, "resolve", lambda _t: Path("/usr/bin/true"))
    fake = tmp_path / f"x{backup_service.BACKUP_SUFFIX}"
    fake.write_bytes(b"\x00" * 32)

    with pytest.raises(backup_service.BackupKeyMissingError, match="import-key"):
        backup_service.restore_to_cluster(fake, pgdata_dir=tmp_path / "pgdata")


def test_import_backup_key_validation() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        backup_service.import_backup_key("not-a-key")
    with pytest.raises(ValueError, match="32 bytes"):
        backup_service.import_backup_key("abc123")  # too short

    good = os.urandom(32).hex()
    backup_service.import_backup_key(good)
    assert keystore.backup_key_imported() is True
    assert keystore.get_backup_key() is not None

    # Leave the session keystore as we found it (no imported flag leaking
    # into other tests' error-message branches).
    keystore.delete_secret(keystore.BACKUP_KEY_IMPORTED_FLAG)
    keystore.delete_secret(keystore.BACKUP_KEY_EXPORTED_FLAG)


# ---------------------------------------------------------------------------
# Release-prep phase 2 — the MANDATORY restore drill with count comparison:
# back up a cluster holding a real sale, restore into a CLEAN cluster, and
# assert the restored record counts EXACTLY match the pre-disaster counts
# (items / invoices / users / audit log / movements / batches / customers).
# ---------------------------------------------------------------------------

_COUNT_TABLES = (
    "users",
    "branches",
    "medications",
    "medication_batches",
    "invoices",
    "invoice_items",
    "stock_movements",
    "audit_logs",
    "customers",
)


async def _count_all(dsn: str) -> dict[str, int]:
    conn = await asyncpg.connect(dsn)
    try:
        return {
            table: int(await conn.fetchval(f"SELECT COUNT(*) FROM {table}"))  # noqa: S608
            for table in _COUNT_TABLES
        }
    finally:
        await conn.close()


def test_restore_preserves_all_record_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pharmaos_api.services.backup_service import (
        _init_staging,
        _pg_ctl,
        _pg_ctl_start,
        _wait_ready,
    )

    monkeypatch.setattr(keystore, "get_db_password", lambda: _DB_PASSWORD)
    live_dir = tmp_path / "pgdata"
    live_dir.mkdir()
    try:
        _init_staging(live_dir, db_user="pharmaos", db_password=_DB_PASSWORD, port=_LIVE_PORT)
        _pg_ctl_start(live_dir, tmp_path / "pg-live.log")
        _wait_ready(_LIVE_PORT)

        async def _create_live_db() -> None:
            conn = await asyncpg.connect(_dsn("postgres"))
            try:
                await conn.execute('CREATE DATABASE "pharmaos"')
            finally:
                await conn.close()

        _run(_create_live_db())
        run_migrations(_dsn("pharmaos"))

        # Seed REAL business data through the REAL service paths: a branch,
        # a fictional medication with stock, a cashier, and one completed
        # 10-tablet sale (invoice + items + movements + audit rows).
        _run(_seed_live(_dsn("pharmaos")))
        # The service-layer seeding + sale run through the app's SQLAlchemy
        # engine — point the cached engine/settings at THIS throwaway cluster
        # for the duration (monkeypatch restores everything at teardown).
        import pharmaos_api.db as db_mod
        from pharmaos_api.config import get_settings

        monkeypatch.setenv("DATABASE_URL", _dsn("pharmaos"))
        monkeypatch.setattr(db_mod, "_engine", None)
        monkeypatch.setattr(db_mod, "_session_factory", None)
        get_settings.cache_clear()
        try:
            _run(_seed_sale(_dsn("pharmaos")))
        finally:
            # The cache refilled from the overridden env mid-test — empty it so
            # later tests rebuild from the restored environment, not from the
            # throwaway cluster (monkeypatch restores env + engine attrs).
            get_settings.cache_clear()

        before = _run(_count_all(_dsn("pharmaos")))
        assert before["invoices"] == 1 and before["invoice_items"] >= 1
        assert before["audit_logs"] >= 1

        backup_file = backup_service.create_backup(tmp_path, database_url=_dsn("pharmaos"))
        exported_key = keystore.get_backup_key()
        assert exported_key is not None

        # Clean replacement device: only the owner's offline key survives.
        for name in (
            keystore.BACKUP_KEY_NAME,
            keystore.BACKUP_KEY_IMPORTED_FLAG,
            keystore.BACKUP_KEY_EXPORTED_FLAG,
            keystore.DB_PASSWORD_NAME,
            keystore.JWT_PRIVATE_KEY_NAME,
            keystore.JWT_PUBLIC_KEY_NAME,
            keystore.ENCRYPTION_KEY_NAME,
            keystore.CLOCK_HMAC_KEY_NAME,
        ):
            keystore.delete_secret(name)
        backup_service.import_backup_key(exported_key.hex())

        # Disaster: live cluster destroyed, restore into the empty path.
        _pg_ctl(live_dir, "stop", "-m", "fast")
        shutil.rmtree(live_dir)
        live_dir.mkdir()
        backup_service.restore_to_cluster(backup_file, pgdata_dir=live_dir, live_port=_LIVE_PORT)

        after = _run(_count_all(_dsn("pharmaos")))
        assert after == before, f"restored counts differ: {before} -> {after}"
    finally:
        backup_service._stop_cluster(live_dir, tolerate_not_running=True)
        # restore_to_cluster itself calls get_settings() while the env is
        # still overridden — clear the cache once more so later tests rebuild
        # from the restored environment.
        get_settings.cache_clear()


async def _seed_sale(dsn: str) -> None:
    """One real sale through sales_service.create_sale (atomic path)."""
    import datetime as dt
    import uuid as uuid_mod
    from decimal import Decimal

    from sqlalchemy import text

    from pharmaos_api.db import get_session_factory
    from pharmaos_api.models import (
        Branch,
        Medication,
        MedicationBarcode,
        MedicationPackaging,
        User,
    )
    from pharmaos_api.security.passwords import hash_password
    from pharmaos_api.services.inventory_service import receive_batch
    from pharmaos_api.services.sales_service import SaleLine, create_sale

    factory = get_session_factory()
    async with factory() as session:
        role_id = (
            await session.execute(
                text(
                    "INSERT INTO roles (code, name_ar, is_system) "
                    "VALUES ('cashier', 'كاشير', TRUE) "
                    "ON CONFLICT (code) DO UPDATE SET name_ar = EXCLUDED.name_ar RETURNING id"
                )
            )
        ).scalar_one()
        cashier = User(
            username=f"cashier-{uuid_mod.uuid4().hex[:8]}",
            full_name="كاشير الاستعادة",
            password_hash=hash_password("R3store!Test#42"),
            role_id=role_id,
        )
        session.add(cashier)
        branch = Branch(
            name=f"فرع مقارنة {uuid_mod.uuid4().hex[:6]}", country_code="EG", currency_code="EGP"
        )
        session.add(branch)
        await session.flush()

        unit_id = (
            await session.execute(
                text(
                    "INSERT INTO units (name_ar) VALUES ('شريط') "
                    "ON CONFLICT (name_ar) DO UPDATE SET name_ar=EXCLUDED.name_ar RETURNING id"
                )
            )
        ).scalar_one()
        med = Medication(
            trade_name="GEN-TEST-RESTORE", trade_name_ar="دواء مقارنة الاستعادة (وهمي)"
        )
        session.add(med)
        await session.flush()
        strip = MedicationPackaging(
            medication_id=med.id,
            level=1,
            unit_id=unit_id,
            name_ar="شريط",
            qty_in_parent=Decimal(10),
            selling_price=Decimal("12.00"),
            is_default_sale=True,
        )
        session.add(strip)
        await session.flush()
        session.add(
            MedicationBarcode(
                medication_id=med.id,
                packaging_id=strip.id,
                barcode="6229900000999",
                is_primary=True,
            )
        )
        await session.commit()

        await receive_batch(
            session,
            actor=cashier,
            branch_id=branch.id,
            medication_id=med.id,
            batch_number=f"RESTORE-{uuid_mod.uuid4().hex[:6]}",
            expiry_date=dt.date.today() + dt.timedelta(days=720),
            quantity=Decimal(1_000),
            purchase_price=Decimal("5.00"),
        )
        await session.commit()

        await create_sale(
            session,
            branch_id=branch.id,
            lines=[SaleLine(barcode="6229900000999", quantity=Decimal(1))],
            cashier=cashier,
        )
