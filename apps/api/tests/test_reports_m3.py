"""Expiry & waste analytics (P3-M3): buckets (reused from expiry_alerts),
waste value swept in a date range, the forward weekly trend, and
expired/locked capital (reused from batch_status_report).

test_expiry_waste_value_from_swept_batches is the load-bearing test here: it
directly proves the waste-value design is NOT built on the (wrong-by-analogy)
assumption that expiry_writeoff's quantity_delta is negative like sale_out's —
expiry_sweep writes it as exactly 0 (verified from its own source before this
report was designed), so waste value has to come from the swept batches'
still-present `quantity`, not from summing quantity_delta. A test using the
wrong assumption would have silently asserted "0.00" and still passed.
"""

import datetime as dt
import uuid
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.errors import ApiError
from pharmaos_api.models import Branch, Medication, MedicationBatch, Role, User
from pharmaos_api.services import inventory_service


@pytest.fixture
async def actor(db_session: AsyncSession, seeded_user: dict) -> User:
    return (
        await db_session.execute(select(User).where(User.username == seeded_user["username"]))
    ).scalar_one()


@pytest.fixture
async def branch(db_session: AsyncSession) -> Branch:
    b = Branch(name=f"فرع {uuid.uuid4().hex[:6]}", country_code="EG", currency_code="EGP")
    db_session.add(b)
    await db_session.commit()
    return b


async def _make_med(db_session: AsyncSession) -> uuid.UUID:
    med = Medication(trade_name=f"M3Med {uuid.uuid4().hex[:6]}", trade_name_ar="دواء التقرير")
    db_session.add(med)
    await db_session.commit()
    return med.id


async def _receive(
    db_session: AsyncSession,
    actor: User,
    branch: Branch,
    med_id: uuid.UUID,
    *,
    days: int,
    qty: str,
    price: str = "2.00",
) -> MedicationBatch:
    return await inventory_service.receive_stock(
        db_session,
        actor=actor,
        branch_id=branch.id,
        medication_id=med_id,
        batch_number=f"B-{uuid.uuid4().hex[:6]}",
        expiry_date=dt.date.today() + dt.timedelta(days=days),
        quantity=Decimal(qty),
        purchase_price=Decimal(price),
    )


async def _force_expiry_offset(db_session: AsyncSession, batch_id: uuid.UUID, days: int) -> None:
    """Directly set a batch's expiry_date to today+days (including negative,
    bypassing receive's future-date guard) — same technique test_batch_
    tracking_m4.py's _force_past_expiry uses."""
    await db_session.execute(
        text("UPDATE medication_batches SET expiry_date = :d WHERE id = :i").bindparams(
            d=dt.date.today() + dt.timedelta(days=days), i=batch_id
        )
    )
    await db_session.commit()


# ------------------------------ service ------------------------------


