"""Returns / credit notes (P2-M7; stock disposition per plan D3 / review C6).

CLAUDE.md rule 14: a completed invoice is NEVER modified. A return is a separate
credit note that references the original invoice, lands the returned units in a
DISTINCT batch (quarantined by default for pharmacist review, or active if the
branch opts in via settings.returned_stock_to_active), credits the customer at
the ORIGINAL price and VAT rate, reverses the loyalty points earned on the sale
(clamped), and records a NEGATIVE payment (refund) in the money ledger.
Everything happens in ONE transaction.
"""

import datetime as dt
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.audit import AuditAction
from pharmaos_api.errors import ApiError, ErrorCode
from pharmaos_api.models import (
    Invoice,
    InvoiceItem,
    Medication,
    MedicationBatch,
    MedicationPackaging,
    Return,
    ReturnItem,
    Settings,
    StockMovement,
    User,
)
from pharmaos_api.services import (
    audit_service,
    cashier_service,
    customer_service,
    inventory_service,
    payment_service,
    tax_service,
)

_CENTS = Decimal("0.01")
_QTY = Decimal("0.001")
_REFUND_METHODS = {"cash", "card", "store_credit"}
MAX_PAGE_SIZE = 100


@dataclass(frozen=True)
class ReturnLine:
    invoice_item_id: uuid.UUID
    quantity: Decimal  # at the sold packaging level (<= sold - already returned)


@dataclass
class _Computed:
    """A validated return line with its credit amounts (pre-stock)."""

    item: InvoiceItem
    qty: Decimal
    qty_smallest: Decimal
    line_total: Decimal
    tax: Decimal
    tax_rate: Decimal


async def _next_return_number(session: AsyncSession, branch_id: uuid.UUID) -> str:
    today = dt.date.today().strftime("%Y%m%d")
    prefix = f"RET-{today}-"
    count = (
        await session.execute(
            select(func.count(Return.id)).where(
                Return.branch_id == branch_id, Return.return_number.like(prefix + "%")
            )
        )
    ).scalar_one()
    return f"{prefix}{count + 1:04d}"


async def _returned_stock_to_active(session: AsyncSession, branch_id: uuid.UUID) -> bool:
    """Branch policy (plan D3): returned stock is quarantined by default; a branch
    may opt in to sending it straight back to sellable. Missing settings row ⇒
    the safe default (quarantine)."""
    value = (
        await session.execute(
            select(Settings.returned_stock_to_active).where(
                Settings.branch_id == branch_id, Settings.is_deleted.is_(False)
            )
        )
    ).scalar_one_or_none()
    return bool(value)


async def _returned_qty(session: AsyncSession, invoice_item_id: uuid.UUID) -> Decimal:
    """Display-unit quantity already returned against an invoice line."""
    total = (
        await session.execute(
            select(func.coalesce(func.sum(ReturnItem.quantity), 0)).where(
                ReturnItem.invoice_item_id == invoice_item_id, ReturnItem.is_deleted.is_(False)
            )
        )
    ).scalar_one()
    return Decimal(total)


