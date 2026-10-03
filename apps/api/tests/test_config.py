"""Branch & settings management (P1-M4): settings upsert, branch update,
settings.changed audit, the view/edit permission tiers, CSRF, and validation.
End-to-end over HTTP."""

import uuid

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.models import Branch, Role, User
from pharmaos_api.security.passwords import hash_password


async def _seed_user(db_session: AsyncSession, role_code: str) -> str:
    role = (await db_session.execute(select(Role).where(Role.code == role_code))).scalar_one()
    username = f"{role_code}_{uuid.uuid4().hex[:8]}"
    db_session.add(
        User(
            username=username,
            full_name=f"مستخدم {role_code}",
            password_hash=hash_password("T3st@user!"),
            role_id=role.id,
        )
    )
    await db_session.commit()
    return username


async def _login(client: httpx.AsyncClient, username: str) -> str:
    r = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": "T3st@user!"}
    )
    assert r.status_code == 200, r.text
    return r.json()["data"]["csrf_token"]


async def _branch_id(db_session: AsyncSession) -> str:
    branch = Branch(name=f"فرع {uuid.uuid4().hex[:6]}", country_code="EG", currency_code="EGP")
    db_session.add(branch)
    await db_session.commit()
    return str(branch.id)


@pytest.fixture
async def admin(client: httpx.AsyncClient, db_session: AsyncSession):
    username = await _seed_user(db_session, "super_admin")
    csrf = await _login(client, username)
    return client, csrf


async def test_settings_upsert_creates_then_updates_and_audits(admin, db_session: AsyncSession):
    client, csrf = admin
    bid = await _branch_id(db_session)

    # create
    r = await client.put(
        f"/api/v1/branches/{bid}/settings",
        headers={"X-CSRF-Token": csrf},
        json={
            "pharmacy_name": "صيدلية النور",
            "license_number": "LIC-123",
            "paper_size": "80mm",
            "show_qr_code": True,
            "max_discount_percent": "10.00",
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["pharmacy_name"] == "صيدلية النور"
    assert data["show_qr_code"] is True
    assert data["max_discount_percent"] == "10.00"

    # update (same branch -> upsert updates the same row)
    r2 = await client.put(
        f"/api/v1/branches/{bid}/settings",
        headers={"X-CSRF-Token": csrf},
        json={"pharmacy_name": "صيدلية النور", "thank_you_message": "شكراً", "paper_size": "A4"},
    )
    assert r2.status_code == 200
    assert r2.json()["data"]["paper_size"] == "A4"

    # exactly one settings row for the branch (upsert, not insert-twice)
    count = (
        await db_session.execute(
            text("SELECT COUNT(*) FROM settings WHERE branch_id = CAST(:b AS uuid)").bindparams(
                b=bid
            )
        )
    ).scalar_one()
    assert count == 1

    audits = (
        await db_session.execute(
            text(
                "SELECT COUNT(*) FROM audit_logs WHERE action='settings.changed' "
                "AND entity_type='settings'"
            )
        )
    ).scalar_one()
    assert audits >= 2  # create + update


async def test_branch_update_validates_and_audits(admin, db_session: AsyncSession):
    client, csrf = admin
    bid = await _branch_id(db_session)

    # unknown currency rejected
    bad = await client.patch(
        f"/api/v1/branches/{bid}",
        headers={"X-CSRF-Token": csrf},
        json={"currency_code": "ZZZ"},
    )
    assert bad.status_code == 422

    ok = await client.patch(
        f"/api/v1/branches/{bid}",
        headers={"X-CSRF-Token": csrf},
        json={"name": "الفرع الرئيسي"},
    )
    assert ok.status_code == 200
    assert ok.json()["data"]["name"] == "الفرع الرئيسي"

    audited = (
        await db_session.execute(
            text(
                "SELECT COUNT(*) FROM audit_logs WHERE action='settings.changed' "
                "AND entity_type='branch' AND entity_id=CAST(:b AS uuid)"
            ).bindparams(b=bid)
        )
    ).scalar_one()
    assert audited == 1


async def test_paper_size_and_discount_validation(admin, db_session: AsyncSession):
    client, csrf = admin
    bid = await _branch_id(db_session)
    r = await client.put(
        f"/api/v1/branches/{bid}/settings",
        headers={"X-CSRF-Token": csrf},
        json={"pharmacy_name": "x", "paper_size": "A3"},  # invalid
    )
    assert r.status_code == 422
    r2 = await client.put(
        f"/api/v1/branches/{bid}/settings",
        headers={"X-CSRF-Token": csrf},
        json={"pharmacy_name": "x", "max_discount_percent": "150"},  # > 100
    )
    assert r2.status_code == 422


async def test_branch_manager_can_view_not_edit(
    client: httpx.AsyncClient, db_session: AsyncSession
):
    bid = await _branch_id(db_session)
    manager = await _seed_user(db_session, "branch_manager")
    csrf = await _login(client, manager)

    # view allowed (settings.view)
    assert (await client.get("/api/v1/branches")).status_code == 200
    assert (await client.get(f"/api/v1/branches/{bid}/settings")).status_code == 200

    # edit denied (settings.edit is super_admin only)
    r = await client.put(
        f"/api/v1/branches/{bid}/settings",
        headers={"X-CSRF-Token": csrf},
        json={"pharmacy_name": "x"},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "E-AUTH-002"


async def test_cashier_cannot_view_settings(client: httpx.AsyncClient, db_session: AsyncSession):
    cashier = await _seed_user(db_session, "cashier")
    await _login(client, cashier)
    assert (await client.get("/api/v1/branches")).status_code == 403


async def test_settings_edit_requires_csrf(admin, db_session: AsyncSession):
    client, _csrf = admin
    bid = await _branch_id(db_session)
    r = await client.put(f"/api/v1/branches/{bid}/settings", json={"pharmacy_name": "x"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "E-AUTH-004"
