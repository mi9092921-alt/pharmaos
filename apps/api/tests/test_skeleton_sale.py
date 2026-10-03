"""M12 walking-skeleton verification: scan -> sale (FEFO, atomic) -> receipt bytes.

Covers the CLAUDE.md inventory rules: batches as the only truth, movements for
every change, smallest-unit storage, FEFO order, blocked-batch protection, and
crash atomicity (nothing persists on failure)."""

import datetime as dt
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.errors import ApiError
from pharmaos_api.models import (
    Branch,
    Invoice,
    InvoiceItem,
    Medication,
    MedicationBarcode,
    MedicationBatch,
    MedicationPackaging,
    StockMovement,
    User,
)
from pharmaos_api.services import sales_service
from pharmaos_api.services.sales_service import SaleLine


@pytest.fixture
async def cashier(db_session: AsyncSession, seeded_user: dict) -> User:
    return (
        await db_session.execute(select(User).where(User.username == seeded_user["username"]))
    ).scalar_one()


@pytest.fixture
async def branch(db_session: AsyncSession) -> Branch:
    branch = Branch(
        name=f"فرع الاختبار {uuid.uuid4().hex[:6]}", country_code="EG", currency_code="EGP"
    )
    db_session.add(branch)
    await db_session.commit()
    return branch


async def _make_medication(
    db_session: AsyncSession, branch: Branch, *, batches: list[tuple[str, int, int]]
) -> tuple[str, str]:
    """Create a med with box/strip/tablet levels + a STRIP-level barcode + batches.

    batches: list of (batch_suffix, tablets_quantity, expiry_days_from_now).
    Batch numbers are namespaced per test run ("{uid}-{suffix}") because the
    shared test DB accumulates rows across tests.
    Returns (barcode, uid) — query batches as f"{uid}-{suffix}".
    """
    uid = uuid.uuid4().hex[:8]
    unit_ids = {}
    for name_ar in ("علبة", "شريط", "قرص"):
        unit_ids[name_ar] = (
            await db_session.execute(
                text(
                    "INSERT INTO units (name_ar) VALUES (:n) "
                    "ON CONFLICT (name_ar) DO UPDATE SET name_ar=EXCLUDED.name_ar RETURNING id"
                ).bindparams(n=name_ar)
            )
        ).scalar_one()

    med = Medication(trade_name=f"TestMed {uuid.uuid4().hex[:6]}", trade_name_ar="دواء اختبار")
    db_session.add(med)
    await db_session.flush()

    strip = MedicationPackaging(
        medication_id=med.id,
        level=2,
        unit_id=unit_ids["شريط"],
        name_ar="شريط",
        qty_in_parent=Decimal(3),
        selling_price=Decimal("30.00"),
        is_default_sale=True,
    )
    db_session.add_all(
        [
            MedicationPackaging(
                medication_id=med.id,
                level=1,
                unit_id=unit_ids["علبة"],
                name_ar="علبة",
                qty_in_parent=None,
                selling_price=Decimal("90.00"),
            ),
            strip,
            MedicationPackaging(
                medication_id=med.id,
                level=3,
                unit_id=unit_ids["قرص"],
                name_ar="قرص",
                qty_in_parent=Decimal(10),
                selling_price=Decimal("3.50"),
            ),
        ]
    )
    await db_session.flush()

    barcode = f"622{uuid.uuid4().int % 10**10:010d}"
    db_session.add(
        MedicationBarcode(
            medication_id=med.id, packaging_id=strip.id, barcode=barcode, is_primary=True
        )
    )
    for batch_suffix, tablets, expiry_days in batches:
        db_session.add(
            MedicationBatch(
                branch_id=branch.id,
                medication_id=med.id,
                batch_number=f"{uid}-{batch_suffix}",
                expiry_date=dt.date.today() + dt.timedelta(days=expiry_days),
                quantity=Decimal(tablets),
                purchase_price=Decimal("2.00"),
            )
        )
    await db_session.commit()
    return barcode, uid


async def test_scan_returns_default_level_fast(db_session: AsyncSession, branch: Branch) -> None:
    barcode, uid = await _make_medication(db_session, branch, batches=[("B1", 100, 300)])
    result = await sales_service.resolve_barcode(db_session, barcode)
    assert result.packaging_name_ar == "شريط" and result.level == 2
    assert result.selling_price == Decimal("30.00")
    # The < 50ms scan target is guaranteed structurally by the exact-match
    # barcode index (test_query_plans.py asserts the plan) and verified on the
    # device (docs/pilot-checklist.md) — not via a wall-clock assert on
    # shared/variable CI, which produces false failures.


async def test_sale_deducts_fefo_and_writes_ledger(
    db_session: AsyncSession, branch: Branch, cashier: User
) -> None:
    # Nearest expiry (B-near, 25 tablets) must be consumed before B-far.
    barcode, uid = await _make_medication(
        db_session, branch, batches=[("B-far", 100, 365), ("B-near", 25, 30)]
    )
    # 2 strips = 20 tablets -> all from B-near.
    invoice = await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(barcode=barcode, quantity=Decimal(2))],
        cashier=cashier,
    )
    assert invoice.total == Decimal("60.00") and invoice.currency_code == "EGP"
    assert invoice.invoice_number.startswith("INV-")

    near = (
        await db_session.execute(
            select(MedicationBatch).where(MedicationBatch.batch_number == f"{uid}-B-near")
        )
    ).scalar_one()
    far = (
        await db_session.execute(
            select(MedicationBatch).where(MedicationBatch.batch_number == f"{uid}-B-far")
        )
    ).scalar_one()
    assert near.quantity == Decimal(5) and far.quantity == Decimal(100)  # FEFO ✓

    # Ledger: one sale_out movement of -20 referencing the invoice.
    movements = (
        (
            await db_session.execute(
                select(StockMovement).where(StockMovement.reference_id == invoice.id)
            )
        )
        .scalars()
        .all()
    )
    assert [m.movement_type for m in movements] == ["sale_out"]
    assert movements[0].quantity_delta == Decimal(-20)


