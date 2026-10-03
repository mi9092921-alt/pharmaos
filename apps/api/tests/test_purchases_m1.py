"""P2-M1 — supplier management HTTP tests.

Exercises the wiring on top of supplier_service: permission tiers
(purchases.view / purchases.create), CSRF on mutations, envelopes, validation,
full-field round-trip, active-only filtering, and 404s.
"""

import uuid

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.models import Role, User
from pharmaos_api.security.passwords import hash_password


async def _seed_user(db_session: AsyncSession, role_code: str) -> str:
    role = (await db_session.execute(select(Role).where(Role.code == role_code))).scalar_one()
    username = f"{role_code}_{uuid.uuid4().hex[:8]}"
    db_session.add(
        User(
            username=username,
            full_name=f"م {role_code}",
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


@pytest.fixture
async def admin(client: httpx.AsyncClient, db_session: AsyncSession):  # type: ignore[no-untyped-def]
    csrf = await _login(client, await _seed_user(db_session, "super_admin"))
    return client, csrf


_FULL = {
    "name": "شركة الأدوية المتحدة",
    "contact_name": "أحمد سمير",
    "phone": "0223456789",
    "email": "sales@united-pharma.eg",
    "address": "القاهرة، مصر",
    "tax_registration_no": "123-456-789",
    "payment_terms": "صافي 30 يوم",
    "notes": "مورّد رئيسي",
}


async def _create(client: httpx.AsyncClient, csrf: str, **overrides: object) -> httpx.Response:
    body = {**_FULL, **overrides}
    return await client.post(
        "/api/v1/purchases/suppliers", headers={"X-CSRF-Token": csrf}, json=body
    )


# ------------------------------ create / list / get ------------------------------


async def test_create_list_get_supplier(admin):  # type: ignore[no-untyped-def]
    client, csrf = admin
    unique = uuid.uuid4().hex[:8]
    made = await _create(client, csrf, name=f"{_FULL['name']} {unique}")
    assert made.status_code == 200, made.text
    data = made.json()["data"]
    supplier_id = data["id"]
    assert data["name"] == f"{_FULL['name']} {unique}"
    assert data["tax_registration_no"] == "123-456-789"
    assert data["is_active"] is True

    # Search by the unique tag so the membership check is robust to the paginated
    # list (default limit 50, ordered is_active DESC then name).
    listed = await client.get("/api/v1/purchases/suppliers", params={"search": unique})
    assert listed.status_code == 200, listed.text
    assert listed.json()["meta"]["total"] == 1
    assert listed.json()["data"][0]["id"] == supplier_id

    got = await client.get(f"/api/v1/purchases/suppliers/{supplier_id}")
    assert got.status_code == 200
    assert got.json()["data"]["contact_name"] == "أحمد سمير"


async def test_update_and_deactivate(admin):  # type: ignore[no-untyped-def]
    client, csrf = admin
    supplier_id = (await _create(client, csrf)).json()["data"]["id"]

    upd = await client.patch(
        f"/api/v1/purchases/suppliers/{supplier_id}",
        headers={"X-CSRF-Token": csrf},
        json={"phone": "0100000000", "is_active": False},
    )
    assert upd.status_code == 200, upd.text
    assert upd.json()["data"]["phone"] == "0100000000"
    assert upd.json()["data"]["is_active"] is False

    active = await client.get("/api/v1/purchases/suppliers", params={"active_only": True})
    assert all(s["id"] != supplier_id for s in active.json()["data"])
    # Still exists (soft-deactivated, not removed) — fetch by id. An inactive
    # supplier sorts last and can fall off the paginated default list once enough
    # active suppliers exist, so a page scan here would be a latent flake.
    fetched = await client.get(f"/api/v1/purchases/suppliers/{supplier_id}")
    assert fetched.status_code == 200
    assert fetched.json()["data"]["is_active"] is False


async def test_search_filter(admin):  # type: ignore[no-untyped-def]
    client, csrf = admin
    unique = uuid.uuid4().hex[:8]
    await _create(client, csrf, name=f"مورّد {unique}")
    found = await client.get("/api/v1/purchases/suppliers", params={"search": unique})
    assert found.status_code == 200
    assert found.json()["meta"]["total"] == 1


# ------------------------------ permissions / CSRF / validation ------------------------------


async def test_pharmacist_can_view_not_create(client, db_session: AsyncSession):  # type: ignore[no-untyped-def]
    """pharmacist holds purchases.view but NOT purchases.create."""
    csrf = await _login(client, await _seed_user(db_session, "pharmacist"))
    assert (await client.get("/api/v1/purchases/suppliers")).status_code == 200
    r = await _create(client, csrf)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "E-AUTH-002"


async def test_view_requires_permission(client, db_session: AsyncSession):  # type: ignore[no-untyped-def]
    """cashier is not in purchases.view."""
    await _login(client, await _seed_user(db_session, "cashier"))
    r = await client.get("/api/v1/purchases/suppliers")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "E-AUTH-002"


async def test_create_needs_csrf(admin):  # type: ignore[no-untyped-def]
    client, _csrf = admin
    r = await client.post("/api/v1/purchases/suppliers", json=_FULL)  # no X-CSRF-Token
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "E-AUTH-004"


async def test_name_required(admin):  # type: ignore[no-untyped-def]
    client, csrf = admin
    r = await _create(client, csrf, name="")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "E-VAL-001"


async def test_get_unknown_supplier_404(admin):  # type: ignore[no-untyped-def]
    client, _csrf = admin
    r = await client.get(f"/api/v1/purchases/suppliers/{uuid.uuid4()}")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "E-VAL-001"
