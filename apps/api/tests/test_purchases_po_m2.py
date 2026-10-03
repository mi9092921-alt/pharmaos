"""P2-M2 — purchase-order lifecycle HTTP tests (request -> approve -> receive).

Exercises the state machine, the permission tiers (create/approve/receive),
CSRF, server-side totals, and that receiving flows through the inventory ledger
(a batch + purchase_in movement + derived-cache delta, drift-free).
"""

import datetime as dt
import uuid
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.models import Branch, Medication, MedicationPackaging, Role, User
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
async def admin(client: httpx.AsyncClient, db_session: AsyncSession):  # type: ignore[no-untyped-def]
    csrf = await _login(client, await _seed_user(db_session, "super_admin"))
    return client, csrf


@pytest.fixture
async def branch(db_session: AsyncSession) -> Branch:
    b = Branch(name=f"فرع {uuid.uuid4().hex[:6]}", country_code="EG", currency_code="EGP")
    db_session.add(b)
    await db_session.commit()
    return b


async def _unit_id(db_session: AsyncSession) -> uuid.UUID:
    uid = (
        await db_session.execute(text("SELECT id FROM units WHERE NOT is_deleted LIMIT 1"))
    ).scalar_one_or_none()
    if uid is None:
        uid = (
            await db_session.execute(
                text("INSERT INTO units (name_ar) VALUES (:n) RETURNING id").bindparams(
                    n=f"وحدة {uuid.uuid4().hex[:6]}"
                )
            )
        ).scalar_one()
        await db_session.commit()
    return uid


async def _make_med(db_session: AsyncSession) -> tuple[str, str]:
    """A medication with one packaging level; returns (medication_id, packaging_id)."""
    unit_id = await _unit_id(db_session)
    med = Medication(trade_name=f"PO {uuid.uuid4().hex[:6]}", trade_name_ar="دواء شراء")
    db_session.add(med)
    await db_session.flush()
    pkg = MedicationPackaging(
        medication_id=med.id,
        level=1,
        unit_id=unit_id,
        name_ar="علبة",
        selling_price=Decimal("10.00"),
        is_default_sale=True,
    )
    db_session.add(pkg)
    await db_session.commit()
    return str(med.id), str(pkg.id)


async def _make_supplier(client: httpx.AsyncClient, csrf: str) -> str:
    r = await client.post(
        "/api/v1/purchases/suppliers",
        headers={"X-CSRF-Token": csrf},
        json={"name": f"مورّد {uuid.uuid4().hex[:6]}"},
    )
    assert r.status_code == 200, r.text
    return r.json()["data"]["id"]


async def _create_po(client, csrf, *, branch_id, supplier_id, lines):  # type: ignore[no-untyped-def]
    return await client.post(
        "/api/v1/purchases/orders",
        headers={"X-CSRF-Token": csrf},
        json={"branch_id": branch_id, "supplier_id": supplier_id, "lines": lines},
    )


async def _post(client, csrf, path):  # type: ignore[no-untyped-def]
    return await client.post(path, headers={"X-CSRF-Token": csrf})


# ------------------------------ create / totals ------------------------------


