"""Purchase orders (P2-M2): request -> approve -> receive.

Lifecycle (status): draft -> pending_approval -> approved -> partially_received
-> received; cancel from draft/pending_approval/approved. Receiving a line
REUSES inventory_service.receive_batch (batch + purchase_in movement + derived
-cache delta) linked via reference_type='purchase_order' — goods-in flows
through the one inventory ledger, and a whole multi-line receipt is ONE atomic
transaction (receive_batch does not commit; this service commits once).

Quantities are in the smallest unit (matching medication_batches); packaging_id
is the ordered level (reference); unit_cost is per smallest unit. No dedicated
audit action exists for POs in CLAUDE.md's AUDITED_OPERATIONS — the
stock_movements ledger is the receipt trail. Permissions (router):
create/submit/cancel = purchases.create, approve = purchases.approve,
receive = purchases.receive.
"""

import datetime as dt
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.errors import ApiError, ErrorCode
from pharmaos_api.models import (
    Branch,
    MedicationPackaging,
    PurchaseItem,
    PurchaseOrder,
    Supplier,
    User,
)
from pharmaos_api.services import inventory_service

MAX_PAGE_SIZE = 100
_OPEN_FOR_RECEIVE = {"approved", "partially_received"}
# Review D4: a partially_received PO can be CANCELLED to close out the
# undelivered remainder — already-received stock stays booked (it is real
# inventory); cancelling just stops expecting the rest.
_CANCELLABLE = {"draft", "pending_approval", "approved", "partially_received"}


@dataclass(frozen=True)
class PurchaseLineIn:
    medication_id: uuid.UUID
    packaging_id: uuid.UUID
    qty_ordered: Decimal
    unit_cost: Decimal


@dataclass(frozen=True)
class ReceiptLineIn:
    purchase_item_id: uuid.UUID
    batch_number: str
    expiry_date: dt.date
    quantity: Decimal
    gtin: str | None = None
    serials: list[str] | None = None


def _po_dict(po: PurchaseOrder) -> dict[str, object]:
    return {
        "id": str(po.id),
        "branch_id": str(po.branch_id),
        "supplier_id": str(po.supplier_id),
        "po_number": po.po_number,
        "status": po.status,
        "order_date": po.order_date.isoformat(),
        "expected_date": po.expected_date.isoformat() if po.expected_date else None,
        "currency_code": po.currency_code,
        "subtotal": str(po.subtotal),
        "tax_amount": str(po.tax_amount),
        "total": str(po.total),
        "notes": po.notes,
        "approved_by": str(po.approved_by) if po.approved_by else None,
        "approved_at": po.approved_at.isoformat() if po.approved_at else None,
        "created_at": po.created_at.isoformat(),
    }


def _item_dict(item: PurchaseItem) -> dict[str, object]:
    return {
        "id": str(item.id),
        "medication_id": str(item.medication_id),
        "packaging_id": str(item.packaging_id),
        "qty_ordered": str(item.qty_ordered),
        "qty_received": str(item.qty_received),
        "unit_cost": str(item.unit_cost),
        "line_total": str(item.line_total),
    }


def to_dict(po: PurchaseOrder, items: list[PurchaseItem]) -> dict[str, object]:
    body = _po_dict(po)
    body["items"] = [_item_dict(i) for i in items]
    return body


async def get_items(session: AsyncSession, po_id: uuid.UUID) -> list[PurchaseItem]:
    return list(
        (
            await session.execute(
                select(PurchaseItem)
                .where(PurchaseItem.purchase_order_id == po_id, PurchaseItem.is_deleted.is_(False))
                .order_by(PurchaseItem.created_at)
            )
        )
        .scalars()
        .all()
    )


async def get_purchase_order(session: AsyncSession, po_id: uuid.UUID) -> PurchaseOrder:
    po = (
        await session.execute(
            select(PurchaseOrder).where(
                PurchaseOrder.id == po_id, PurchaseOrder.is_deleted.is_(False)
            )
        )
    ).scalar_one_or_none()
    if po is None:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 404, message="Purchase order not found.")
    return po


async def _next_po_number(session: AsyncSession, branch_id: uuid.UUID) -> str:
    """PO-YYYYMMDD-NNNN per branch per (local) day; UNIQUE(branch_id, po_number)
    is the correctness backstop (mirrors the invoice-numbering convention)."""
    today = dt.date.today().strftime("%Y%m%d")
    prefix = f"PO-{today}-"
    count = (
        await session.execute(
            select(func.count(PurchaseOrder.id)).where(
                PurchaseOrder.branch_id == branch_id, PurchaseOrder.po_number.like(prefix + "%")
            )
        )
    ).scalar_one()
    return f"{prefix}{count + 1:04d}"


