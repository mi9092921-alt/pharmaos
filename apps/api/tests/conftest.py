"""Test fixtures.

DB-backed tests run against a real PostgreSQL 17 with the project migrations
applied (TEST_DATABASE_URL). Unit tests (passwords/JWT/CSRF) need no DB.
Keys use the non-production dev-store fallback in a temp directory.
"""

import os
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest

# Test environment MUST be set before importing app modules (settings cache).
os.environ.setdefault("PHARMAOS_ENV", "test")
_TEST_DB = os.environ.get("TEST_DATABASE_URL", "postgresql://postgres@127.0.0.1:5433/pharmaos_test")
os.environ["DATABASE_URL"] = _TEST_DB
# The account-lockout tests need >5 login calls in one minute; the transport
# rate limit (5/min/IP) is exercised by its own dedicated test instead.
os.environ.setdefault("LOGIN_RATE_LIMIT_PER_MINUTE", "1000")


@pytest.fixture(scope="session", autouse=True)
def _isolated_keystore(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Point the dev-store fallback at ONE temp dir for the whole session and force
    the fallback (no OS keyring in CI). Session scope keeps the encryption key
    STABLE across tests — mirroring the real OS keystore, where a value encrypted
    by one operation is decryptable by another (a per-test key would not be)."""
    from pathlib import Path

    from pharmaos_api.security import keystore

    def _no_backend(*_a: object, **_k: object) -> None:
        raise keystore.KeyringError("no backend")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(keystore, "_DEV_STORE_DIR", Path(tmp_path_factory.mktemp("devkeys")))
        mp.setattr(keystore.keyring, "get_password", _no_backend)
        mp.setattr(keystore.keyring, "set_password", _no_backend)
        yield


@pytest.fixture
async def db_session() -> AsyncIterator[object]:
    from pharmaos_api.db import get_session_factory

    async with get_session_factory()() as session:
        yield session


@pytest.fixture
async def seeded_user(db_session) -> dict[str, str]:  # type: ignore[no-untyped-def]
    """A fresh active user with a known password and the super_admin role."""
    from sqlalchemy import select

    from pharmaos_api.cli import SUPER_ADMIN_ROLE_CODE, SUPER_ADMIN_ROLE_NAME_AR
    from pharmaos_api.models import Role, User
    from pharmaos_api.security.passwords import hash_password

    role = (
        await db_session.execute(select(Role).where(Role.code == SUPER_ADMIN_ROLE_CODE))
    ).scalar_one_or_none()
    if role is None:
        role = Role(code=SUPER_ADMIN_ROLE_CODE, name_ar=SUPER_ADMIN_ROLE_NAME_AR, is_system=True)
        db_session.add(role)
        await db_session.flush()

    username = f"admin_{uuid.uuid4().hex[:10]}"
    password = "Sup3r@dmin!"
    user = User(
        username=username,
        full_name="مدير الاختبار",
        password_hash=hash_password(password),
        role_id=role.id,
    )
    db_session.add(user)
    await db_session.commit()
    return {"username": username, "password": password, "id": str(user.id)}


@pytest.fixture
async def client() -> AsyncIterator[object]:
    import httpx

    from pharmaos_api.main import create_app

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