async def test_create_draft_computes_totals(admin, branch, db_session: AsyncSession):  # type: ignore[no-untyped-def]
    client, csrf = admin
    supplier_id = await _make_supplier(client, csrf)
    med1, pkg1 = await _make_med(db_session)
    med2, pkg2 = await _make_med(db_session)
    r = await _create_po(
        client,
        csrf,
        branch_id=str(branch.id),
        supplier_id=supplier_id,
        lines=[
            {
                "medication_id": med1,
                "packaging_id": pkg1,
                "qty_ordered": "100",
                "unit_cost": "2.50",
            },
            {"medication_id": med2, "packaging_id": pkg2, "qty_ordered": "50", "unit_cost": "4.00"},
        ],
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["status"] == "draft"
    assert data["po_number"].startswith("PO-")
    assert data["subtotal"] == "450.00" and data["total"] == "450.00"
    assert data["currency_code"] == "EGP"
    assert len(data["items"]) == 2


async def test_create_rejects_unknown_supplier(admin, branch, db_session: AsyncSession):  # type: ignore[no-untyped-def]
    client, csrf = admin
    med, pkg = await _make_med(db_session)
    r = await _create_po(
        client,
        csrf,
        branch_id=str(branch.id),
        supplier_id=str(uuid.uuid4()),
        lines=[{"medication_id": med, "packaging_id": pkg, "qty_ordered": "5", "unit_cost": "1"}],
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "E-VAL-001"


async def test_create_needs_csrf(admin, branch, db_session: AsyncSession):  # type: ignore[no-untyped-def]
    client, csrf = admin
    supplier_id = await _make_supplier(client, csrf)
    med, pkg = await _make_med(db_session)
    r = await client.post(
        "/api/v1/purchases/orders",
        json={
            "branch_id": str(branch.id),
            "supplier_id": supplier_id,
            "lines": [
                {"medication_id": med, "packaging_id": pkg, "qty_ordered": "5", "unit_cost": "1"}
            ],
        },
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "E-AUTH-004"


# ------------------------------ lifecycle + receiving ------------------------------


async def test_full_lifecycle_receive_updates_inventory(admin, branch, db_session: AsyncSession):  # type: ignore[no-untyped-def]
    client, csrf = admin
    supplier_id = await _make_supplier(client, csrf)
    med, pkg = await _make_med(db_session)
    po = (
        await _create_po(
            client,
            csrf,
            branch_id=str(branch.id),
            supplier_id=supplier_id,
            lines=[
                {"medication_id": med, "packaging_id": pkg, "qty_ordered": "100", "unit_cost": "2"}
            ],
        )
    ).json()["data"]
    po_id, item_id = po["id"], po["items"][0]["id"]

    # draft -> submit -> approve
    assert (await _post(client, csrf, f"/api/v1/purchases/orders/{po_id}/submit")).json()["data"][
        "status"
    ] == "pending_approval"
    assert (await _post(client, csrf, f"/api/v1/purchases/orders/{po_id}/approve")).json()["data"][
        "status"
    ] == "approved"

    # partial receipt (40 of 100) -> partially_received, inventory reflects 40
    r = await client.post(
        f"/api/v1/purchases/orders/{po_id}/receive",
        headers={"X-CSRF-Token": csrf},
        json={
            "receipts": [
                {
                    "purchase_item_id": item_id,
                    "batch_number": "B-1",
                    "expiry_date": FUTURE,
                    "quantity": "40",
                }
            ]
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["status"] == "partially_received"
    assert r.json()["data"]["items"][0]["qty_received"] == "40.000"

    inv = await client.get("/api/v1/inventory", params={"branch_id": str(branch.id)})
    mine = next(x for x in inv.json()["data"] if x["medication_id"] == med)
    assert mine["cached_quantity"] in ("40.000", "40")

    # receive the remaining 60 -> received
    r2 = await client.post(
        f"/api/v1/purchases/orders/{po_id}/receive",
        headers={"X-CSRF-Token": csrf},
        json={
            "receipts": [
                {
                    "purchase_item_id": item_id,
                    "batch_number": "B-2",
                    "expiry_date": FUTURE,
                    "quantity": "60",
                }
            ]
        },
    )
    assert r2.json()["data"]["status"] == "received"
    assert r2.json()["data"]["items"][0]["qty_received"] == "100.000"

    # two batches created, linked to the PO supplier; cache is drift-free
    batches = await client.get(
        "/api/v1/inventory/batches", params={"branch_id": str(branch.id), "medication_id": med}
    )
    rows = batches.json()["data"]
    assert len(rows) == 2 and all(b["supplier_id"] == supplier_id for b in rows)
    drift = await client.get("/api/v1/inventory/drift", params={"branch_id": str(branch.id)})
    assert drift.json()["data"]["ok"] is True


async def test_partially_received_po_can_be_cancelled(admin, branch, db_session: AsyncSession):  # type: ignore[no-untyped-def]
    """Review D4: a partially-received PO can be cancelled to close out the
    undelivered remainder; the stock already received stays booked."""
    client, csrf = admin
    supplier_id = await _make_supplier(client, csrf)
    med, pkg = await _make_med(db_session)
    po = (
        await _create_po(
            client,
            csrf,
            branch_id=str(branch.id),
            supplier_id=supplier_id,
            lines=[
                {"medication_id": med, "packaging_id": pkg, "qty_ordered": "100", "unit_cost": "2"}
            ],
        )
    ).json()["data"]
    po_id, item_id = po["id"], po["items"][0]["id"]
    await _post(client, csrf, f"/api/v1/purchases/orders/{po_id}/submit")
    await _post(client, csrf, f"/api/v1/purchases/orders/{po_id}/approve")
    r = await client.post(
        f"/api/v1/purchases/orders/{po_id}/receive",
        headers={"X-CSRF-Token": csrf},
        json={
            "receipts": [
                {
                    "purchase_item_id": item_id,
                    "batch_number": "B-1",
                    "expiry_date": FUTURE,
                    "quantity": "40",
                }
            ]
        },
    )
    assert r.json()["data"]["status"] == "partially_received"

    # Close out the remaining 60 by cancelling.
    cancelled = await _post(client, csrf, f"/api/v1/purchases/orders/{po_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["data"]["status"] == "cancelled"

    # The 40 already received remain in inventory (one batch).
    inv = await client.get("/api/v1/inventory", params={"branch_id": str(branch.id)})
    mine = next(x for x in inv.json()["data"] if x["medication_id"] == med)
    assert mine["cached_quantity"] in ("40.000", "40")


async def test_invalid_transitions(admin, branch, db_session: AsyncSession):  # type: ignore[no-untyped-def]
    client, csrf = admin
    supplier_id = await _make_supplier(client, csrf)
    med, pkg = await _make_med(db_session)
    po = (
        await _create_po(
            client,
            csrf,
            branch_id=str(branch.id),
            supplier_id=supplier_id,
            lines=[
                {"medication_id": med, "packaging_id": pkg, "qty_ordered": "10", "unit_cost": "1"}
            ],
        )
    ).json()["data"]
    po_id, item_id = po["id"], po["items"][0]["id"]

    # cannot approve a draft, cannot receive before approval
    assert (
        await _post(client, csrf, f"/api/v1/purchases/orders/{po_id}/approve")
    ).status_code == 422
    rec = await client.post(
        f"/api/v1/purchases/orders/{po_id}/receive",
        headers={"X-CSRF-Token": csrf},
        json={
            "receipts": [
                {
                    "purchase_item_id": item_id,
                    "batch_number": "X",
                    "expiry_date": FUTURE,
                    "quantity": "1",
                }
            ]
        },
    )
    assert rec.status_code == 422
    # submit twice -> second is invalid
    assert (
        await _post(client, csrf, f"/api/v1/purchases/orders/{po_id}/submit")
    ).status_code == 200
    assert (
        await _post(client, csrf, f"/api/v1/purchases/orders/{po_id}/submit")
    ).status_code == 422


async def test_cancel_paths(admin, branch, db_session: AsyncSession):  # type: ignore[no-untyped-def]
    client, csrf = admin
    supplier_id = await _make_supplier(client, csrf)
    med, pkg = await _make_med(db_session)
    po = (
        await _create_po(
            client,
            csrf,
            branch_id=str(branch.id),
            supplier_id=supplier_id,
            lines=[
                {"medication_id": med, "packaging_id": pkg, "qty_ordered": "10", "unit_cost": "1"}
            ],
        )
    ).json()["data"]
    po_id = po["id"]
    assert (await _post(client, csrf, f"/api/v1/purchases/orders/{po_id}/cancel")).json()["data"][
        "status"
    ] == "cancelled"
    # cannot submit a cancelled PO
    assert (
        await _post(client, csrf, f"/api/v1/purchases/orders/{po_id}/submit")
    ).status_code == 422


# ------------------------------ permissions ------------------------------


async def test_permission_tiers(client, branch, db_session: AsyncSession):  # type: ignore[no-untyped-def]
    # admin sets up an approved PO
    admin_csrf = await _login(client, await _seed_user(db_session, "super_admin"))
    supplier_id = await _make_supplier(client, admin_csrf)
    med, pkg = await _make_med(db_session)
    po = (
        await _create_po(
            client,
            admin_csrf,
            branch_id=str(branch.id),
            supplier_id=supplier_id,
            lines=[
                {"medication_id": med, "packaging_id": pkg, "qty_ordered": "10", "unit_cost": "1"}
            ],
        )
    ).json()["data"]
    po_id, item_id = po["id"], po["items"][0]["id"]
    await _post(client, admin_csrf, f"/api/v1/purchases/orders/{po_id}/submit")
    await _post(client, admin_csrf, f"/api/v1/purchases/orders/{po_id}/approve")

    # pharmacist: view yes, create no, approve no, receive YES
    ph_csrf = await _login(client, await _seed_user(db_session, "pharmacist"))
    assert (await client.get("/api/v1/purchases/orders")).status_code == 200
    denied_create = await _create_po(
        client,
        ph_csrf,
        branch_id=str(branch.id),
        supplier_id=supplier_id,
        lines=[{"medication_id": med, "packaging_id": pkg, "qty_ordered": "1", "unit_cost": "1"}],
    )
    assert (
        denied_create.status_code == 403 and denied_create.json()["error"]["code"] == "E-AUTH-002"
    )
    # a fresh approved PO to receive against (pharmacist cannot approve, so admin already did above)
    rec = await client.post(
        f"/api/v1/purchases/orders/{po_id}/receive",
        headers={"X-CSRF-Token": ph_csrf},
        json={
            "receipts": [
                {
                    "purchase_item_id": item_id,
                    "batch_number": "PH-1",
                    "expiry_date": FUTURE,
                    "quantity": "10",
                }
            ]
        },
    )
    assert rec.status_code == 200, rec.text
    assert rec.json()["data"]["status"] == "received"

    # cashier: not even view
    await _login(client, await _seed_user(db_session, "cashier"))
    assert (await client.get("/api/v1/purchases/orders")).status_code == 403