async def create_purchase_order(
    session: AsyncSession,
    *,
    actor: User,
    branch_id: uuid.UUID,
    supplier_id: uuid.UUID,
    lines: list[PurchaseLineIn],
    expected_date: dt.date | None = None,
    notes: str | None = None,
) -> tuple[PurchaseOrder, list[PurchaseItem]]:
    """Create a DRAFT purchase order with its lines; amounts computed server-side."""
    if not lines:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="At least one line is required.")

    branch = await session.get(Branch, branch_id)
    if branch is None or branch.is_deleted or not branch.is_active:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Unknown branch.")
    supplier = (
        await session.execute(
            select(Supplier).where(Supplier.id == supplier_id, Supplier.is_deleted.is_(False))
        )
    ).scalar_one_or_none()
    if supplier is None:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Unknown supplier.")

    for line in lines:
        if line.qty_ordered <= 0 or line.unit_cost < 0:
            raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Invalid line qty/cost.")
        pkg = (
            await session.execute(
                select(MedicationPackaging).where(
                    MedicationPackaging.id == line.packaging_id,
                    MedicationPackaging.medication_id == line.medication_id,
                    MedicationPackaging.is_deleted.is_(False),
                )
            )
        ).scalar_one_or_none()
        if pkg is None:
            raise ApiError(
                ErrorCode.VALIDATION_FAILED, 422, message="Unknown medication/packaging."
            )

    subtotal = sum((line.qty_ordered * line.unit_cost for line in lines), Decimal("0"))
    po = PurchaseOrder(
        branch_id=branch_id,
        supplier_id=supplier_id,
        po_number=await _next_po_number(session, branch_id),
        status="draft",
        expected_date=expected_date,
        currency_code=branch.currency_code,
        subtotal=subtotal,
        tax_amount=Decimal("0"),
        total=subtotal,
        notes=notes,
        created_by=actor.id,
        updated_by=actor.id,
    )
    session.add(po)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ApiError(ErrorCode.SYNC_CONFLICT, 409, message="PO number collision.") from exc

    for line in lines:
        session.add(
            PurchaseItem(
                branch_id=branch_id,
                purchase_order_id=po.id,
                medication_id=line.medication_id,
                packaging_id=line.packaging_id,
                qty_ordered=line.qty_ordered,
                qty_received=Decimal("0"),
                unit_cost=line.unit_cost,
                line_total=line.qty_ordered * line.unit_cost,
                created_by=actor.id,
            )
        )
    await session.commit()
    await session.refresh(po)
    return po, await get_items(session, po.id)


async def list_purchase_orders(
    session: AsyncSession,
    *,
    branch_id: uuid.UUID | None = None,
    status: str | None = None,
    supplier_id: uuid.UUID | None = None,
    skip: int = 0,
    limit: int = 50,
) -> tuple[list[dict[str, object]], int]:
    capped = min(max(limit, 1), MAX_PAGE_SIZE)
    conditions: list[ColumnElement[bool]] = [PurchaseOrder.is_deleted.is_(False)]
    if branch_id is not None:
        conditions.append(PurchaseOrder.branch_id == branch_id)
    if status is not None:
        conditions.append(PurchaseOrder.status == status)
    if supplier_id is not None:
        conditions.append(PurchaseOrder.supplier_id == supplier_id)
    total = (
        await session.execute(select(func.count(PurchaseOrder.id)).where(*conditions))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(PurchaseOrder)
                .where(*conditions)
                .order_by(PurchaseOrder.created_at.desc())
                .offset(max(skip, 0))
                .limit(capped)
            )
        )
        .scalars()
        .all()
    )
    return [_po_dict(po) for po in rows], int(total)


def _require_status(po: PurchaseOrder, allowed: set[str]) -> None:
    if po.status not in allowed:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            422,
            message=f"Invalid state '{po.status}' for this action.",
        )


async def submit(session: AsyncSession, *, actor: User, po: PurchaseOrder) -> PurchaseOrder:
    _require_status(po, {"draft"})
    po.status = "pending_approval"
    po.updated_by = actor.id
    await session.commit()
    await session.refresh(po)
    return po


async def approve(session: AsyncSession, *, actor: User, po: PurchaseOrder) -> PurchaseOrder:
    _require_status(po, {"pending_approval"})
    po.status = "approved"
    po.approved_by = actor.id
    po.approved_at = dt.datetime.now(dt.UTC)
    po.updated_by = actor.id
    await session.commit()
    await session.refresh(po)
    return po


async def cancel(session: AsyncSession, *, actor: User, po: PurchaseOrder) -> PurchaseOrder:
    _require_status(po, _CANCELLABLE)
    po.status = "cancelled"
    po.updated_by = actor.id
    await session.commit()
    await session.refresh(po)
    return po


async def receive(
    session: AsyncSession, *, actor: User, po: PurchaseOrder, receipts: list[ReceiptLineIn]
) -> tuple[PurchaseOrder, list[PurchaseItem]]:
    """Receive one or more lines against an approved PO — ONE atomic transaction.

    Each line creates a batch through the inventory receiving primitive
    (reference_type='purchase_order'); the PO status is recomputed from the
    per-line received quantities (received when every line is fully received)."""
    _require_status(po, _OPEN_FOR_RECEIVE)
    if not receipts:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="No receipt lines.")

    items = {item.id: item for item in await get_items(session, po.id)}
    for line in receipts:
        item = items.get(line.purchase_item_id)
        if item is None:
            raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Line is not on this PO.")
        if line.quantity <= 0:
            raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Quantity must be positive.")
        await inventory_service.receive_batch(
            session,
            actor=actor,
            branch_id=po.branch_id,
            medication_id=item.medication_id,
            batch_number=line.batch_number,
            expiry_date=line.expiry_date,
            quantity=line.quantity,
            purchase_price=item.unit_cost,
            supplier_id=po.supplier_id,
            reference_type="purchase_order",
            reference_id=po.id,
            gtin=line.gtin,
            serials=line.serials,
        )
        item.qty_received = item.qty_received + line.quantity

    fully = all(item.qty_received >= item.qty_ordered for item in items.values())
    po.status = "received" if fully else "partially_received"
    po.updated_by = actor.id
    await session.commit()
    await session.refresh(po)
    return po, await get_items(session, po.id)
