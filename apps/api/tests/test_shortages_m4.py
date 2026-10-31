"""Shortages module (M4 of the stocktake/shortages round): dedupe, auto-sync
anti-resurrection (the owner's re-arm rule), auto-close on receiving,
purchase-order wiring, substitutes with the strength warning, permissions.

Concurrency-safe dedupe rides on the partial unique indexes; the re-arm rule
is exercised END-TO-END through the real receive_batch hook.
"""

import datetime as dt
import uuid
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.models import (
    Branch,
    Medication,
    MedicationBatch,
    MedicationPackaging,
    Role,
    ShortageItem,
    User,
)
from pharmaos_api.security.passwords import hash_password

FUTURE = (dt.date.today() + dt.timedelta(days=365)).isoformat()
SH = "/api/v1/shortages"


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


async def _actor(db_session: AsyncSession) -> User:
    username = await _seed_user(db_session, "super_admin")
    return (await db_session.execute(select(User).where(User.username == username))).scalar_one()


async def _make_med(db_session: AsyncSession, name: str, *, sci: str | None = None) -> Medication:
    med = Medication(
        trade_name=f"{name} {uuid.uuid4().hex[:6]}",
        trade_name_ar=f"دواء {name}",
        scientific_name=sci,
    )
    db_session.add(med)
    await db_session.commit()
    return med


async def _receive(
    db_session: AsyncSession,
    branch: Branch,
    med: Medication,
    qty: str,
    *,
    price: str = "2.00",
) -> MedicationBatch:
    from pharmaos_api.services import inventory_service

    actor = await _actor(db_session)
    batch = await inventory_service.receive_batch(
        db_session,
        actor=actor,
        branch_id=branch.id,
        medication_id=med.id,
        batch_number=f"B-{uuid.uuid4().hex[:6]}",
        expiry_date=dt.date.today() + dt.timedelta(days=300),
        quantity=Decimal(qty),
        purchase_price=Decimal(price),
    )
    await db_session.commit()
    return batch


async def _set_threshold(db_session: AsyncSession, branch: Branch, med: Medication, **kw) -> None:
    """Ensure the cache row exists (update_stock_settings creates a 0-qty row
    for known-but-never-received items) then set thresholds."""
    from pharmaos_api.services import inventory_service

    actor = await _actor(db_session)
    await inventory_service.update_stock_settings(
        db_session,
        actor=actor,
        branch_id=branch.id,
        medication_id=med.id,
        min_stock_level=(Decimal(str(kw["min_stock_level"])) if "min_stock_level" in kw else None),
        reorder_point=Decimal(str(kw["reorder_point"])) if "reorder_point" in kw else None,
    )
    await db_session.commit()


async def _sync(client, csrf, branch_id):
    return await client.post(
        f"{SH}/sync", params={"branch_id": branch_id}, headers={"X-CSRF-Token": csrf}
    )


