"""Inventory core (P1-M7): receiving, adjustments, quarantine, the derived
cache invariant (cached_quantity == SUM(active batches)), drift check/rebuild,
and cache maintenance inside the SALE transaction."""

import datetime as dt
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.errors import ApiError
from pharmaos_api.models import Branch, Medication, MedicationBarcode, MedicationPackaging, User
from pharmaos_api.services import inventory_service as inv
from pharmaos_api.services import sales_service
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


async def _med_with_barcode(db_session: AsyncSession) -> tuple[uuid.UUID, str]:
    unit_id = (
        await db_session.execute(
            text(
                "INSERT INTO units (name_ar) VALUES ('قرص') "
                "ON CONFLICT (name_ar) DO UPDATE SET name_ar=EXCLUDED.name_ar RETURNING id"
            )
        )
    ).scalar_one()
    await db_session.commit()  # release upsert lock (cross-session FK checks)
    med = Medication(trade_name=f"InvMed {uuid.uuid4().hex[:6]}")
    db_session.add(med)
    await db_session.flush()
    pkg = MedicationPackaging(
        medication_id=med.id,
        level=3,
        unit_id=unit_id,
        name_ar="قرص",
        qty_in_parent=Decimal(1),
        selling_price=Decimal("5.00"),
        is_default_sale=True,
    )
    db_session.add(pkg)
    await db_session.flush()
    barcode = f"620{uuid.uuid4().int % 10**10:010d}"
    db_session.add(MedicationBarcode(medication_id=med.id, packaging_id=pkg.id, barcode=barcode))
    await db_session.commit()
    return med.id, barcode


async def _cached(db_session: AsyncSession, branch_id: uuid.UUID, med_id: uuid.UUID) -> Decimal:
    val = (
        await db_session.execute(
            text(
                "SELECT cached_quantity FROM branch_inventory "
                "WHERE branch_id = :b AND medication_id = :m"
            ).bindparams(b=branch_id, m=med_id)
        )
    ).scalar_one_or_none()
    return Decimal(val) if val is not None else Decimal(0)


