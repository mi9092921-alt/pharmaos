"""Inventory integrity fixes (M1 of the stocktake/shortages round).

F1 — adjust_batch on a depleted batch: a positive delta with a valid expiry
reactivates the batch (sellable again via FEFO, cache updated by the
generalized active-window rule). An expired depleted batch stays depleted.

F2 — get_batch(for_update=True): the adjust/status paths lock the batch row
before their absolute-quantity write, matching the sale path's lock order
(batch → branch_inventory). The concurrency test below runs two REAL pooled
connections with forced interleaving (no mocks): a sale blocked on the locked
batch, then the adjust committing first — both effects must survive.
"""

import asyncio
import datetime as dt
import uuid
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.models import (
    Branch,
    Medication,
    MedicationBarcode,
    MedicationBatch,
    MedicationPackaging,
    Role,
    User,
)
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


async def _make_med(db_session: AsyncSession) -> str:
    med = Medication(trade_name=f"FixM1 {uuid.uuid4().hex[:6]}", trade_name_ar="دواء اختبار")
    db_session.add(med)
    await db_session.commit()
    return str(med.id)


async def _receive(client, csrf, *, branch_id, medication_id, qty="50", expiry=FUTURE):
    body = {
        "branch_id": branch_id,
        "medication_id": medication_id,
        "batch_number": f"B-{uuid.uuid4().hex[:6]}",
        "expiry_date": expiry,
        "quantity": qty,
        "purchase_price": "2.50",
    }
    return await client.post("/api/v1/inventory/receive", headers={"X-CSRF-Token": csrf}, json=body)


async def _adjust(client, csrf, batch_id: str, delta: str, reason: str):
    return await client.post(
        f"/api/v1/inventory/batches/{batch_id}/adjust",
        headers={"X-CSRF-Token": csrf},
        json={"quantity_delta": delta, "reason": reason},
    )


async def _cache(db_session: AsyncSession, branch_id: str, medication_id: str) -> int:
    """branch_inventory has no ORM model — the service reads it as raw SQL too."""
    from sqlalchemy import text

    val = (
        await db_session.execute(
            text(
                "SELECT cached_quantity FROM branch_inventory "
                "WHERE branch_id = :b AND medication_id = :m"
            ),
            {"b": branch_id, "m": medication_id},
        )
    ).scalar_one()
    return int(val)


# --------------------------------- F1 ---------------------------------


async def test_adjust_reactivates_depleted_batch_and_cache(admin, branch, db_session: AsyncSession):
    """+5 on a sold-out (depleted) batch: back to active, sellable, cache +5, drift clean."""
    client, csrf = admin
    med_id = await _make_med(db_session)
    bid = str(branch.id)
    batch = (await _receive(client, csrf, branch_id=bid, medication_id=med_id, qty="50")).json()[
        "data"
    ]

    # sell out via adjustment: active -> depleted, cache 0
    out = await _adjust(client, csrf, batch["id"], "-50", "نفاد فعلي")
    assert out.status_code == 200, out.text
    assert out.json()["data"]["status"] == "depleted"
    assert (await _cache(db_session, bid, med_id)) == 0

    # the count-correction brings stock back
    back = await _adjust(client, csrf, batch["id"], "5", "تصحيح جرد")
    assert back.status_code == 200, back.text
    assert back.json()["data"]["status"] == "active"
    assert back.json()["data"]["quantity"] == "5.000"

    # the derived cache follows (it was excluded while depleted, so delta is +5 not 0)
    assert (await _cache(db_session, bid, med_id)) == 5

    # observable through the stock list (sellable) and the ledger invariant holds
    inv = await client.get("/api/v1/inventory", params={"branch_id": bid, "medication_id": med_id})
    mine = next(x for x in inv.json()["data"] if x["medication_id"] == med_id)
    assert mine["cached_quantity"] in ("5.000", "5")
    drift = await client.get("/api/v1/inventory/drift", params={"branch_id": bid})
    assert drift.status_code == 200 and drift.json()["data"]["ok"] is True


