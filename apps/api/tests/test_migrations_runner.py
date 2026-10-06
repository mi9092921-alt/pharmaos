"""The device `migrate` runner — exact bash-script semantics (decision 7).

Runs against a scratch database with the REAL repo migrations (no mocked
schema): filename order, one transaction per file, version tracking in
_pharmaos_migrations, seeds re-applied on EVERY run, and a failed migration
rolls back together with its version row.
"""

import asyncio
import os
import uuid
from pathlib import Path

import asyncpg
import pytest

from pharmaos_api.migrations_runner import (
    MigrationError,
    default_migrations_dir,
    default_seeds_dir,
    run_migrations,
)


@pytest.fixture
async def scratch_dsn() -> object:
    base = os.environ["DATABASE_URL"]  # ...:5432/pharmaos_test
    admin_dsn = base.rsplit("/", 1)[0] + "/postgres"
    dbname = f"pharmaos_migrate_{uuid.uuid4().hex[:8]}"
    dsn = base.rsplit("/", 1)[0] + f"/{dbname}"
    conn = await asyncpg.connect(admin_dsn)
    await conn.execute(f'CREATE DATABASE "{dbname}"')
    await conn.close()
    yield dsn
    conn = await asyncpg.connect(admin_dsn)
    await conn.execute(f'DROP DATABASE "{dbname}" WITH (FORCE)')
    await conn.close()


def test_repo_layout_resolution() -> None:
    migrations = default_migrations_dir()
    seeds = default_seeds_dir()
    assert migrations.name == "migrations"
    assert (migrations / "20260711003000_installation_state.sql").is_file()
    assert seeds.name == "seeds"
    assert (seeds / "rbac_seed.sql").is_file()


def test_full_migration_run_and_idempotent_second(scratch_dsn: object) -> None:
    migrations_dir = default_migrations_dir()
    total = len(list(migrations_dir.glob("*.sql")))

    first = run_migrations(str(scratch_dsn))
    assert len(first["applied"]) == total  # type: ignore[arg-type]
    assert first["skipped"] == []  # type: ignore[operator]
    assert "rbac_seed.sql" in first["seeds"]  # type: ignore[operator]
    # The newest migration's object exists (installation_state, decision 7).
    # (Verified through the versions table, not a connection, for simplicity.)

    second = run_migrations(str(scratch_dsn))
    assert second["applied"] == []  # type: ignore[operator]
    assert len(second["skipped"]) == total  # type: ignore[arg-type]
    # Seeds STILL re-apply on every run ("code always wins" — CLAUDE.md).
    assert "rbac_seed.sql" in second["seeds"]  # type: ignore[operator]


def test_installation_state_table_created(scratch_dsn: object) -> None:
    run_migrations(str(scratch_dsn))

    async def _check() -> dict[str, str]:
        conn = await asyncpg.connect(str(scratch_dsn))
        try:
            rows = await conn.fetch("SELECT key, value FROM installation_state ORDER BY key")
            return {r["key"]: r["value"] for r in rows}
        finally:
            await conn.close()

    state = asyncio.run(_check())
    assert state == {
        "last_completed_step": "none",
        "setup_complete": "0",
        "setup_version": "1",
    }


def test_failed_migration_rolls_back_with_version_row(scratch_dsn: object, tmp_path: Path) -> None:
    """One bad file must not leave a half-applied state or a tracked version;
    the next `migrate` invocation resumes exactly where this one stopped."""
    good = tmp_path / "20260101000100_create_alpha.sql"
    good.write_text("CREATE TABLE alpha (id int);", encoding="utf-8")
    bad = tmp_path / "20260101000200_break.sql"
    bad.write_text(
        "CREATE TABLE beta (id int); INSERT INTO no_such_table VALUES (1);", encoding="utf-8"
    )
    after = tmp_path / "20260101000300_create_gamma.sql"
    after.write_text("CREATE TABLE gamma (id int);", encoding="utf-8")

    with pytest.raises(MigrationError, match="20260101000200_break"):
        run_migrations(str(scratch_dsn), migrations_dir=tmp_path, seeds_dir=tmp_path)

    async def _tracked() -> list[str]:
        conn = await asyncpg.connect(str(scratch_dsn))
        try:
            rows = await conn.fetch("SELECT version FROM _pharmaos_migrations ORDER BY version")
            return [r["version"] for r in rows]
        finally:
            await conn.close()

    async def _exists(table: str) -> bool:
        conn = await asyncpg.connect(str(scratch_dsn))
        try:
            result = await conn.fetchval(f"SELECT to_regclass('{table}') IS NOT NULL")  # noqa: S608
            return bool(result)
        finally:
            await conn.close()

    assert asyncio.run(_exists("alpha"))  # good file applied
    assert not asyncio.run(_exists("beta"))  # bad file rolled back
    assert asyncio.run(_tracked()) == ["20260101000100_create_alpha"]  # version row too
    assert not asyncio.run(_exists("gamma"))  # runner stopped at the failure
