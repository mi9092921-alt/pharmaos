"""Inventory integrity fixes (M1 of the stocktake/shortages round).

F1 — adjust_batch on a depleted batch: a positive delta with a valid expiry
reactivates the batch (sellable again via FEFO, cache updated by the
generalized active-window rule). An expired depleted batch stays depleted.
"""

import datetime as dt
import uuid

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.models import Branch, Medication, MedicationBatch, Role, User
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
