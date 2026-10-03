"""P2-M3 — pack serials: 2D capture on receive, duplicate guard (E-TT-002),
dispense linkage to the invoice, and the serials read endpoint.

Serials flow through the SAME receiving primitive for ad-hoc receive and PO
receive; dispensing links scanned serials to the invoice atomically with the sale.
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

FUTURE = (dt.date.today() + dt.timedelta(days=400)).isoformat()


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


async def _make_med(db_session: AsyncSession, *, gtin: str | None = None) -> tuple[str, str]:
    unit_id = await _unit_id(db_session)
    med = Medication(trade_name=f"S {uuid.uuid4().hex[:6]}", trade_name_ar="دواء تسلسل", gtin=gtin)
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


def _gtin() -> str:
    return f"{uuid.uuid4().int % 10**14:014d}"


async def _receive(client, csrf, *, branch_id, medication_id, gtin=None, serials=None, qty="100"):  # type: ignore[no-untyped-def]
    body: dict[str, object] = {
        "branch_id": branch_id,
        "medication_id": medication_id,
        "batch_number": f"B-{uuid.uuid4().hex[:6]}",
        "expiry_date": FUTURE,
        "quantity": qty,
        "purchase_price": "2.00",
    }
    if gtin is not None:
        body["gtin"] = gtin
    if serials is not None:
        body["serials"] = serials
    return await client.post("/api/v1/inventory/receive", headers={"X-CSRF-Token": csrf}, json=body)


# ------------------------------ capture on receive ------------------------------


async def test_capture_on_receive(admin, branch, db_session: AsyncSession):  # type: ignore[no-untyped-def]
    client, csrf = admin
    g = _gtin()
    med, _pkg = await _make_med(db_session, gtin=g)
    u = uuid.uuid4().hex[:8]
    r = await _receive(
        client,
        csrf,
        branch_id=str(branch.id),
        medication_id=med,
        gtin=g,
        serials=[f"S1-{u}", f"S2-{u}"],
    )
    assert r.status_code == 200, r.text
    batch_id = r.json()["data"]["id"]

    ser = await client.get(
        "/api/v1/inventory/serials", params={"branch_id": str(branch.id), "batch_id": batch_id}
    )
    assert ser.status_code == 200, ser.text
    data = ser.json()["data"]
    assert len(data) == 2
    assert all(s["status"] == "in_stock" and s["gtin"] == g for s in data)
    assert {s["serial_number"] for s in data} == {f"S1-{u}", f"S2-{u}"}


async def test_receive_duplicate_serial_rejected(admin, branch, db_session: AsyncSession):  # type: ignore[no-untyped-def]
    client, csrf = admin
    g = _gtin()
    med, _pkg = await _make_med(db_session, gtin=g)
    serial = f"DUP-{uuid.uuid4().hex[:8]}"
    first = await _receive(
        client, csrf, branch_id=str(branch.id), medication_id=med, gtin=g, serials=[serial]
    )
    assert first.status_code == 200, first.text
    dup = await _receive(
        client, csrf, branch_id=str(branch.id), medication_id=med, gtin=g, serials=[serial]
    )
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "E-TT-002"


async def test_gtin_required_when_medication_has_none(admin, branch, db_session: AsyncSession):  # type: ignore[no-untyped-def]
    client, csrf = admin
    med, _pkg = await _make_med(db_session, gtin=None)
    r = await _receive(
        client,
        csrf,
        branch_id=str(branch.id),
        medication_id=med,
        serials=[f"X-{uuid.uuid4().hex[:8]}"],
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "E-VAL-001"


# ------------------------------ dispense linkage ------------------------------


async def test_dispense_links_serial_to_invoice(admin, branch, db_session: AsyncSession):  # type: ignore[no-untyped-def]
    client, csrf = admin
    g = _gtin()
    med, pkg = await _make_med(db_session, gtin=g)
    serial = f"D-{uuid.uuid4().hex[:8]}"
    assert (
        await _receive(
            client,
            csrf,
            branch_id=str(branch.id),
            medication_id=med,
            gtin=g,
            serials=[serial],
            qty="100",
        )
    ).status_code == 200

    sale = await client.post(
        "/api/v1/pos/sale",
        headers={"X-CSRF-Token": csrf},
        json={
            "branch_id": str(branch.id),
            "lines": [{"medication_id": med, "packaging_id": pkg, "quantity": "1"}],
            "payment_method": "cash",
            "serials": [serial],
        },
    )
    assert sale.status_code == 200, sale.text
    invoice_id = sale.json()["data"]["invoice_id"]

    ser = await client.get(
        "/api/v1/inventory/serials", params={"branch_id": str(branch.id), "status": "dispensed"}
    )
    mine = [s for s in ser.json()["data"] if s["serial_number"] == serial]
    assert mine and mine[0]["status"] == "dispensed"
    assert mine[0]["dispensed_invoice_id"] == invoice_id


async def test_dispense_rejects_serial_from_wrong_batch(admin, branch, db_session: AsyncSession):  # type: ignore[no-untyped-def]
    """Review C1: a scanned serial must come from a batch this sale actually
    dispensed. FEFO picks the earlier-expiry batch; scanning a pack from the
    other (later-expiry) batch is rejected with E-TT-003."""
    client, csrf = admin
    g = _gtin()
    med, pkg = await _make_med(db_session, gtin=g)
    near, far = uuid.uuid4().hex[:8], uuid.uuid4().hex[:8]
    early = (dt.date.today() + dt.timedelta(days=60)).isoformat()
    late = (dt.date.today() + dt.timedelta(days=400)).isoformat()

    # Batch A (earlier expiry — FEFO picks this) with serial S_near.
    a = await client.post(
        "/api/v1/inventory/receive",
        headers={"X-CSRF-Token": csrf},
        json={
            "branch_id": str(branch.id),
            "medication_id": med,
            "batch_number": f"A-{near}",
            "expiry_date": early,
            "quantity": "5",
            "purchase_price": "2.00",
            "gtin": g,
            "serials": [f"NEAR-{near}"],
        },
    )
    assert a.status_code == 200, a.text
    # Batch B (later expiry) with serial S_far.
    b = await client.post(
        "/api/v1/inventory/receive",
        headers={"X-CSRF-Token": csrf},
        json={
            "branch_id": str(branch.id),
            "medication_id": med,
            "batch_number": f"B-{far}",
            "expiry_date": late,
            "quantity": "5",
            "purchase_price": "2.00",
            "gtin": g,
            "serials": [f"FAR-{far}"],
        },
    )
    assert b.status_code == 200, b.text

    # Sell 1 — FEFO dispenses batch A; scanning the batch-B pack must be rejected.
    mismatch = await client.post(
        "/api/v1/pos/sale",
        headers={"X-CSRF-Token": csrf},
        json={
            "branch_id": str(branch.id),
            "lines": [{"medication_id": med, "packaging_id": pkg, "quantity": "1"}],
            "payment_method": "cash",
            "serials": [f"FAR-{far}"],
        },
    )
    assert mismatch.status_code == 422
    assert mismatch.json()["error"]["code"] == "E-TT-003"

    # Scanning the batch-A pack (the one FEFO dispensed) succeeds.
    ok = await client.post(
        "/api/v1/pos/sale",
        headers={"X-CSRF-Token": csrf},
        json={
            "branch_id": str(branch.id),
            "lines": [{"medication_id": med, "packaging_id": pkg, "quantity": "1"}],
            "payment_method": "cash",
            "serials": [f"NEAR-{near}"],
        },
    )
    assert ok.status_code == 200, ok.text


async def test_sale_rejects_unknown_serial(admin, branch, db_session: AsyncSession):  # type: ignore[no-untyped-def]
    client, csrf = admin
    med, pkg = await _make_med(db_session, gtin=_gtin())
    assert (
        await _receive(client, csrf, branch_id=str(branch.id), medication_id=med, qty="50")
    ).status_code == 200

    sale = await client.post(
        "/api/v1/pos/sale",
        headers={"X-CSRF-Token": csrf},
        json={
            "branch_id": str(branch.id),
            "lines": [{"medication_id": med, "packaging_id": pkg, "quantity": "1"}],
            "payment_method": "cash",
            "serials": [f"NOPE-{uuid.uuid4().hex[:8]}"],
        },
    )
    assert sale.status_code == 422
    assert sale.json()["error"]["code"] == "E-VAL-001"


# ------------------------------ capture on PO receive ------------------------------


async def test_capture_on_po_receive(admin, branch, db_session: AsyncSession):  # type: ignore[no-untyped-def]
    client, csrf = admin
    supplier_id = (
        await client.post(
            "/api/v1/purchases/suppliers",
            headers={"X-CSRF-Token": csrf},
            json={"name": f"مورّد {uuid.uuid4().hex[:6]}"},
        )
    ).json()["data"]["id"]
    g = _gtin()
    med, pkg = await _make_med(db_session, gtin=g)
    serial = f"PO-{uuid.uuid4().hex[:8]}"

    po = (
        await client.post(
            "/api/v1/purchases/orders",
            headers={"X-CSRF-Token": csrf},
            json={
                "branch_id": str(branch.id),
                "supplier_id": supplier_id,
                "lines": [
                    {
                        "medication_id": med,
                        "packaging_id": pkg,
                        "qty_ordered": "10",
                        "unit_cost": "1",
                    }
                ],
            },
        )
    ).json()["data"]
    po_id, item_id = po["id"], po["items"][0]["id"]
    await client.post(f"/api/v1/purchases/orders/{po_id}/submit", headers={"X-CSRF-Token": csrf})
    await client.post(f"/api/v1/purchases/orders/{po_id}/approve", headers={"X-CSRF-Token": csrf})
    rec = await client.post(
        f"/api/v1/purchases/orders/{po_id}/receive",
        headers={"X-CSRF-Token": csrf},
        json={
            "receipts": [
                {
                    "purchase_item_id": item_id,
                    "batch_number": "POB-1",
                    "expiry_date": FUTURE,
                    "quantity": "10",
                    "gtin": g,
                    "serials": [serial],
                }
            ]
        },
    )
    assert rec.status_code == 200, rec.text

    ser = await client.get(
        "/api/v1/inventory/serials", params={"branch_id": str(branch.id), "status": "in_stock"}
    )
    assert any(s["serial_number"] == serial and s["gtin"] == g for s in ser.json()["data"])