async def test_adjust_depleted_expired_batch_stays_depleted(
    admin, branch, db_session: AsyncSession
):
    """A depleted batch whose expiry passed must NOT reactivate (stock is expired).

    Realistic lifecycle: receive 50 -> sell out via adjust (depleted, cache 0)
    -> time passes and the batch expires while depleted -> a count correction
    of +5 moves the batch truth but the batch stays depleted (invisible to
    FEFO) and the cache is untouched."""
    client, csrf = admin
    med_id = await _make_med(db_session)
    bid = str(branch.id)
    batch = (await _receive(client, csrf, branch_id=bid, medication_id=med_id, qty="50")).json()[
        "data"
    ]

    out = await _adjust(client, csrf, batch["id"], "-50", "نفاد فعلي")
    assert out.status_code == 200, out.text
    assert out.json()["data"]["status"] == "depleted"
    assert (await _cache(db_session, bid, med_id)) == 0

    # the batch expires while sitting depleted
    row = (
        await db_session.execute(
            select(MedicationBatch).where(MedicationBatch.id == uuid.UUID(batch["id"]))
        )
    ).scalar_one()
    row.expiry_date = dt.date.today() - dt.timedelta(days=1)
    await db_session.commit()

    r = await _adjust(client, csrf, batch["id"], "5", "تصحيح جرد")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["status"] == "depleted"
    assert r.json()["data"]["quantity"] == "5.000"
    # cache untouched — expired stock is not sellable stock
    assert (await _cache(db_session, bid, med_id)) == 0
    drift = await client.get("/api/v1/inventory/drift", params={"branch_id": bid})
    assert drift.status_code == 200 and drift.json()["data"]["ok"] is True


async def test_adjust_active_positive_delta_cache_unchanged_semantics(
    admin, branch, db_session: AsyncSession
):
    """Regression guard: the generalized rule keeps plain active-batch behavior."""
    client, csrf = admin
    med_id = await _make_med(db_session)
    bid = str(branch.id)
    batch = (await _receive(client, csrf, branch_id=bid, medication_id=med_id, qty="10")).json()[
        "data"
    ]
    r = await _adjust(client, csrf, batch["id"], "7", "زيادة فعلي")
    assert r.status_code == 200
    assert (await _cache(db_session, bid, med_id)) == 17


# --------------------------------- F2 ---------------------------------


async def _actor(db_session: AsyncSession) -> User:
    """A real super_admin row to pass to service-level calls (no HTTP auth)."""
    username = await _seed_user(db_session, "super_admin")
    return (await db_session.execute(select(User).where(User.username == username))).scalar_one()


async def _sellable_setup(
    db_session: AsyncSession, branch: Branch, actor: User
) -> tuple[str, str, str]:
    """Medication + strip barcode + ONE batch of 55 tablets, cache-consistent
    (received through the real receive_batch primitive, not raw ORM)."""
    from sqlalchemy import text as _text

    from pharmaos_api.services import inventory_service

    med = Medication(trade_name=f"FixF2 {uuid.uuid4().hex[:6]}", trade_name_ar="دواء اختبار")
    db_session.add(med)
    await db_session.flush()
    unit_ids = {}
    for name_ar in ("علبة", "شريط", "قرص"):
        unit_ids[name_ar] = (
            await db_session.execute(
                _text(
                    "INSERT INTO units (name_ar) VALUES (:n) "
                    "ON CONFLICT (name_ar) DO UPDATE SET name_ar=EXCLUDED.name_ar RETURNING id"
                ).bindparams(n=name_ar)
            )
        ).scalar_one()
    strip = MedicationPackaging(
        medication_id=med.id,
        level=2,
        unit_id=unit_ids["شريط"],
        name_ar="شريط",
        qty_in_parent=Decimal(10),
        selling_price=Decimal("30.00"),
        is_default_sale=True,
    )
    tablet = MedicationPackaging(
        medication_id=med.id,
        level=3,
        unit_id=unit_ids["قرص"],
        name_ar="قرص",
        # qty_in_parent of the DEEPER level = units per parent: 1 strip = 10 tablets
        qty_in_parent=Decimal(10),
        selling_price=Decimal("3.50"),
    )
    db_session.add_all([strip, tablet])
    await db_session.flush()
    barcode = f"622{uuid.uuid4().int % 10**10:010d}"
    db_session.add(
        MedicationBarcode(
            medication_id=med.id, packaging_id=strip.id, barcode=barcode, is_primary=True
        )
    )
    batch = await inventory_service.receive_batch(
        db_session,
        actor=actor,
        branch_id=branch.id,
        medication_id=med.id,
        batch_number=f"B-{uuid.uuid4().hex[:6]}",
        expiry_date=dt.date.today() + dt.timedelta(days=300),
        quantity=Decimal(55),
        purchase_price=Decimal("2.00"),
    )
    await db_session.commit()
    return barcode, str(batch.id), str(med.id)


