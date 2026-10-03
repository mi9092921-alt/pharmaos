"""User & role management (P1-M3): CRUD, role assignment, activate/deactivate,
password reset, session invalidation (token_version), audit wiring, the
settings.users permission guard, and self-lockout guards.

End-to-end over HTTP so the router guards + CSRF are exercised too."""

import uuid

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.main import create_app
from pharmaos_api.models import Role, User
from pharmaos_api.security.passwords import hash_password


async def _seed_role_user(
    db_session: AsyncSession, role_code: str, password: str = "T3st@user!"
) -> str:
    role = (await db_session.execute(select(Role).where(Role.code == role_code))).scalar_one()
    username = f"{role_code}_{uuid.uuid4().hex[:8]}"
    db_session.add(
        User(
            username=username,
            full_name=f"مستخدم {role_code}",
            password_hash=hash_password(password),
            role_id=role.id,
        )
    )
    await db_session.commit()
    return username


async def _login(client: httpx.AsyncClient, username: str, password: str = "T3st@user!") -> str:
    r = await client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["data"]["csrf_token"]


@pytest.fixture
async def admin_client(client: httpx.AsyncClient, db_session: AsyncSession):
    """An httpx client logged in as a super_admin, plus its CSRF token."""
    username = await _seed_role_user(db_session, "super_admin")
    csrf = await _login(client, username)
    return client, csrf, username


async def test_non_admin_cannot_access(client: httpx.AsyncClient, db_session: AsyncSession):
    username = await _seed_role_user(db_session, "pharmacist")
    await _login(client, username)
    r = await client.get("/api/v1/users")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "E-AUTH-002"