async def test_receive_updates_truth_ledger_and_cache(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_id, _ = await _med_with_barcode(db_session)
    batch = await inv.receive_stock(
        db_session,
        actor=actor,
        branch_id=branch.id,
        medication_id=med_id,
        batch_number="RCV-1",
        expiry_date=dt.date.today() + dt.timedelta(days=365),
        quantity=Decimal(100),
        purchase_price=Decimal("2.50"),
    )
    assert batch.quantity == Decimal(100)
    # ledger
    moves = (
        await db_session.execute(
            text(
                "SELECT movement_type, quantity_delta FROM stock_movements WHERE batch_id = :i"
            ).bindparams(i=batch.id)
        )
    ).all()
    assert [(m[0], Decimal(m[1])) for m in moves] == [("purchase_in", Decimal(100))]
    # derived cache maintained in the same tx
    assert await _cached(db_session, branch.id, med_id) == Decimal(100)
    # drift-free
    assert await inv.drift_check(db_session, branch.id) == []


async def test_receive_rejects_expired(db_session: AsyncSession, actor: User, branch: Branch):
    med_id, _ = await _med_with_barcode(db_session)
    with pytest.raises(ApiError) as exc:
        await inv.receive_stock(
            db_session,
            actor=actor,
            branch_id=branch.id,
            medication_id=med_id,
            batch_number="EXP-1",
            expiry_date=dt.date.today() - dt.timedelta(days=1),
            quantity=Decimal(10),
            purchase_price=Decimal("1.00"),
        )
    assert exc.value.code == "E-STK-002"
    await db_session.rollback()


async def test_adjustment_requires_reason_and_audits(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    bid = branch.id  # plain values survive rollback-expiry of ORM objects
    med_id, _ = await _med_with_barcode(db_session)
    batch = await inv.receive_stock(
        db_session,
        actor=actor,
        branch_id=bid,
        medication_id=med_id,
        batch_number="ADJ-1",
        expiry_date=dt.date.today() + dt.timedelta(days=200),
        quantity=Decimal(50),
        purchase_price=Decimal("1.00"),
    )
    batch_id = batch.id  # capture before rollback (rollback expires ORM objects)
    with pytest.raises(ApiError):
        await inv.adjust_batch(
            db_session, actor=actor, batch=batch, quantity_delta=Decimal(-5), reason="  "
        )
    await db_session.rollback()
    await db_session.refresh(actor)  # rollback expired it too

    batch = await inv.get_batch(db_session, batch_id)
    batch = await inv.adjust_batch(
        db_session, actor=actor, batch=batch, quantity_delta=Decimal(-5), reason="تالف"
    )
    assert batch.quantity == Decimal(45)
    assert await _cached(db_session, bid, med_id) == Decimal(45)
    audited = (
        await db_session.execute(
            text(
                "SELECT COUNT(*) FROM audit_logs WHERE action='stock.adjusted' AND entity_id=:i"
            ).bindparams(i=batch.id)
        )
    ).scalar_one()
    assert audited == 1
    # over-draw rejected (quantity CHECK >= 0 backed by service check)
    with pytest.raises(ApiError) as exc:
        await inv.adjust_batch(
            db_session, actor=actor, batch=batch, quantity_delta=Decimal(-999), reason="خطأ"
        )
    assert exc.value.code == "E-STK-001"
    await db_session.rollback()


async def test_quarantine_removes_from_cache_and_audits(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_id, _ = await _med_with_barcode(db_session)
    batch = await inv.receive_stock(
        db_session,
        actor=actor,
        branch_id=branch.id,
        medication_id=med_id,
        batch_number="Q-1",
        expiry_date=dt.date.today() + dt.timedelta(days=100),
        quantity=Decimal(30),
        purchase_price=Decimal("1.00"),
    )
    batch = await inv.set_batch_status(
        db_session, actor=actor, batch=batch, status="quarantined", reason="اشتباه تلف"
    )
    assert batch.status == "quarantined"
    assert await _cached(db_session, branch.id, med_id) == Decimal(0)  # not sellable stock
    assert await inv.drift_check(db_session, branch.id) == []
    audited = (
        await db_session.execute(
            text(
                "SELECT COUNT(*) FROM audit_logs WHERE action='batch.quarantined' AND entity_id=:i"
            ).bindparams(i=batch.id)
        )
    ).scalar_one()
    assert audited == 1
    # release back to active -> cache restored
    batch = await inv.set_batch_status(
        db_session, actor=actor, batch=batch, status="active", reason="سليم"
    )
    assert await _cached(db_session, branch.id, med_id) == Decimal(30)


async def test_sale_maintains_cache_and_drift_rebuild(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_id, barcode = await _med_with_barcode(db_session)
    await inv.receive_stock(
        db_session,
        actor=actor,
        branch_id=branch.id,
        medication_id=med_id,
        batch_number="S-1",
        expiry_date=dt.date.today() + dt.timedelta(days=300),
        quantity=Decimal(40),
        purchase_price=Decimal("1.00"),
    )
    await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(barcode=barcode, quantity=Decimal(12))],
        cashier=actor,
    )
    assert await _cached(db_session, branch.id, med_id) == Decimal(28)
    assert await inv.drift_check(db_session, branch.id) == []

    # sabotage the cache -> drift detected -> rebuild restores the invariant
    await db_session.execute(
        text(
            "UPDATE branch_inventory SET cached_quantity = 999 "
            "WHERE branch_id=:b AND medication_id=:m"
        ).bindparams(b=branch.id, m=med_id)
    )
    await db_session.commit()
    drift = await inv.drift_check(db_session, branch.id)
    assert len(drift) == 1 and drift[0]["truth"] == "28.000"
    await inv.rebuild_cache(db_session, branch.id)
    assert await _cached(db_session, branch.id, med_id) == Decimal(28)
    assert await inv.drift_check(db_session, branch.id) == []


async def test_adjust_on_non_active_batch_keeps_cache_consistent(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    """A quarantined batch is NOT in the derived cache; adjusting it must move the
    batch truth (a stock_movement) WITHOUT touching the cache, or the invariant
    drifts. Regression for adjust_batch applying a cache delta unconditionally."""
    bid = branch.id
    med_id, _ = await _med_with_barcode(db_session)
    batch = await inv.receive_stock(
        db_session,
        actor=actor,
        branch_id=bid,
        medication_id=med_id,
        batch_number="QADJ-1",
        expiry_date=dt.date.today() + dt.timedelta(days=200),
        quantity=Decimal(50),
        purchase_price=Decimal("1.00"),
    )
    batch_id = batch.id
    batch = await inv.set_batch_status(
        db_session, actor=actor, batch=batch, status="quarantined", reason="hold"
    )
    assert await _cached(db_session, bid, med_id) == Decimal(0)  # not sellable → not cached

    # Adjust the QUARANTINED batch: physical truth changes, cache must stay 0.
    batch = await inv.adjust_batch(
        db_session, actor=actor, batch=batch, quantity_delta=Decimal(-10), reason="تلف"
    )
    assert batch.quantity == Decimal(40)
    assert await _cached(db_session, bid, med_id) == Decimal(0)
    assert await inv.drift_check(db_session, bid) == []

    # Releasing back to active brings the ADJUSTED physical quantity into the cache.
    batch = await inv.get_batch(db_session, batch_id)
    batch = await inv.set_batch_status(
        db_session, actor=actor, batch=batch, status="active", reason="ok"
    )
    assert await _cached(db_session, bid, med_id) == Decimal(40)
    assert await inv.drift_check(db_session, bid) == []