async def test_adjust_locked_no_lost_update_vs_concurrent_sale(
    db_session: AsyncSession, branch: Branch
):
    """Two REAL pooled connections: T1 locks the batch, T2's full create_sale
    BLOCKS on the row lock, T1's adjust commits first — final quantity must
    reflect BOTH effects (55 + 5 - 10 = 50). Without the F2 lock, the adjust
    would write its stale absolute value and the sale's -10 would vanish
    (final 60) — that is the lost update this test pins."""
    from sqlalchemy.ext.asyncio import AsyncSession as SessionAlias

    from pharmaos_api.db import get_engine
    from pharmaos_api.services import inventory_service, sales_service
    from pharmaos_api.services.sales_service import SaleLine

    barcode, batch_id, med_id = await _sellable_setup(db_session, branch, await _actor(db_session))

    actor = await _actor(db_session)

    s1 = SessionAlias(get_engine())  # T1: adjust transaction
    s2 = SessionAlias(get_engine())  # T2: concurrent sale
    try:
        locked = await inventory_service.get_batch(s1, uuid.UUID(batch_id), for_update=True)
        assert locked.quantity == 55  # read UNDER the row lock

        sale_task = asyncio.create_task(
            sales_service.create_sale(
                s2,
                branch_id=branch.id,
                lines=[SaleLine(barcode=barcode, quantity=Decimal(1))],  # 1 strip = 10 tablets
                cashier=actor,
            )
        )
        await asyncio.sleep(0.5)
        assert not sale_task.done(), "sale must be blocked on the batch row lock"

        # T1 applies its count correction on the locked read and commits,
        # releasing the lock — T2 then proceeds on the FRESH quantity.
        await inventory_service.adjust_batch(
            s1, actor=actor, batch=locked, quantity_delta=Decimal(5), reason="تصحيح جرد"
        )
        invoice = await sale_task
        assert invoice.total == Decimal("30.00")
    finally:
        await s1.close()
        await s2.close()

    fresh = await inventory_service.get_batch(db_session, uuid.UUID(batch_id))
    assert fresh.quantity == 50, "55 (receive) + 5 (adjust) - 10 (sale): no lost update"
    assert fresh.status == "active"

    bid = str(branch.id)
    assert (await _cache(db_session, bid, med_id)) == 50
    drift_rows = await inventory_service.drift_check(db_session, branch.id)
    assert drift_rows == [], "cache == SUM(active batches) must hold after the race"


# --------------------------------- F3 ---------------------------------


async def test_low_stock_alert_uses_shared_threshold(db_session: AsyncSession, branch: Branch):
    """F3: ONE definition of low stock. An item tracked ONLY by min_stock_level
    (no reorder_point) must fire the low_stock alert AND appear in the
    low-stock list — before the fix the alert ignored min_stock_level."""
    from sqlalchemy import text as _text

    from pharmaos_api.services import alerts_service, inventory_service

    actor = await _actor(db_session)
    med = Medication(trade_name=f"FixF3 {uuid.uuid4().hex[:6]}", trade_name_ar="دواء اختبار")
    db_session.add(med)
    await db_session.flush()
    await inventory_service.receive_batch(
        db_session,
        actor=actor,
        branch_id=branch.id,
        medication_id=med.id,
        batch_number=f"B-{uuid.uuid4().hex[:6]}",
        expiry_date=dt.date.today() + dt.timedelta(days=300),
        quantity=Decimal(10),
        purchase_price=Decimal("2.00"),
    )
    await db_session.commit()
    # track ONLY via min_stock_level (reorder_point stays NULL)
    await db_session.execute(
        _text(
            "UPDATE branch_inventory SET min_stock_level = 15 "
            "WHERE branch_id = :b AND medication_id = :m"
        ).bindparams(b=branch.id, m=med.id)
    )
    await db_session.commit()

    report = await alerts_service.evaluate_branch(db_session, branch.id)
    findings = report["findings"]
    assert isinstance(findings, dict)
    assert findings["low_stock"] == 1, "min_stock_level-only item must alert"

    rows, _total = await inventory_service.list_inventory(
        db_session, branch_id=branch.id, low_stock_only=True
    )
    assert any(r["medication_id"] == str(med.id) for r in rows), "list must agree with the alert"