async def _rows(db_session: AsyncSession, branch: Branch) -> list[ShortageItem]:
    # HTTP mutations commit in OTHER sessions — populate_existing refreshes
    # the identity map's stale objects DURING the query (in greenlet context)
    return list(
        (
            await db_session.execute(
                select(ShortageItem)
                .where(ShortageItem.branch_id == branch.id)
                .order_by(ShortageItem.created_at)
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )


# ------------------------------ create + dedupe ------------------------------


async def test_create_and_partial_unique_dedupe(admin, branch, db_session: AsyncSession):
    client, csrf = admin
    med = await _make_med(db_session, "Dedupe")
    body = {
        "branch_id": str(branch.id),
        "kind": "pharmacy",
        "source": "manual",
        "medication_id": str(med.id),
        "qty_needed": "3",
    }
    r1 = await client.post(SH, headers={"X-CSRF-Token": csrf}, json=body)
    assert r1.status_code == 200, r1.text
    r2 = await client.post(SH, headers={"X-CSRF-Token": csrf}, json=body)
    assert r2.status_code == 422
    assert r2.json()["error"]["code"] == "E-SRT-001"

    # free-text market item + customer
    market = {
        "branch_id": str(branch.id),
        "kind": "market",
        "source": "customer_request",
        "name_text": "شكلوفة نادرة",
        "customer_name": "أم أحمد",
        "customer_phone": "01000000000",
    }
    rm = await client.post(SH, headers={"X-CSRF-Token": csrf}, json=market)
    assert rm.status_code == 200, rm.text
    rm2 = await client.post(SH, headers={"X-CSRF-Token": csrf}, json=market)
    assert rm2.status_code == 422  # name-level dedupe for market items


async def test_sync_requires_edit_permission(admin, branch, db_session: AsyncSession):
    client, _ = admin
    # cashier has inventory.view but NOT inventory.edit
    async with await _new_client() as cashier_client:
        cashier_csrf = await _login(cashier_client, await _seed_user(db_session, "cashier"))
        r = await _sync(cashier_client, cashier_csrf, str(branch.id))
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "E-AUTH-002"


async def _new_client():
    import httpx

    from pharmaos_api.main import create_app

    transport = httpx.ASGITransport(app=create_app(license_scheduler=False))
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


# ------------------------------ sync + re-arm ------------------------------


async def test_sync_auto_creates_and_never_resurrects_cancelled(
    admin, branch, db_session: AsyncSession
):
    """THE owner rule: cancelled/unavailable auto rows block re-adding until
    stock RECOVERS (receive passes the threshold → re-arm → next drop re-adds)."""
    client, csrf = admin
    med = await _make_med(db_session, "Resurrect")
    # a KNOWN-BUT-EMPTY cache row (settings create rows with quantity 0)
    await _set_threshold(db_session, branch, med, min_stock_level=5)
    bid = str(branch.id)

    r = await _sync(client, csrf, bid)
    assert r.status_code == 200, r.text
    rows = await _rows(db_session, branch)
    assert len(rows) == 1 and rows[0].source == "auto_out_of_stock"
    assert rows[0].status == "open"

    # idempotent: second sync changes nothing
    await _sync(client, csrf, bid)
    assert len(await _rows(db_session, branch)) == 1

    # staff cancels it → suppression
    item = (await _rows(db_session, branch))[0]
    rc = await client.patch(
        f"{SH}/{item.id}",
        params={"branch_id": bid},
        headers={"X-CSRF-Token": csrf},
        json={"status": "cancelled"},
    )
    assert rc.status_code == 200, rc.text
    await _sync(client, csrf, bid)
    rows = await _rows(db_session, branch)
    assert len(rows) == 1 and rows[0].status == "cancelled", "must NOT resurrect"

    # stock RECOVERS through the real receive hook → re-arm clears suppression
    await _receive(db_session, branch, med, "10")
    item = (await _rows(db_session, branch))[0]
    await db_session.refresh(item)
    assert item.suppress_auto_resync is False, "recovery clears the suppression"

    # ...and the quantity drops again → sync re-adds (recovered, then dropped)
    from pharmaos_api.services import inventory_service

    actor = await _actor(db_session)
    batch = (
        await db_session.execute(
            select(MedicationBatch).where(MedicationBatch.medication_id == med.id)
        )
    ).scalar_one()
    await inventory_service.adjust_batch(
        db_session, actor=actor, batch=batch, quantity_delta=Decimal(-10), reason="صرف خارجي"
    )
    await _sync(client, csrf, bid)
    rows = await _rows(db_session, branch)
    assert any(r.status == "open" and r.source == "auto_out_of_stock" for r in rows)


async def test_receive_auto_closes_and_notifies_customer(admin, branch, db_session: AsyncSession):
    """Receiving past the threshold closes open shortage rows (re-armed) and
    notifies the waiting customer in-app. Threshold-0 items close on ANY qty."""
    client, csrf = admin
    med = await _make_med(db_session, "Arrive")
    await _set_threshold(db_session, branch, med, min_stock_level=5)  # cache row qty 0
    bid = str(branch.id)

    # customer-request pharmacy row (waiting customer)
    r = await client.post(
        SH,
        headers={"X-CSRF-Token": csrf},
        json={
            "branch_id": bid,
            "kind": "pharmacy",
            "source": "customer_request",
            "medication_id": str(med.id),
            "customer_name": "عميل منتظر",
        },
    )
    assert r.status_code == 200, r.text

    # receive 10 → threshold 5 passed → the row closes + notification
    await _receive(db_session, branch, med, "10")
    rows = await _rows(db_session, branch)
    assert rows[0].status == "received" and rows[0].received_at is not None
    assert rows[0].suppress_auto_resync is False

    notes = (
        await db_session.execute(
            text("SELECT title_key FROM notifications WHERE branch_id = :b").bindparams(b=branch.id)
        )
    ).all()
    assert any("shortages.arrived" in str(n[0]) for n in notes)

    # threshold-0 (untracked thresholds) item: ANY quantity closes it
    med2 = await _make_med(db_session, "ZeroThr")
    r2 = await client.post(
        SH,
        headers={"X-CSRF-Token": csrf},
        json={
            "branch_id": bid,
            "kind": "pharmacy",
            "source": "manual",
            "medication_id": str(med2.id),
        },
    )
    assert r2.status_code == 200
    await _receive(db_session, branch, med2, "3")
    rows2 = [x for x in await _rows(db_session, branch) if x.medication_id == med2.id]
    assert rows2[0].status == "received"


# ------------------------------ PO wiring ------------------------------


async def test_create_po_from_shortages(admin, branch, db_session: AsyncSession):
    client, csrf = admin
    med = await _make_med(db_session, "PoItem")
    packaging = MedicationPackaging(
        medication_id=med.id,
        level=3,
        unit_id=await _unit_id(db_session, "قرص"),
        name_ar="قرص",
        qty_in_parent=Decimal(1),
        selling_price=Decimal("3.50"),
    )
    db_session.add(packaging)
    await db_session.commit()
    await _receive(db_session, branch, med, "10", price="4.50")  # last cost source

    sup = await client.post(
        "/api/v1/inventory/suppliers", headers={"X-CSRF-Token": csrf}, json={"name": "مورد الجرد"}
    )
    supplier_id = sup.json()["data"]["id"]

    r = await client.post(
        SH,
        headers={"X-CSRF-Token": csrf},
        json={
            "branch_id": str(branch.id),
            "kind": "pharmacy",
            "source": "manual",
            "medication_id": str(med.id),
            "qty_needed": "20",
        },
    )
    item_id = r.json()["data"]["id"]

    po = await client.post(
        f"{SH}/create-po",
        headers={"X-CSRF-Token": csrf},
        json={
            "branch_id": str(branch.id),
            "ids": [item_id],
            "supplier_id": supplier_id,
        },
    )
    assert po.status_code == 200, po.text
    data = po.json()["data"]
    assert data["items"] == 1 and data["po_number"].startswith("PO-")

    item = (
        await db_session.execute(select(ShortageItem).where(ShortageItem.id == uuid.UUID(item_id)))
    ).scalar_one()
    await db_session.refresh(item)
    assert item.status == "ordered" and str(item.purchase_order_id) == data["po_id"]
    assert str(item.supplier_id) == supplier_id


async def _unit_id(db_session: AsyncSession, name_ar: str):
    from sqlalchemy import text as _t

    return (
        await db_session.execute(
            _t(
                "INSERT INTO units (name_ar) VALUES (:n) "
                "ON CONFLICT (name_ar) DO UPDATE SET name_ar=EXCLUDED.name_ar RETURNING id"
            ).bindparams(n=name_ar)
        )
    ).scalar_one()


# ------------------------------ substitutes ------------------------------


async def test_substitutes_same_active_ingredient_with_warning(
    admin, branch, db_session: AsyncSession
):
    client, csrf = admin
    main = await _make_med(db_session, "Main", sci="PARACETAMOL-ISO")
    alt = await _make_med(db_session, "Alt", sci="PARACETAMOL-ISO")
    await _receive(db_session, branch, alt, "30")

    r = await client.post(
        SH,
        headers={"X-CSRF-Token": csrf},
        json={
            "branch_id": str(branch.id),
            "kind": "pharmacy",
            "source": "manual",
            "medication_id": str(main.id),
        },
    )
    item_id = r.json()["data"]["id"]

    subs = await client.get(f"{SH}/{item_id}/substitutes", params={"branch_id": str(branch.id)})
    assert subs.status_code == 200, subs.text
    data = subs.json()["data"]
    assert data["warning_key"] == "shortages.substitute_warning"
    names = [x["medication_id"] for x in data["items"]]
    assert str(alt.id) in names


# ------------------------------ bulk + export ------------------------------


async def test_bulk_and_csv_export_injection_safe(admin, branch, db_session: AsyncSession):
    client, csrf = admin
    r1 = await client.post(
        SH,
        headers={"X-CSRF-Token": csrf},
        json={
            "branch_id": str(branch.id),
            "kind": "market",
            "source": "customer_request",
            "name_text": "=cmd|formula",
        },
    )
    r2 = await client.post(
        SH,
        headers={"X-CSRF-Token": csrf},
        json={
            "branch_id": str(branch.id),
            "kind": "market",
            "source": "manual",
            "name_text": "@danger",
        },
    )
    id1 = r1.json()["data"]["id"]
    id2 = r2.json()["data"]["id"]

    bulk = await client.post(
        f"{SH}/bulk",
        headers={"X-CSRF-Token": csrf},
        json={"ids": [id1, id2], "action": "mark_unavailable"},
    )
    assert bulk.status_code == 200, bulk.text
    rows = await _rows(db_session, branch)
    assert all(x.status == "unavailable" and x.suppress_auto_resync for x in rows)

    csv_r = await client.get(f"{SH}/export.csv", params={"branch_id": str(branch.id)})
    assert csv_r.status_code == 200
    for line in csv_r.text.splitlines():
        for cell in line.split(","):
            assert not cell.startswith(("=", "+", "-", "@")), f"unescaped cell: {cell!r}"

    no_csrf = await client.post(f"{SH}/bulk", json={"ids": [id1], "action": "cancel"})
    assert no_csrf.status_code == 403 and no_csrf.json()["error"]["code"] == "E-AUTH-004"
