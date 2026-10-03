"""Inventory REST API (P1-M7): receive / adjust / quarantine endpoints with the
permission tiers and CSRF, stock-on-hand + batch reads, GS1-prefilled receiving,
minimal suppliers, and the cache-integrity (drift / rebuild) endpoints.

These exercise the HTTP layer on top of the already-tested inventory_service
core (see test_inventory.py), so they focus on wiring: permissions, envelopes,
validation mapping, and that the derived cache is observable through the API.
"""

import datetime as dt
import uuid

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.models import Branch, Medication, Role, User
from pharmaos_api.security.passwords import hash_password

FUTURE = (dt.date.today() + dt.timedelta(days=365)).isoformat()


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
async def admin(client: httpx.AsyncClient, db_session: AsyncSession):
    csrf = await _login(client, await _seed_user(db_session, "super_admin"))
    return client, csrf


@pytest.fixture
async def branch(db_session: AsyncSession) -> Branch:
    b = Branch(name=f"فرع {uuid.uuid4().hex[:6]}", country_code="EG", currency_code="EGP")
    db_session.add(b)
    await db_session.commit()
    return b


async def _make_med(db_session: AsyncSession, *, gtin: str | None = None) -> str:
    med = Medication(
        trade_name=f"InvApi {uuid.uuid4().hex[:6]}", trade_name_ar="دواء اختبار", gtin=gtin
    )
    db_session.add(med)
    await db_session.commit()
    return str(med.id)


async def _receive(client, csrf, *, branch_id, medication_id, qty="100", expiry=FUTURE, **extra):
    body = {
        "branch_id": branch_id,
        "medication_id": medication_id,
        "batch_number": f"B-{uuid.uuid4().hex[:6]}",
        "expiry_date": expiry,
        "quantity": qty,
        "purchase_price": "2.50",
        **extra,
    }
    return await client.post("/api/v1/inventory/receive", headers={"X-CSRF-Token": csrf}, json=body)


# ------------------------------ receive + reads ------------------------------


async def test_receive_shows_in_stock_and_batches(admin, branch, db_session: AsyncSession):
    client, csrf = admin
    med_id = await _make_med(db_session)
    r = await _receive(client, csrf, branch_id=str(branch.id), medication_id=med_id, qty="120")
    assert r.status_code == 200, r.text
    batch = r.json()["data"]
    assert batch["quantity"] == "120.000" or batch["quantity"] == "120"
    assert batch["status"] == "active"

    # the branch is discoverable through the inventory-scoped branch list
    branches = await client.get("/api/v1/inventory/branches")
    assert branches.status_code == 200
    assert any(b["id"] == str(branch.id) for b in branches.json()["data"])

    # stock-on-hand reflects the derived cache
    inv = await client.get("/api/v1/inventory", params={"branch_id": str(branch.id)})
    assert inv.status_code == 200, inv.text
    rows = inv.json()["data"]
    mine = next(x for x in rows if x["medication_id"] == med_id)
    assert mine["cached_quantity"] in ("120.000", "120")

    # batches list (FEFO) shows the received batch with the medication name
    bl = await client.get(
        "/api/v1/inventory/batches",
        params={"branch_id": str(branch.id), "medication_id": med_id},
    )
    assert bl.status_code == 200
    data = bl.json()["data"]
    assert len(data) == 1 and data[0]["batch_number"] == batch["batch_number"]
    assert data[0]["trade_name_ar"] == "دواء اختبار"


