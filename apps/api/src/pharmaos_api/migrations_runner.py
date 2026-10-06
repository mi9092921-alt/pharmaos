"""Python port of packages/db/scripts/apply-migrations.sh (installer decision 7).

Identical semantics to the bash runner, which stays the CI/scratch path:
- every migration file applies in ONE transaction, in filename order;
- applied versions are tracked in ``_pharmaos_migrations``;
- code-defined seeds (RBAC) re-apply on EVERY run — idempotent, "code wins";
- UTF-8 end to end (asyncpg speaks UTF-8 natively — the psql console-codepage
  mojibake class of bugs cannot happen here).

Device path (no bash/psql on the device): asyncpg only. Multi-statement files
with plpgsql bodies work because asyncpg executes parameterless statements
through the simple query protocol.

One deliberate improvement over the bash script: the version INSERT happens
inside the SAME transaction as the migration, so a crash can never leave a
migration applied-but-untracked.
"""

import os
from pathlib import Path

import asyncpg

_MIGRATIONS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS _pharmaos_migrations (
    version     TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


class MigrationError(RuntimeError):
    """A migration failed; the transaction (and version row) rolled back."""


def default_migrations_dir() -> Path:
    """Repo layout in dev/CI; on a device PHARMAOS_MIGRATIONS_DIR points at
    the bundled copy (PyInstaller data files, wired in M2)."""
    env = os.environ.get("PHARMAOS_MIGRATIONS_DIR")
    if env:
        return Path(env)
    # .../apps/api/src/pharmaos_api/migrations_runner.py -> repo root
    repo_root = Path(__file__).resolve().parents[4]
    return repo_root / "supabase" / "migrations"


def default_seeds_dir() -> Path:
    env = os.environ.get("PHARMAOS_SEEDS_DIR")
    if env:
        return Path(env)
    repo_root = Path(__file__).resolve().parents[4]
    return repo_root / "packages" / "db" / "seeds"


def _load_migrations(migrations_dir: Path) -> list[tuple[str, str]]:
    return [
        (file.stem, file.read_text(encoding="utf-8"))
        for file in sorted(migrations_dir.glob("*.sql"))
    ]


def _load_seeds(seeds_dir: Path) -> list[tuple[str, str]]:
    return [
        (file.name, file.read_text(encoding="utf-8")) for file in sorted(seeds_dir.glob("*.sql"))
    ]


async def run_migrations_async(
    dsn: str, *, migrations: list[tuple[str, str]], seeds: list[tuple[str, str]]
) -> dict[str, object]:
    """Apply pre-loaded migrations + seeds. ``migrations``/``seeds`` carry
    (version-or-filename, sql) — loaded synchronously by run_migrations()."""
    applied: list[str] = []
    skipped: list[str] = []
    seed_names: list[str] = []

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(_MIGRATIONS_TABLE_SQL)
        tracked = await conn.fetch("SELECT version FROM _pharmaos_migrations")
        done = {row["version"] for row in tracked}

        for version, sql in migrations:
            if version in done:
                skipped.append(version)
                continue
            try:
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO _pharmaos_migrations(version) VALUES ($1)", version
                    )
            except Exception as exc:
                raise MigrationError(f"migration {version} failed and rolled back: {exc}") from exc
            applied.append(version)

        for name, sql in seeds:
            await conn.execute(sql)
            seed_names.append(name)
    finally:
        await conn.close()

    return {"applied": applied, "skipped": skipped, "seeds": seed_names}


def run_migrations(
    dsn: str, *, migrations_dir: Path | None = None, seeds_dir: Path | None = None
) -> dict[str, object]:
    """Sync entry point (CLI/backup-restore). Returns applied/skipped/seeds."""
    from pharmaos_api.utils_async import run_coro_sync

    return run_coro_sync(
        run_migrations_async(
            dsn,
            migrations=_load_migrations(migrations_dir or default_migrations_dir()),
            seeds=_load_seeds(seeds_dir if seeds_dir is not None else default_seeds_dir()),
        )
    )