async def get_returnable_by_number(
    session: AsyncSession, *, branch_id: uuid.UUID, invoice_number: str
) -> dict[str, object]:
    """Resolve a human-facing invoice number (as printed on the receipt) within a
    branch, then return the same shape as get_returnable — the entry point for
    the returns UI, which never sees invoice UUIDs."""
    invoice = (
        await session.execute(
            select(Invoice).where(
                Invoice.branch_id == branch_id,
                Invoice.invoice_number == invoice_number.strip(),
                Invoice.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if invoice is None:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 404, message="Invoice not found.")
    return await get_returnable(session, invoice.id)


async def get_returnable(session: AsyncSession, invoice_id: uuid.UUID) -> dict[str, object]:
    """Per original line: sold / already-returned / still-returnable quantities.
    Drives the return UI. Lines are aggregated by invoice_item (a FEFO-sliced sale
    keeps one return line per batch, so stock returns to the exact batch)."""
    invoice = (
        await session.execute(
            select(Invoice).where(Invoice.id == invoice_id, Invoice.is_deleted.is_(False))
        )
    ).scalar_one_or_none()
    if invoice is None:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 404, message="Invoice not found.")

    rows = (
        await session.execute(
            select(InvoiceItem, Medication, MedicationPackaging)
            .join(Medication, Medication.id == InvoiceItem.medication_id)
            .join(MedicationPackaging, MedicationPackaging.id == InvoiceItem.packaging_id)
            .where(InvoiceItem.invoice_id == invoice_id, InvoiceItem.is_deleted.is_(False))
            .order_by(InvoiceItem.created_at)
        )
    ).all()

    lines: list[dict[str, object]] = []
    for item, medication, packaging in rows:
        returned = await _returned_qty(session, item.id)
        returnable = item.quantity - returned
        lines.append(
            {
                "invoice_item_id": str(item.id),
                "medication_id": str(item.medication_id),
                "trade_name": medication.trade_name,
                "trade_name_ar": medication.trade_name_ar,
                "packaging_name_ar": packaging.name_ar,
                "unit_price": str(item.unit_price),
                "tax_rate": str(item.tax_rate),
                "sold_qty": str(item.quantity),
                "returned_qty": str(returned),
                "returnable_qty": str(returnable if returnable > 0 else Decimal("0.000")),
            }
        )
    return {
        "invoice_id": str(invoice.id),
        "invoice_number": invoice.invoice_number,
        "invoice_type": invoice.invoice_type,
        "status": invoice.status,
        "currency_code": invoice.currency_code,
        "lines": lines,
    }


async def create_return(
    session: AsyncSession,
    *,
    actor: User,
    original_invoice_id: uuid.UUID,
    lines: list[ReturnLine],
    reason: str | None = None,
    refund_method: str = "cash",
) -> Return:
    """Create a credit note for part or all of a completed sale (one atomic tx)."""
    if not lines:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Empty return.")
    if refund_method not in _REFUND_METHODS:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Invalid refund method.")

    invoice = (
        await session.execute(
            select(Invoice).where(Invoice.id == original_invoice_id, Invoice.is_deleted.is_(False))
        )
    ).scalar_one_or_none()
    if invoice is None:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 404, message="Invoice not found.")
    if invoice.status != "completed":
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Invoice is not completed.")
    if invoice.invoice_type == "return":
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Cannot return a credit note.")

    # Pass 1: validate every line and compute credits BEFORE touching stock, so a
    # bad line rejects the whole return cleanly (no partial state).
    computed: list[_Computed] = []
    subtotal = Decimal("0.00")
    tax_total = Decimal("0.00")
    total = Decimal("0.00")
    for line in lines:
        if line.quantity <= 0:
            raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Quantity must be positive.")
        item = (
            await session.execute(
                select(InvoiceItem).where(
                    InvoiceItem.id == line.invoice_item_id,
                    InvoiceItem.invoice_id == original_invoice_id,
                    InvoiceItem.is_deleted.is_(False),
                )
            )
        ).scalar_one_or_none()
        if item is None:
            raise ApiError(ErrorCode.VALIDATION_FAILED, 404, message="Invoice line not found.")
        already = await _returned_qty(session, item.id)
        if line.quantity > item.quantity - already:
            raise ApiError(
                ErrorCode.VALIDATION_FAILED, 422, message="Return exceeds the sold quantity."
            )
        ratio = line.quantity / item.quantity
        qty_smallest = (item.qty_smallest * ratio).quantize(_QTY)
        line_total = (item.unit_price * line.quantity).quantize(_CENTS)
        _, tax = tax_service.split_inclusive(line_total, item.tax_rate)
        computed.append(
            _Computed(
                item=item,
                qty=line.quantity,
                qty_smallest=qty_smallest,
                line_total=line_total,
                tax=tax,
                tax_rate=item.tax_rate,
            )
        )
        total += line_total
        tax_total += tax
    subtotal = (total - tax_total).quantize(_CENTS)

    open_session = await cashier_service.get_open_session(
        session, branch_id=invoice.branch_id, cashier_id=actor.id
    )
    credit_note = Return(
        branch_id=invoice.branch_id,
        original_invoice_id=invoice.id,
        return_number=await _next_return_number(session, invoice.branch_id),
        reason=(reason.strip() or None) if reason else None,
        currency_code=invoice.currency_code,
        subtotal=subtotal,
        tax_amount=tax_total,
        total=total,
        refund_method=refund_method,
        customer_id=invoice.customer_id,
        cash_session_id=open_session.id if open_session is not None else None,
        created_by=actor.id,
    )
    session.add(credit_note)
    await session.flush()

    # Returned stock lands in a DISTINCT batch (plan D3): quarantined by default
    # (pharmacist review before resale), or active if the branch opts in. Using a
    # separate batch — rather than merging back into the original — lets returned
    # units carry their own status so only inspected stock becomes sellable.
    to_active = await _returned_stock_to_active(session, invoice.branch_id)
    return_status = "active" if to_active else "quarantined"

    # Pass 2: land returned stock in its return batch + write the credit lines.
    for c in computed:
        item = c.item
        origin = (
            await session.execute(
                select(MedicationBatch).where(MedicationBatch.id == item.batch_id)
            )
        ).scalar_one()
        return_batch = MedicationBatch(
            branch_id=invoice.branch_id,
            medication_id=item.medication_id,
            batch_number=origin.batch_number,
            expiry_date=origin.expiry_date,
            quantity=c.qty_smallest,
            purchase_price=origin.purchase_price,
            supplier_id=origin.supplier_id,
            status=return_status,
            created_by=actor.id,
        )
        session.add(return_batch)
        await session.flush()  # resolve return_batch.id for the movement/line FKs
        session.add(
            StockMovement(
                branch_id=invoice.branch_id,
                batch_id=return_batch.id,
                movement_type="return_in",
                quantity_delta=c.qty_smallest,
                reference_type="return",
                reference_id=credit_note.id,
                reason="customer_return",
                created_by=actor.id,
            )
        )
        # Only ACTIVE batches contribute to the derived cache (invariant holds);
        # quarantined returned stock is added to the cache on later release
        # (inventory_service.set_batch_status).
        if return_status == "active":
            await inventory_service.apply_cache_delta(
                session, invoice.branch_id, item.medication_id, c.qty_smallest
            )
        session.add(
            ReturnItem(
                branch_id=invoice.branch_id,
                return_id=credit_note.id,
                invoice_item_id=item.id,
                medication_id=item.medication_id,
                packaging_id=item.packaging_id,
                batch_id=return_batch.id,
                quantity=c.qty,
                qty_smallest=c.qty_smallest,
                unit_price=item.unit_price,
                line_total=c.line_total,
                tax_rate=c.tax_rate,
                tax_amount=c.tax,
                created_by=actor.id,
            )
        )

    # Reverse loyalty points earned on the original sale (clamped, never negative)
    # — atomic with the credit note. No-op for a walk-in sale (no customer).
    if invoice.customer_id is not None:
        await customer_service.reverse_for_return(
            session,
            actor=actor,
            customer_id=invoice.customer_id,
            refunded_total=total,
            return_id=credit_note.id,
        )

    # Refund in the money ledger (negative), atomic with the credit note.
    await payment_service.record(
        session,
        actor=actor,
        branch_id=invoice.branch_id,
        amount=-total,
        method=refund_method,
        return_id=credit_note.id,
        cash_session_id=credit_note.cash_session_id,
    )

    await audit_service.record(
        session,
        AuditAction.RETURN_CREATED,
        actor=actor,
        branch_id=invoice.branch_id,
        entity_type="return",
        entity_id=credit_note.id,
        metadata={
            "return_number": credit_note.return_number,
            "original_invoice": invoice.invoice_number,
            "total": str(total),
            "line_count": len(computed),
        },
    )
    await session.commit()
    await session.refresh(credit_note)
    return credit_note