async def test_sale_splits_across_batches_fefo(
    db_session: AsyncSession, branch: Branch, cashier: User
) -> None:
    barcode, uid = await _make_medication(
        db_session, branch, batches=[("B-near", 15, 30), ("B-far", 100, 365)]
    )
    # 3 strips = 30 tablets -> 15 from B-near + 15 from B-far (two item slices).
    invoice = await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(barcode=barcode, quantity=Decimal(3))],
        cashier=cashier,
    )
    items = (
        (await db_session.execute(select(InvoiceItem).where(InvoiceItem.invoice_id == invoice.id)))
        .scalars()
        .all()
    )
    assert len(items) == 2
    assert sorted(i.qty_smallest for i in items) == [Decimal(15), Decimal(15)]
    assert sum(i.line_total for i in items) == invoice.subtotal == Decimal("90.00")

    near = (
        await db_session.execute(
            select(MedicationBatch).where(MedicationBatch.batch_number == f"{uid}-B-near")
        )
    ).scalar_one()
    assert near.quantity == 0 and near.status == "depleted"


async def test_insufficient_stock_persists_nothing(
    db_session: AsyncSession, branch: Branch, cashier: User
) -> None:
    barcode, uid = await _make_medication(db_session, branch, batches=[("B1", 10, 300)])
    invoices_before = (await db_session.execute(select(func.count(Invoice.id)))).scalar_one()
    movements_before = (await db_session.execute(select(func.count(StockMovement.id)))).scalar_one()

    with pytest.raises(ApiError) as exc:
        # 2 strips = 20 tablets > 10 available
        await sales_service.create_sale(
            db_session,
            branch_id=branch.id,
            lines=[SaleLine(barcode=barcode, quantity=Decimal(2))],
            cashier=cashier,
        )
    assert exc.value.code == "E-STK-001"
    await db_session.rollback()

    # ATOMICITY: no invoice, no movement, and the batch is untouched.
    assert (
        await db_session.execute(select(func.count(Invoice.id)))
    ).scalar_one() == invoices_before
    assert (
        await db_session.execute(select(func.count(StockMovement.id)))
    ).scalar_one() == movements_before
    batch = (
        await db_session.execute(
            select(MedicationBatch).where(MedicationBatch.batch_number == f"{uid}-B1")
        )
    ).scalar_one()
    assert batch.quantity == Decimal(10)


async def test_blocked_batches_never_sell(
    db_session: AsyncSession, branch: Branch, cashier: User
) -> None:
    # Only stock: one QUARANTINED batch and one EXPIRED batch -> E-STK-002.
    barcode, uid = await _make_medication(
        db_session, branch, batches=[("B-quar", 100, 300), ("B-expired", 100, -1)]
    )
    quar = (
        await db_session.execute(
            select(MedicationBatch).where(MedicationBatch.batch_number == f"{uid}-B-quar")
        )
    ).scalar_one()
    quar.status = "quarantined"
    await db_session.commit()

    with pytest.raises(ApiError) as exc:
        await sales_service.create_sale(
            db_session,
            branch_id=branch.id,
            lines=[SaleLine(barcode=barcode, quantity=Decimal(1))],
            cashier=cashier,
        )
    assert exc.value.code == "E-STK-002"  # rule 18: never sell from a non-active batch
    await db_session.rollback()


async def test_invoice_numbers_sequential_per_day(
    db_session: AsyncSession, branch: Branch, cashier: User
) -> None:
    barcode, uid = await _make_medication(db_session, branch, batches=[("B1", 300, 300)])
    first = await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(barcode=barcode, quantity=Decimal(1))],
        cashier=cashier,
    )
    second = await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(barcode=barcode, quantity=Decimal(1))],
        cashier=cashier,
    )
    n1 = int(first.invoice_number.rsplit("-", 1)[1])
    n2 = int(second.invoice_number.rsplit("-", 1)[1])
    assert n2 == n1 + 1


def test_escpos_receipt_bytes() -> None:
    from pharmaos_api.printing.escpos import (
        CUT,
        DRAWER_PULSE,
        INIT,
        ReceiptData,
        ReceiptLine,
        build_receipt,
    )

    payload = build_receipt(
        ReceiptData(
            pharmacy_name="PharmaOS",
            branch_name="الفرع الرئيسي",
            invoice_number="INV-20260711-0001",
            created_at_display="2026-07-11 02:00",
            lines=[
                ReceiptLine(
                    name="بنادول ٥٠٠",
                    quantity=Decimal(2),
                    unit_name="شريط",
                    line_total=Decimal("60.00"),
                )
            ],
            subtotal=Decimal("60.00"),
            discount=Decimal("0.00"),
            total=Decimal("60.00"),
            currency_symbol="ج.م",
            thank_you_message="شكراً لزيارتكم",
        )
    )
    assert payload.startswith(INIT)
    assert CUT in payload
    assert payload.endswith(DRAWER_PULSE)  # ESC p — the drawer opens with the receipt
    assert b"INV-20260711-0001" in payload
    assert "بنادول ٥٠٠".encode() in payload  # UTF-8 Arabic content present
    assert payload.index(CUT) < payload.index(DRAWER_PULSE)
