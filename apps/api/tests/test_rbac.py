"""RBAC verification (CLAUDE.md: full permission-matrix test suite).

- The FULL matrix in the DB must equal the matrix in the generated seed
  (source: packages/shared/src/permissions.ts — code wins).
- require_permission enforces grants on endpoints (allowed vs denied).
- Manual DB grants on system roles are reverted by re-seeding.
"""

import re
import subprocess
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi import Depends
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SEED_FILE = _REPO_ROOT / "packages" / "db" / "seeds" / "rbac_seed.sql"
_PAIR_RE = re.compile(r"^\s*\('([a-z_]+)',\s*'([a-z_.]+)'\)[,;]?\s*$")


def _expected_matrix() -> set[tuple[str, str]]:
    """(role_code, permission_code) pairs parsed from the generated seed."""
    pairs: set[tuple[str, str]] = set()
    in_matrix = False
    for line in _SEED_FILE.read_text(encoding="utf-8").splitlines():
        if "INSERT INTO _rbac_matrix" in line:
            in_matrix = True
            continue
        if in_matrix:
            m = _PAIR_RE.match(line)
            if m:
                pairs.add((m.group(1), m.group(2)))
            elif line.strip().startswith("--") or not line.strip():
                break
    return pairs


def _apply_seed(database_url: str) -> None:
    subprocess.run(
        ["psql", database_url, "-v", "ON_ERROR_STOP=1", "-q", "-f", str(_SEED_FILE)],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def sync_test_db_url() -> str:
    import os

    return os.environ["DATABASE_URL"]


async def test_full_matrix_matches_code(db_session: AsyncSession, sync_test_db_url: str) -> None:
    _apply_seed(sync_test_db_url)
    expected = _expected_matrix()
    assert len(expected) == 106  # 6 roles / 40 permissions / 106 grants (P2-M8 added 4 x 3 roles)

    rows = (await db_session.execute(text("""
                SELECT r.code, p.code
                FROM role_permissions rp
                JOIN roles r ON r.id = rp.role_id
                JOIN permissions p ON p.id = rp.permission_id
                WHERE NOT rp.is_deleted AND r.is_system
                """))).all()
    actual = {(role, perm) for role, perm in rows}
    assert actual == expected


async def test_super_admin_has_every_permission(
    db_session: AsyncSession, sync_test_db_url: str
) -> None:
    _apply_seed(sync_test_db_url)
    count = (await db_session.execute(text("""
                SELECT COUNT(*)
                FROM role_permissions rp
                JOIN roles r ON r.id = rp.role_id
                WHERE r.code = 'super_admin' AND NOT rp.is_deleted
                """))).scalar_one()
    total = (
        await db_session.execute(text("SELECT COUNT(*) FROM permissions WHERE NOT is_deleted"))
    ).scalar_one()
    assert count == total == 40


async def test_code_wins_over_manual_db_edit(
    db_session: AsyncSession, sync_test_db_url: str
) -> None:
    """Manually granting 'settings.edit' to viewer must be reverted by the seed."""
    _apply_seed(sync_test_db_url)
    await db_session.execute(text("""
            INSERT INTO role_permissions (role_id, permission_id)
            SELECT r.id, p.id FROM roles r, permissions p
            WHERE r.code = 'viewer' AND p.code = 'settings.edit'
            ON CONFLICT (role_id, permission_id) DO UPDATE SET is_deleted = FALSE
            """))
    await db_session.commit()

    _apply_seed(sync_test_db_url)
    granted = (await db_session.execute(text("""
                SELECT COUNT(*) FROM role_permissions rp
                JOIN roles r ON r.id = rp.role_id
                JOIN permissions p ON p.id = rp.permission_id
                WHERE r.code = 'viewer' AND p.code = 'settings.edit' AND NOT rp.is_deleted
                """))).scalar_one()
    assert granted == 0  # soft-deleted by the seeder (code wins; never hard-deleted)


async def _login_as_role(
    client: httpx.AsyncClient, db_session: AsyncSession, role_code: str
) -> None:
    from pharmaos_api.models import Role, User
    from pharmaos_api.security.passwords import hash_password

    role = (await db_session.execute(select(Role).where(Role.code == role_code))).scalar_one()
    username = f"{role_code}_{uuid.uuid4().hex[:8]}"
    db_session.add(
        User(
            username=username,
            full_name=f"اختبار {role_code}",
            password_hash=hash_password("T3st@user!"),
            role_id=role.id,
        )
    )
    await db_session.commit()
    r = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": "T3st@user!"}
    )
    assert r.status_code == 200


async def test_require_permission_guard(db_session: AsyncSession, sync_test_db_url: str) -> None:
    """Endpoint guarded with settings.edit: super_admin allowed, cashier denied (E-AUTH-002)."""
    from pharmaos_api.deps import require_permission
    from pharmaos_api.main import create_app

    _apply_seed(sync_test_db_url)
    app = create_app()

    @app.get("/api/v1/_test/protected")
    async def protected(_: None = Depends(require_permission("settings.edit"))) -> dict[str, bool]:
        return {"ok": True}

    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login_as_role(client, db_session, "cashier")
        r = await client.get("/api/v1/_test/protected")
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "E-AUTH-002"

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login_as_role(client, db_session, "super_admin")
        r = await client.get("/api/v1/_test/protected")
        assert r.status_code == 200