async def test_expiry_waste_buckets_match_expiry_alerts(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_id = await _make_med(db_session)
    await _receive(db_session, actor, branch, med_id, days=10, qty="40", price="5.00")

    today = dt.date.today()
    report = await inventory_service.expiry_waste_report(
        db_session, branch_id=branch.id, date_from=today, date_to=today
    )
    direct_alerts = await inventory_service.expiry_alerts(db_session, branch_id=branch.id)

    # Reused verbatim — the report's buckets must be byte-identical to a
    # direct expiry_alerts call, not a re-derived approximation.
    assert report["buckets"] == direct_alerts["buckets"]
    assert report["as_of"] == direct_alerts["as_of"]
    assert report["buckets"]["within_30"]["count"] == 1
    assert report["buckets"]["within_30"]["total_value"] == "200.00"


async def test_expiry_waste_value_from_swept_batches(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    """The load-bearing test — see module docstring."""
    med_id = await _make_med(db_session)
    batch = await _receive(db_session, actor, branch, med_id, days=5, qty="100", price="3.00")
    await _force_expiry_offset(db_session, batch.id, days=-1)
    swept = await inventory_service.expiry_sweep(db_session)
    assert swept["swept"] >= 1  # global sweep; other tests' data may coexist

    today = dt.date.today()
    report = await inventory_service.expiry_waste_report(
        db_session,
        branch_id=branch.id,
        date_from=today - dt.timedelta(days=1),
        date_to=today,
    )
    # 100 units x 3.00 — the batch's OWN quantity, not SUM(quantity_delta)
    # (which is exactly 0 for every expiry_writeoff row, by design).
    assert report["waste_swept"] == {"count": 1, "quantity": "100.000", "value": "300.00"}


async def test_expiry_waste_sums_across_multiple_swept_batches(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    """Every prior waste_swept assertion used exactly one swept batch — never
    proved the SUM/COUNT actually aggregate across more than one. Two
    DIFFERENT medications, two DIFFERENT batches, swept together in one
    expiry_sweep call: count must be 2, value must be the sum of both
    (60.00 + 20.00), not either one alone or a double-count of one."""
    med_a = await _make_med(db_session)
    med_b = await _make_med(db_session)
    batch_a = await _receive(db_session, actor, branch, med_a, days=5, qty="20", price="3.00")
    batch_b = await _receive(db_session, actor, branch, med_b, days=5, qty="5", price="4.00")
    await _force_expiry_offset(db_session, batch_a.id, days=-1)
    await _force_expiry_offset(db_session, batch_b.id, days=-1)
    swept = await inventory_service.expiry_sweep(db_session)
    assert swept["swept"] >= 2

    today = dt.date.today()
    report = await inventory_service.expiry_waste_report(
        db_session, branch_id=branch.id, date_from=today, date_to=today
    )
    # 20x3.00=60.00 + 5x4.00=20.00 = 80.00; quantity 20+5=25.
    assert report["waste_swept"] == {"count": 2, "quantity": "25.000", "value": "80.00"}


async def test_expiry_waste_swept_excluded_outside_date_range(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_id = await _make_med(db_session)
    batch = await _receive(db_session, actor, branch, med_id, days=5, qty="10", price="1.00")
    await _force_expiry_offset(db_session, batch.id, days=-1)
    await inventory_service.expiry_sweep(db_session)

    # A date range that does NOT include today (when the sweep happened).
    far_past_from = dt.date.today() - dt.timedelta(days=60)
    far_past_to = dt.date.today() - dt.timedelta(days=50)
    report = await inventory_service.expiry_waste_report(
        db_session, branch_id=branch.id, date_from=far_past_from, date_to=far_past_to
    )
    assert report["waste_swept"] == {"count": 0, "quantity": "0.000", "value": "0.00"}


async def test_expiry_waste_trend_weekly_buckets_and_horizon(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_id = await _make_med(db_session)
    # day 1 -> week 0 (floor(1/7)=0); day 10 -> week 1 (floor(10/7)=1); day 40 -> week 5.
    await _receive(db_session, actor, branch, med_id, days=1, qty="5", price="2.00")
    await _receive(db_session, actor, branch, med_id, days=10, qty="7", price="2.00")
    await _receive(db_session, actor, branch, med_id, days=40, qty="9", price="2.00")
    # Outside the 90-day horizon entirely -> must not appear in ANY bucket.
    far = await _receive(db_session, actor, branch, med_id, days=95, qty="3", price="2.00")

    today = dt.date.today()
    report = await inventory_service.expiry_waste_report(
        db_session, branch_id=branch.id, date_from=today, date_to=today
    )
    trend = report["trend"]
    assert len(trend) == 13  # ceil(90/7) weekly buckets, 0-indexed
    assert trend[0] == {"week": 0, "count": 1, "quantity": "5.000", "value": "10.00"}
    assert trend[1] == {"week": 1, "count": 1, "quantity": "7.000", "value": "14.00"}
    assert trend[5] == {"week": 5, "count": 1, "quantity": "9.000", "value": "18.00"}
    total_batches_in_trend = sum(int(w["count"]) for w in trend)
    assert total_batches_in_trend == 3  # the day=95 batch (far) is excluded
    assert str(far.id)  # sanity: the batch exists, it's just outside the horizon


async def test_expiry_waste_trend_exact_horizon_boundary(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    """day=90 is INSIDE the EXPIRY_WARNING_DAYS horizon (expiry_date <=
    CURRENT_DATE + 90) and must land in the last bucket, week 12
    (floor(90/7)=12, the LEAST(...,12) cap does nothing here — it's already
    exactly 12). day=91 is one day outside and must be excluded from the
    trend entirely, not clamped into week 12 too. Isolated in its own branch
    so it can't be confused with the coarser multi-week test above."""
    med_id = await _make_med(db_session)
    await _receive(db_session, actor, branch, med_id, days=90, qty="4", price="1.00")
    await _receive(db_session, actor, branch, med_id, days=91, qty="6", price="1.00")

    today = dt.date.today()
    report = await inventory_service.expiry_waste_report(
        db_session, branch_id=branch.id, date_from=today, date_to=today
    )
    trend = report["trend"]
    assert trend[12] == {"week": 12, "count": 1, "quantity": "4.000", "value": "4.00"}
    total_in_trend = sum(int(w["count"]) for w in trend)
    assert total_in_trend == 1  # the day=91 batch never appears anywhere


async def test_expiry_waste_expired_and_locked_value_match_batch_status_report(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_id = await _make_med(db_session)
    expired_batch = await _receive(
        db_session, actor, branch, med_id, days=5, qty="20", price="4.00"
    )
    await _force_expiry_offset(db_session, expired_batch.id, days=-1)
    await inventory_service.expiry_sweep(db_session)
    quarantined_batch = await _receive(
        db_session, actor, branch, med_id, days=20, qty="10", price="5.00"
    )
    await inventory_service.set_batch_status(
        db_session, actor=actor, batch=quarantined_batch, status="quarantined", reason="فحص"
    )

    today = dt.date.today()
    report = await inventory_service.expiry_waste_report(
        db_session, branch_id=branch.id, date_from=today, date_to=today
    )
    status_report = await inventory_service.batch_status_report(db_session, branch_id=branch.id)

    assert report["expired_value"] == status_report["by_status"]["expired"]["total_value"]
    assert report["expired_value"] == "80.00"  # 20 x 4.00
    assert report["locked_value"] == status_report["locked_value"]
    assert report["locked_value"] == "130.00"  # 80 expired + 50 quarantined (10 x 5.00)


async def test_expiry_waste_rejects_reversed_range(
    db_session: AsyncSession, branch: Branch
) -> None:
    today = dt.date.today()
    with pytest.raises(ApiError):
        await inventory_service.expiry_waste_report(
            db_session, branch_id=branch.id, date_from=today, date_to=today - dt.timedelta(days=1)
        )


# ------------------------------ HTTP layer ------------------------------


async def _seed_role_user(db_session: AsyncSession, role_code: str) -> str:
    from pharmaos_api.security.passwords import hash_password

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


async def test_expiry_waste_report_permission_matrix_and_shape(
    client: httpx.AsyncClient, db_session: AsyncSession, branch: Branch
) -> None:
    today = dt.date.today().isoformat()
    params = {"branch_id": str(branch.id), "date_from": today, "date_to": today}

    await _login(client, await _seed_role_user(db_session, "cashier"))
    denied = await client.get("/api/v1/reports/inventory/expiry-waste", params=params)
    assert denied.status_code == 403

    # pharmacist has reports.inventory, same tier as the other M2/M3 reports.
    await _login(client, await _seed_role_user(db_session, "pharmacist"))
    ok_ph = await client.get("/api/v1/reports/inventory/expiry-waste", params=params)
    assert ok_ph.status_code == 200, ok_ph.text

    await _login(client, await _seed_role_user(db_session, "branch_manager"))
    ok = await client.get("/api/v1/reports/inventory/expiry-waste", params=params)
    assert ok.status_code == 200, ok.text
    data = ok.json()["data"]
    assert set(data) >= {
        "date_from",
        "date_to",
        "as_of",
        "buckets",
        "expired_value",
        "locked_value",
        "waste_swept",
        "trend",
    }

    bad = await client.get(
        "/api/v1/reports/inventory/expiry-waste",
        params={**params, "date_to": "2020-01-01"},
    )
    assert bad.status_code == 422
