"""Licensing test isolation (P4-M1).

`license_clock_events` forbids DELETE/TRUNCATE (by design), so tests that write
the chain need a FRESH database per test. Applying all migrations per test
would cost seconds each — instead a session-scoped TEMPLATE database is
migrated once, and every test clones it instantly (CREATE DATABASE … TEMPLATE).

* `licensing_fresh` — per-test cloned DB for the state/activation modules
  (each test gets its own keystore too, so the key_lost / virgin-generation /
  tamper matrix is fully independent).
* `chain_scratch` — a dedicated session-scoped fresh DB for the chain module's
  absolute assertions (genesis, N=50 ⇒ exactly 50 rows, seq 1..50); the chain
  tests are order-independent via relative assertions after the first.
* Keystore: function-scoped dev-store dirs (the root conftest provides the
  session-wide default that mirrors a real device).
"""

import asyncio
import contextlib
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest


def _repo_root() -> Path:
    # tests/licensing/conftest.py → [0]=licensing, [1]=tests, [2]=api, [3]=apps, [4]=repo
    return Path(__file__).resolve().parents[4]


def _admin_url(base_url: str) -> str:
    return base_url.rsplit("/", 1)[0] + "/postgres"


def _run_async(coro: Any) -> Any:
    return asyncio.run(coro)


def _apply_migrations(db_url: str) -> None:
    """Apply every migration + seed to a FRESH scratch DB via asyncpg's simple
    query protocol (multi-statement safe, platform-independent — no bash/psql
    needed). Tracking via _pharmaos_migrations is unnecessary: scratch DBs are
    born empty, fully migrated, and dropped whole."""
    repo = _repo_root()
    migrations = sorted((repo / "supabase" / "migrations").glob("*.sql"))
    seeds = sorted((repo / "packages" / "db" / "seeds").glob("*.sql"))
    if not migrations:
        raise RuntimeError(f"no migrations found under {repo / 'supabase' / 'migrations'}")

    async def _run() -> None:
        import asyncpg

        conn = await asyncpg.connect(db_url)
        try:
            for path in [*migrations, *seeds]:
                await conn.execute(path.read_text(encoding="utf-8"))
        finally:
            await conn.close()

    _run_async(_run())


def _sql(admin_url: str, statement: str) -> None:
    async def _run() -> None:
        import asyncpg

        conn = await asyncpg.connect(admin_url)
        try:
            await conn.execute(statement)
        finally:
            await conn.close()

    _run_async(_run())


class ClonedDb:
    """A per-test clone of the migrated template database."""

    def __init__(self, base_url: str, template: str) -> None:
        self._admin_url = _admin_url(base_url)
        self.name = f"pharmaos_lic_{uuid.uuid4().hex[:10]}"
        self.url = base_url.rsplit("/", 1)[0] + "/" + self.name
        self._template = template
        self._factory: Any = None

    def create(self) -> None:
        _sql(self._admin_url, f'CREATE DATABASE "{self.name}" TEMPLATE "{self._template}"')

    def session_factory(self) -> Any:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        if self._factory is None:
            # TEST_DATABASE_URL uses the bare postgresql:// scheme — the async
            # engine needs the asyncpg driver spelled explicitly.
            url = self.url.replace("postgresql://", "postgresql+asyncpg://", 1)
            engine = create_async_engine(url, pool_pre_ping=True)
            self._factory = async_sessionmaker(engine, expire_on_commit=False)
        return self._factory

    def dispose_and_drop(self) -> None:
        if self._factory is not None:
            engine = self._factory.kw["bind"]
            # connections on a closed test loop may refuse to close — the
            # FORCE drop below terminates them regardless
            with contextlib.suppress(Exception):
                _run_async(engine.dispose())
        _sql(self._admin_url, f'DROP DATABASE IF EXISTS "{self.name}" WITH (FORCE)')


def _base_url() -> str:
    return os.environ.get("TEST_DATABASE_URL", "postgresql://postgres@127.0.0.1:5433/pharmaos_test")


@pytest.fixture(scope="session")
def licensing_template() -> Iterator[str]:
    """Migrate ONE template database per session; every test clones it."""
    base = _base_url()
    template = f"pharmaos_lic_tpl_{uuid.uuid4().hex[:10]}"
    _sql(_admin_url(base), f'CREATE DATABASE "{template}"')
    try:
        _apply_migrations(base.rsplit("/", 1)[0] + "/" + template)
        yield template
    finally:
        _sql(_admin_url(base), f'DROP DATABASE IF EXISTS "{template}" WITH (FORCE)')


@pytest.fixture
def licensing_fresh(licensing_template: str) -> Iterator[Any]:
    """A per-test licensing world: cloned DB + the caller's keystore fixture.
    SYNC fixture on purpose — setup/teardown run outside the session event
    loop, so asyncio.run is safe there while the async tests use the loop."""
    db = ClonedDb(_base_url(), licensing_template)
    db.create()
    try:
        yield db.session_factory()
    finally:
        db.dispose_and_drop()


@pytest.fixture(scope="session")
def chain_scratch() -> Iterator[ClonedDb]:
    """A dedicated session-scoped fresh (fully migrated) DB for the chain
    module's absolute assertions."""
    db = ClonedDb(_base_url(), "")
    _sql(_admin_url(_base_url()), f'CREATE DATABASE "{db.name}"')
    try:
        _apply_migrations(db.url)
        yield db
    finally:
        db.dispose_and_drop()


@pytest.fixture
def keystore_per_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh dev-keystore directory per test — the key_lost / virgin
    generation matrix needs independent stores."""
    from pharmaos_api.security import keystore

    store_dir = tmp_path / "devkeys"
    monkeypatch.setattr(keystore, "_DEV_STORE_DIR", store_dir)
    return store_dir
