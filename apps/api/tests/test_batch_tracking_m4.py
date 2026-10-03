"""Batch tracking deepening (P2-M4): expiry alerts (30/60/90 windows), the
auto-quarantine policy (past-expiry sweep, on demand + at boot), batch reports
(status/value with the sellable vs. locked-up split), and — the safety gate —
the proof that quarantined or expired batches can NEVER be sold (E-STK-002).

Service-layer tests exercise the read-models and the sale block directly; the
API-layer tests exercise the endpoints with their permission tiers + CSRF.
"""

import datetime as dt
import uuid
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.errors import ApiError, ErrorCode
from pharmaos_api.models import (
    Branch,
    Medication,
    MedicationBarcode,
    MedicationPackaging,
    Role,
    User,
)
from pharmaos_api.security.passwords import hash_password
from pharmaos_api.services import inventory_service, sales_service
from pharmaos_api.services.sales_service import SaleLine


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


async def _make_med(db_session: AsyncSession) -> str:
    """A plain medication (no packaging) — enough for receiving + reporting."""
    med = Medication(trade_name=f"M4Med {uuid.uuid4().hex[:6]}", trade_name_ar="دواء الدفعات")
    db_session.add(med)
    await db_session.commit()
    return str(med.id)


async def _make_sellable_med(db_session: AsyncSession) -> tuple[str, str]:
    """A strip-barcode med (10 tablets/strip @ 30.00) for the sale-block tests.
    Returns (med_id, barcode)."""
    unit_id = (
        await db_session.execute(
            text(
                "INSERT INTO units (name_ar) VALUES ('شريط') "
                "ON CONFLICT (name_ar) DO UPDATE SET name_ar=EXCLUDED.name_ar RETURNING id"
            )
        )
    ).scalar_one()
    await db_session.commit()
    med = Medication(trade_name=f"M4Sell {uuid.uuid4().hex[:6]}", trade_name_ar="دواء للبيع")
    db_session.add(med)
    await db_session.flush()
    strip = MedicationPackaging(
        medication_id=med.id,
        level=2,
        unit_id=unit_id,
        name_ar="شريط",
        qty_in_parent=Decimal(10),
        selling_price=Decimal("30.00"),
        is_default_sale=True,
    )
    db_session.add(strip)
    await db_session.flush()
    barcode = f"624{uuid.uuid4().int % 10**10:010d}"
    db_session.add(MedicationBarcode(medication_id=med.id, packaging_id=strip.id, barcode=barcode))
    await db_session.commit()
    return str(med.id), barcode


async def _receive(
    db_session: AsyncSession,
    actor: User,
    branch: Branch,
    med_id: str,
    *,
    days: int,
    qty: str,
    price: str = "2.00",
) -> "inventory_service.MedicationBatch":
    return await inventory_service.receive_stock(
        db_session,
        actor=actor,
        branch_id=branch.id,
        medication_id=uuid.UUID(med_id),
        batch_number=f"B-{uuid.uuid4().hex[:6]}",
        expiry_date=dt.date.today() + dt.timedelta(days=days),
        quantity=Decimal(qty),
        purchase_price=Decimal(price),
    )


async def _force_past_expiry(
    db_session: AsyncSession, batch_id: uuid.UUID, days_ago: int = 1
) -> None:
    """Push a received batch past its expiry (bypassing receive's future-date guard)."""
    await db_session.execute(
        text("UPDATE medication_batches SET expiry_date = :d WHERE id = :i").bindparams(
            d=dt.date.today() - dt.timedelta(days=days_ago), i=batch_id
        )
    )
    await db_session.commit()


# ------------------------------ expiry alerts (30/60/90) ------------------------------