async def test_create_user_audits_and_hides_secrets(admin_client, db_session: AsyncSession):
    client, csrf, _ = admin_client
    uname = f"newuser_{uuid.uuid4().hex[:6]}"
    r = await client.post(
        "/api/v1/users",
        headers={"X-CSRF-Token": csrf},
        json={
            "username": uname,
            "full_name": "مستخدم جديد",
            "password": "Str0ng@pass",
            "role_code": "cashier",
            "phone": "01000000000",
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["role"] == "cashier"
    assert data["phone"] == "01000000000"  # decrypted for the authorized read
    assert "password" not in data and "password_hash" not in data

    # phone stored ENCRYPTED at rest
    raw = (
        await db_session.execute(
            text("SELECT phone_encrypted FROM users WHERE username = :u").bindparams(u=uname)
        )
    ).scalar_one()
    assert raw is not None and b"01000000000" not in bytes(raw)

    # audited: user.created
    action = (
        await db_session.execute(
            text(
                "SELECT action FROM audit_logs WHERE entity_type='user' "
                "AND entity_id = (SELECT id FROM users WHERE username=:u) "
                "AND action='user.created'"
            ).bindparams(u=uname)
        )
    ).scalar_one_or_none()
    assert action == "user.created"


async def test_duplicate_username_rejected(admin_client):
    client, csrf, _ = admin_client
    uname = f"dupe_{uuid.uuid4().hex[:6]}"
    body = {
        "username": uname,
        "full_name": "x",
        "password": "Str0ng@pass",
        "role_code": "viewer",
    }
    r1 = await client.post("/api/v1/users", headers={"X-CSRF-Token": csrf}, json=body)
    assert r1.status_code == 200
    r2 = await client.post("/api/v1/users", headers={"X-CSRF-Token": csrf}, json=body)
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "E-USR-001"


async def test_weak_password_rejected(admin_client):
    client, csrf, _ = admin_client
    r = await client.post(
        "/api/v1/users",
        headers={"X-CSRF-Token": csrf},
        json={
            "username": f"weak_{uuid.uuid4().hex[:6]}",
            "full_name": "x",
            "password": "weak",  # fails min length + policy
            "role_code": "viewer",
        },
    )
    assert r.status_code == 422


async def test_create_requires_csrf(admin_client):
    client, _csrf, _ = admin_client
    r = await client.post(
        "/api/v1/users",
        json={
            "username": f"nocsrf_{uuid.uuid4().hex[:6]}",
            "full_name": "x",
            "password": "Str0ng@pass",
            "role_code": "viewer",
        },
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "E-AUTH-004"


async def test_role_change_audits_and_bumps_token(admin_client, db_session: AsyncSession):
    client, csrf, _ = admin_client
    target = await _seed_role_user(db_session, "viewer")
    tid = (
        await db_session.execute(
            text("SELECT id FROM users WHERE username=:u").bindparams(u=target)
        )
    ).scalar_one()

    r = await client.post(
        f"/api/v1/users/{tid}/role",
        headers={"X-CSRF-Token": csrf},
        json={"role_code": "pharmacist"},
    )
    assert r.status_code == 200
    assert r.json()["data"]["role"] == "pharmacist"

    row = (
        await db_session.execute(
            text("SELECT token_version FROM users WHERE id=:i").bindparams(i=tid)
        )
    ).scalar_one()
    assert row == 1  # bumped from 0

    audited = (
        await db_session.execute(
            text(
                "SELECT COUNT(*) FROM audit_logs WHERE action='user.role_changed' "
                "AND entity_id=:i"
            ).bindparams(i=tid)
        )
    ).scalar_one()
    assert audited == 1


async def test_deactivate_logs_out_and_audits(
    client: httpx.AsyncClient, db_session: AsyncSession, admin_client
):
    admin, csrf, _ = admin_client
    # A separate victim who is logged in on their own client.
    victim = await _seed_role_user(db_session, "cashier")
    vid = (
        await db_session.execute(
            text("SELECT id FROM users WHERE username=:u").bindparams(u=victim)
        )
    ).scalar_one()

    victim_transport = httpx.ASGITransport(app=create_app())  # separate client, same shared DB
    async with httpx.AsyncClient(transport=victim_transport, base_url="http://testserver") as vc:
        await _login(vc, victim)
        assert (await vc.get("/api/v1/auth/me")).status_code == 200

        # admin deactivates the victim
        r = await admin.post(
            f"/api/v1/users/{vid}/active",
            headers={"X-CSRF-Token": csrf},
            json={"active": False},
        )
        assert r.status_code == 200
        assert r.json()["data"]["is_active"] is False

        # victim's existing session is now rejected (token_version bumped + inactive)
        assert (await vc.get("/api/v1/auth/me")).status_code == 401

    audited = (
        await db_session.execute(
            text(
                "SELECT COUNT(*) FROM audit_logs WHERE action='user.deactivated' AND entity_id=:i"
            ).bindparams(i=vid)
        )
    ).scalar_one()
    assert audited == 1


async def test_activate_has_no_audit_event(admin_client, db_session: AsyncSession):
    client, csrf, _ = admin_client
    target = await _seed_role_user(db_session, "viewer")
    tid = (
        await db_session.execute(
            text("SELECT id FROM users WHERE username=:u").bindparams(u=target)
        )
    ).scalar_one()
    await client.post(
        f"/api/v1/users/{tid}/active", headers={"X-CSRF-Token": csrf}, json={"active": False}
    )
    await client.post(
        f"/api/v1/users/{tid}/active", headers={"X-CSRF-Token": csrf}, json={"active": True}
    )
    # activation is NOT in AUDITED_OPERATIONS -> no invented event
    count = (
        await db_session.execute(
            text("SELECT COUNT(*) FROM audit_logs WHERE action='user.activated'")
        )
    ).scalar_one()
    assert count == 0


async def test_password_reset_invalidates_sessions(db_session: AsyncSession, admin_client):
    admin, csrf, _ = admin_client
    victim = await _seed_role_user(db_session, "pharmacist")
    vid = (
        await db_session.execute(
            text("SELECT id FROM users WHERE username=:u").bindparams(u=victim)
        )
    ).scalar_one()

    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as vc:
        await _login(vc, victim)
        assert (await vc.get("/api/v1/auth/me")).status_code == 200

        r = await admin.post(
            f"/api/v1/users/{vid}/reset-password",
            headers={"X-CSRF-Token": csrf},
            json={"new_password": "N3w@password"},
        )
        assert r.status_code == 200

        # old session invalidated by token_version bump
        assert (await vc.get("/api/v1/auth/me")).status_code == 401
        # the new password works
        await _login(vc, victim, password="N3w@password")


async def test_cannot_deactivate_or_demote_self(admin_client, db_session: AsyncSession):
    client, csrf, admin_username = admin_client
    aid = (
        await db_session.execute(
            text("SELECT id FROM users WHERE username=:u").bindparams(u=admin_username)
        )
    ).scalar_one()

    r1 = await client.post(
        f"/api/v1/users/{aid}/active", headers={"X-CSRF-Token": csrf}, json={"active": False}
    )
    assert r1.status_code == 422  # self-lockout guard

    r2 = await client.post(
        f"/api/v1/users/{aid}/role",
        headers={"X-CSRF-Token": csrf},
        json={"role_code": "viewer"},
    )
    assert r2.status_code == 422  # cannot change own role


async def test_list_is_paginated(admin_client):
    client, _csrf, _ = admin_client
    r = await client.get("/api/v1/users?skip=0&limit=5")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["meta"]["per_page"] == 5
    assert len(body["data"]) <= 5