# --------------------------- read models ---------------------------


async def get_return(session: AsyncSession, return_id: uuid.UUID) -> dict[str, object]:
    row = (
        await session.execute(
            select(Return, Invoice.invoice_number)
            .join(Invoice, Invoice.id == Return.original_invoice_id)
            .where(Return.id == return_id, Return.is_deleted.is_(False))
        )
    ).first()
    if row is None:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 404, message="Return not found.")
    credit_note, invoice_number = row
    rows = (
        await session.execute(
            select(ReturnItem, Medication, MedicationPackaging)
            .join(Medication, Medication.id == ReturnItem.medication_id)
            .join(MedicationPackaging, MedicationPackaging.id == ReturnItem.packaging_id)
            .where(ReturnItem.return_id == return_id, ReturnItem.is_deleted.is_(False))
            .order_by(ReturnItem.created_at)
        )
    ).all()
    return {
        **_summary(credit_note, invoice_number),
        "items": [_item(i, m, p) for i, m, p in rows],
    }


async def list_returns(
    session: AsyncSession, *, branch_id: uuid.UUID, skip: int = 0, limit: int = 50
) -> tuple[list[dict[str, object]], int]:
    capped = min(max(limit, 1), MAX_PAGE_SIZE)
    conditions = [Return.branch_id == branch_id, Return.is_deleted.is_(False)]
    total = (await session.execute(select(func.count(Return.id)).where(*conditions))).scalar_one()
    rows = (
        await session.execute(
            select(Return, Invoice.invoice_number)
            .join(Invoice, Invoice.id == Return.original_invoice_id)
            .where(*conditions)
            .order_by(Return.created_at.desc())
            .offset(max(skip, 0))
            .limit(capped)
        )
    ).all()
    return [_summary(r, invoice_number) for r, invoice_number in rows], int(total)


def _summary(r: Return, invoice_number: str) -> dict[str, object]:
    return {
        "id": str(r.id),
        "return_number": r.return_number,
        "original_invoice_id": str(r.original_invoice_id),
        "original_invoice_number": invoice_number,
        "currency_code": r.currency_code,
        "subtotal": str(r.subtotal),
        "tax_amount": str(r.tax_amount),
        "total": str(r.total),
        "refund_method": r.refund_method,
        "reason": r.reason,
        "customer_id": str(r.customer_id) if r.customer_id else None,
        "created_at": r.created_at.isoformat(),
    }


def _item(
    item: ReturnItem, medication: Medication, packaging: MedicationPackaging
) -> dict[str, object]:
    return {
        "id": str(item.id),
        "medication_id": str(item.medication_id),
        "trade_name": medication.trade_name,
        "trade_name_ar": medication.trade_name_ar,
        "packaging_name_ar": packaging.name_ar,
        "quantity": str(item.quantity),
        "unit_price": str(item.unit_price),
        "line_total": str(item.line_total),
        "tax_amount": str(item.tax_amount),
    }