async def test_receive_rejects_expired_via_api(admin, branch, db_session: AsyncSession):
    client, csrf = admin
    med_id = await _make_med(db_session)
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    r = await _receive(
        client, csrf, branch_id=str(branch.id), medication_id=med_id, expiry=yesterday
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "E-STK-002"


async def test_receive_requires_purchase_permission(client, branch, db_session: AsyncSession):
    """data_entry has inventory.add but NOT inventory.purchase."""
    csrf = await _login(client, await _seed_user(db_session, "data_entry"))
    med_id = await _make_med(db_session)
    r = await _receive(client, csrf, branch_id=str(branch.id), medication_id=med_id)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "E-AUTH-002"


async def test_receive_needs_csrf(admin, branch, db_session: AsyncSession):
    client, _csrf = admin
    med_id = await _make_med(db_session)
    body = {
        "branch_id": str(branch.id),
        "medication_id": med_id,
        "batch_number": "NOCSRF",
        "expiry_date": FUTURE,
        "quantity": "5",
        "purchase_price": "1.00",
    }
    r = await client.post("/api/v1/inventory/receive", json=body)  # no X-CSRF-Token
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "E-AUTH-004"


# ------------------------------ adjust + status ------------------------------


async def test_adjust_reason_required_and_overdraw(admin, branch, db_session: AsyncSession):
    client, csrf = admin
    med_id = await _make_med(db_session)
    batch = (
        await _receive(client, csrf, branch_id=str(branch.id), medication_id=med_id, qty="50")
    ).json()["data"]
    bid = batch["id"]

    # empty reason -> validation error (body constraint)
    blank = await client.post(
        f"/api/v1/inventory/batches/{bid}/adjust",
        headers={"X-CSRF-Token": csrf},
        json={"quantity_delta": "-5", "reason": ""},
    )
    assert blank.status_code == 422

    ok = await client.post(
        f"/api/v1/inventory/batches/{bid}/adjust",
        headers={"X-CSRF-Token": csrf},
        json={"quantity_delta": "-5", "reason": "تالف"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["data"]["quantity"] == "45.000"

    over = await client.post(
        f"/api/v1/inventory/batches/{bid}/adjust",
        headers={"X-CSRF-Token": csrf},
        json={"quantity_delta": "-999", "reason": "خطأ"},
    )
    assert over.status_code == 409
    assert over.json()["error"]["code"] == "E-STK-001"


async def test_quarantine_removes_from_stock_and_drift_ok(admin, branch, db_session: AsyncSession):
    client, csrf = admin
    med_id = await _make_med(db_session)
    batch = (
        await _receive(client, csrf, branch_id=str(branch.id), medication_id=med_id, qty="30")
    ).json()["data"]

    q = await client.post(
        f"/api/v1/inventory/batches/{batch['id']}/status",
        headers={"X-CSRF-Token": csrf},
        json={"status": "quarantined", "reason": "اشتباه تلف"},
    )
    assert q.status_code == 200, q.text
    assert q.json()["data"]["status"] == "quarantined"

    inv = await client.get(
        "/api/v1/inventory", params={"branch_id": str(branch.id), "search": "اختبار"}
    )
    mine = [x for x in inv.json()["data"] if x["medication_id"] == med_id]
    assert mine and mine[0]["cached_quantity"] in ("0.000", "0")

    drift = await client.get("/api/v1/inventory/drift", params={"branch_id": str(branch.id)})
    assert drift.status_code == 200
    assert drift.json()["data"]["ok"] is True


async def test_adjust_requires_adjust_permission(client, branch, db_session: AsyncSession):
    """cashier lacks inventory.adjust; the guard fires before any state change."""
    admin_name = await _seed_user(db_session, "super_admin")
    admin_csrf = await _login(client, admin_name)
    med_id = await _make_med(db_session)
    batch = (
        await _receive(client, admin_csrf, branch_id=str(branch.id), medication_id=med_id)
    ).json()["data"]

    # switch identity to a cashier
    cashier_csrf = await _login(client, await _seed_user(db_session, "cashier"))
    r = await client.post(
        f"/api/v1/inventory/batches/{batch['id']}/adjust",
        headers={"X-CSRF-Token": cashier_csrf},
        json={"quantity_delta": "-1", "reason": "x"},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "E-AUTH-002"


# ------------------------------ suppliers + GS1 + rebuild ------------------------------


async def test_supplier_create_list_and_receive_with_supplier(
    admin, branch, db_session: AsyncSession
):
    client, csrf = admin
    made = await client.post(
        "/api/v1/inventory/suppliers",
        headers={"X-CSRF-Token": csrf},
        json={"name": "شركة الأدوية المتحدة"},
    )
    assert made.status_code == 200, made.text
    supplier_id = made.json()["data"]["id"]

    listed = await client.get("/api/v1/inventory/suppliers")
    assert any(s["id"] == supplier_id for s in listed.json()["data"])

    med_id = await _make_med(db_session)
    r = await _receive(
        client, csrf, branch_id=str(branch.id), medication_id=med_id, supplier_id=supplier_id
    )
    assert r.status_code == 200
    assert r.json()["data"]["supplier_id"] == supplier_id


async def test_gs1_prefill_then_receive(admin, branch, db_session: AsyncSession):
    """The receiving flow: scan a 2D GS1 code -> resolve medication -> receive it."""
    client, csrf = admin
    gtin = f"0623{uuid.uuid4().int % 10**10:010d}"
    med_id = await _make_med(db_session, gtin=gtin)

    code = f"01{gtin}17280630" + "10LOT42"
    scan = await client.get("/api/v1/catalog/parse-gs1", params={"code": code})
    assert scan.status_code == 200, scan.text
    parsed = scan.json()["data"]
    assert parsed["medication"]["id"] == med_id
    assert parsed["expiry_date"] == "2028-06-30"
    assert parsed["batch_number"] == "LOT42"

    r = await _receive(
        client,
        csrf,
        branch_id=str(branch.id),
        medication_id=parsed["medication"]["id"],
        expiry=parsed["expiry_date"],
        batch_number=parsed["batch_number"],
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["batch_number"] == "LOT42"


async def test_drift_and_rebuild_endpoints(admin, branch, db_session: AsyncSession):
    from sqlalchemy import text

    client, csrf = admin
    med_id = await _make_med(db_session)
    await _receive(client, csrf, branch_id=str(branch.id), medication_id=med_id, qty="40")

    # sabotage the cache directly, then confirm the API reports drift
    await db_session.execute(
        text(
            "UPDATE branch_inventory SET cached_quantity = 999 "
            "WHERE branch_id = :b AND medication_id = :m"
        ).bindparams(b=branch.id, m=uuid.UUID(med_id))
    )
    await db_session.commit()
    drift = await client.get("/api/v1/inventory/drift", params={"branch_id": str(branch.id)})
    assert drift.json()["data"]["ok"] is False

    rebuilt = await client.post(
        "/api/v1/inventory/rebuild",
        headers={"X-CSRF-Token": csrf},
        json={"branch_id": str(branch.id)},
    )
    assert rebuilt.status_code == 200, rebuilt.text
    after = await client.get("/api/v1/inventory/drift", params={"branch_id": str(branch.id)})
    assert after.json()["data"]["ok"] is True