async def test_expiry_alerts_bucket_by_30_60_90(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_id = await _make_med(db_session)
    await _receive(db_session, actor, branch, med_id, days=15, qty="10", price="2.00")  # critical
    await _receive(db_session, actor, branch, med_id, days=45, qty="20", price="2.00")  # warning/60
    await _receive(db_session, actor, branch, med_id, days=75, qty="30", price="2.00")  # warning/90
    await _receive(db_session, actor, branch, med_id, days=200, qty="99", price="2.00")  # excluded

    report = await inventory_service.expiry_alerts(db_session, branch_id=branch.id)
    buckets = report["buckets"]

    assert buckets["within_30"]["count"] == 1
    assert buckets["within_30"]["severity"] == "critical"
    assert buckets["within_60"]["count"] == 1
    assert buckets["within_60"]["severity"] == "warning"
    assert buckets["within_90"]["count"] == 1
    assert buckets["within_90"]["severity"] == "warning"
    assert buckets["expired"]["count"] == 0
    # The 200-day batch is beyond the 90-day horizon → not counted anywhere.
    assert report["totals"]["count"] == 3
    # Value = qty x unit purchase price (10+20+30 units x 2.00).
    assert Decimal(report["totals"]["total_value"]) == Decimal("120.00")
    assert buckets["within_30"]["batches"][0]["days_left"] == 15


async def test_expiry_alerts_include_expired_and_exclude_nonactive(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_id = await _make_med(db_session)
    # A still-active batch pushed past expiry → surfaces in the danger bucket.
    doomed = await _receive(db_session, actor, branch, med_id, days=5, qty="7", price="2.00")
    await _force_past_expiry(db_session, doomed.id)
    # A near-expiry batch that has been quarantined → must NOT appear (not sellable).
    held = await _receive(db_session, actor, branch, med_id, days=10, qty="8", price="2.00")
    await inventory_service.set_batch_status(
        db_session, actor=actor, batch=held, status="quarantined", reason="اشتباه"
    )

    report = await inventory_service.expiry_alerts(db_session, branch_id=branch.id)
    buckets = report["buckets"]
    assert buckets["expired"]["count"] == 1
    assert buckets["expired"]["severity"] == "danger"
    assert buckets["expired"]["batches"][0]["days_left"] < 0
    # Only the expired (still-active) batch counts; the quarantined one is excluded.
    assert report["totals"]["count"] == 1


# ------------------------------ batch report (status / value) ------------------------------


async def test_batch_status_report_counts_and_locked_value(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_id = await _make_med(db_session)
    # 100 active @ 2.00 = 200 sellable; 50 quarantined @ 3.00 = 150 locked.
    await _receive(db_session, actor, branch, med_id, days=300, qty="100", price="2.00")
    held = await _receive(db_session, actor, branch, med_id, days=300, qty="50", price="3.00")
    await inventory_service.set_batch_status(
        db_session, actor=actor, batch=held, status="quarantined", reason="تلف"
    )
    # One expired @ 4.00 x 25 = 100 locked.
    expired = await _receive(db_session, actor, branch, med_id, days=5, qty="25", price="4.00")
    await _force_past_expiry(db_session, expired.id)
    await inventory_service.expiry_sweep(db_session)

    report = await inventory_service.batch_status_report(db_session, branch_id=branch.id)
    by_status = report["by_status"]
    assert by_status["active"]["count"] == 1
    assert by_status["quarantined"]["count"] == 1
    assert by_status["expired"]["count"] == 1
    assert Decimal(report["sellable_value"]) == Decimal("200.00")
    # Locked = quarantined (150) + expired (100) + recalled (0).
    assert Decimal(report["locked_value"]) == Decimal("250.00")
    assert by_status["quarantined"]["total_value"] == "150.00"
    assert by_status["expired"]["total_value"] == "100.00"
    assert report["totals"]["batch_count"] == 3


# --------------------- E-STK-002 sale block (the safety gate) ---------------------


async def test_sale_blocked_from_quarantined_batch(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_id, barcode = await _make_sellable_med(db_session)
    batch = await _receive(db_session, actor, branch, med_id, days=300, qty="100", price="2.00")
    await inventory_service.set_batch_status(
        db_session, actor=actor, batch=batch, status="quarantined", reason="سحب"
    )
    with pytest.raises(ApiError) as exc:
        await sales_service.create_sale(
            db_session,
            branch_id=branch.id,
            lines=[SaleLine(quantity=Decimal(1), barcode=barcode)],
            cashier=actor,
        )
    assert exc.value.code == ErrorCode.BATCH_EXPIRED  # E-STK-002
    assert exc.value.http_status == 409


async def test_sale_blocked_from_expired_batch_after_sweep(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_id, barcode = await _make_sellable_med(db_session)
    batch = await _receive(db_session, actor, branch, med_id, days=3, qty="100", price="2.00")
    await _force_past_expiry(db_session, batch.id)
    swept = await inventory_service.expiry_sweep(db_session)
    assert swept["swept"] >= 1
    await db_session.refresh(batch)
    assert batch.status == "expired"

    with pytest.raises(ApiError) as exc:
        await sales_service.create_sale(
            db_session,
            branch_id=branch.id,
            lines=[SaleLine(quantity=Decimal(1), barcode=barcode)],
            cashier=actor,
        )
    assert exc.value.code == ErrorCode.BATCH_EXPIRED  # E-STK-002
    assert exc.value.http_status == 409


# ------------------------------ API layer (endpoints + permissions) ------------------------------


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


async def test_expiry_alerts_and_batch_report_endpoints(
    client: httpx.AsyncClient, db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_id = await _make_med(db_session)
    await _receive(db_session, actor, branch, med_id, days=20, qty="10", price="5.00")

    # inventory.view is granted to every role — a cashier can read the reports.
    await _login(client, await _seed_user(db_session, "cashier"))

    alerts = await client.get(
        "/api/v1/inventory/expiry-alerts", params={"branch_id": str(branch.id)}
    )
    assert alerts.status_code == 200, alerts.text
    data = alerts.json()["data"]
    assert data["buckets"]["within_30"]["count"] == 1
    assert data["windows"] == {"critical_days": 30, "mid_days": 60, "warning_days": 90}

    rep = await client.get("/api/v1/inventory/batch-report", params={"branch_id": str(branch.id)})
    assert rep.status_code == 200, rep.text
    assert rep.json()["data"]["by_status"]["active"]["count"] == 1
    assert rep.json()["data"]["sellable_value"] == "50.00"


async def test_expiry_sweep_endpoint_permission_csrf_and_effect(
    client: httpx.AsyncClient, db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_id, barcode = await _make_sellable_med(db_session)
    batch = await _receive(db_session, actor, branch, med_id, days=2, qty="40", price="2.00")
    await _force_past_expiry(db_session, batch.id)

    # cashier holds inventory.view but NOT inventory.adjust → the guard fires.
    cashier_csrf = await _login(client, await _seed_user(db_session, "cashier"))
    forbidden = await client.post(
        "/api/v1/inventory/expiry-sweep", headers={"X-CSRF-Token": cashier_csrf}
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "E-AUTH-002"

    # branch_manager holds inventory.adjust; CSRF is still mandatory on the mutation.
    mgr_csrf = await _login(client, await _seed_user(db_session, "branch_manager"))
    no_csrf = await client.post("/api/v1/inventory/expiry-sweep")
    assert no_csrf.status_code == 403
    assert no_csrf.json()["error"]["code"] == "E-AUTH-004"

    ok = await client.post("/api/v1/inventory/expiry-sweep", headers={"X-CSRF-Token": mgr_csrf})
    assert ok.status_code == 200, ok.text
    assert ok.json()["data"]["swept"] >= 1
    await db_session.refresh(batch)
    assert batch.status == "expired"
