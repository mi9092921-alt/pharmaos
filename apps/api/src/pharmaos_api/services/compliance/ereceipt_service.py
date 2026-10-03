"""ETA e-receipt outbox service (P2-M10).

enqueue_for_invoice runs INSIDE the sale transaction (no network, no commit) so
the sale never blocks. A worker (drain, triggered on demand / CLI / Celery)
builds the receipt JSON, signs + submits it through the ETA port/adapter (local
simulator by default), and records the returned UUID + QR. Submission is audited
(ereceipt.submitted on success; ereceipt.failed independently on failure).
"""

import datetime as dt
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.audit import AuditAction
from pharmaos_api.models import (
    EReceiptQueue,
    Invoice,
    InvoiceItem,
    Medication,
    Settings,
    User,
)
from pharmaos_api.services import audit_service
from pharmaos_api.services.compliance import eta_adapter

ETA_SYSTEM = "eta_ereceipt"
MAX_PAGE_SIZE = 100
_TERMINAL = ("accepted", "rejected")
_NEEDS_WORK = ("pending", "building", "signed", "submitting", "submitted", "failed")


async def enqueue_for_invoice(
    session: AsyncSession, *, invoice: Invoice, einvoice_system: str | None
) -> None:
    """Outbox-enqueue an e-receipt for a completed sale — NO commit (atomic with
    the sale). Only when the branch's tax profile uses the ETA e-receipt system;
    otherwise a no-op. The UNIQUE(invoice_id) guard makes a re-enqueue harmless."""
    if einvoice_system != ETA_SYSTEM:
        return
    session.add(
        EReceiptQueue(
            branch_id=invoice.branch_id,
            invoice_id=invoice.id,
            status="pending",
            created_by=invoice.created_by,
        )
    )


async def build_payload(session: AsyncSession, invoice: Invoice) -> dict[str, object]:
    """The ETA-style receipt document (JSON) built from the persisted invoice."""
    rows = (
        await session.execute(
            select(InvoiceItem, Medication)
            .join(Medication, Medication.id == InvoiceItem.medication_id)
            .where(InvoiceItem.invoice_id == invoice.id, InvoiceItem.is_deleted.is_(False))
            .order_by(InvoiceItem.created_at)
        )
    ).all()
    settings = (
        await session.execute(
            select(Settings).where(
                Settings.branch_id == invoice.branch_id, Settings.is_deleted.is_(False)
            )
        )
    ).scalar_one_or_none()
    return {
        "invoice_id": str(invoice.id),
        "invoice_number": invoice.invoice_number,
        "issued_at": invoice.created_at.isoformat(),
        "currency_code": invoice.currency_code,
        "seller": {
            "name": settings.pharmacy_name if settings else None,
            "tax_registration_no": settings.tax_registration_no if settings else None,
        },
        "totals": {
            "net": str(invoice.subtotal),
            "tax": str(invoice.tax_amount),
            "discount": str(invoice.discount_amount),
            "total": str(invoice.total),
        },
        "lines": [
            {
                "description": med.trade_name_ar or med.trade_name,
                "quantity": str(item.quantity),
                "unit_price": str(item.unit_price),
                "tax_rate": str(item.tax_rate),
                "tax_amount": str(item.tax_amount),
                "line_total": str(item.line_total),
            }
            for item, med in rows
        ],
    }


async def process_one(
    session: AsyncSession, *, row: EReceiptQueue, actor: User | None = None
) -> EReceiptQueue:
    """Build → sign → submit ONE queued e-receipt via the adapter; commits the
    row. Terminal rows are left untouched. Failure marks the row 'failed' with
    the error and audits independently (so the failure persists)."""
    if row.status in _TERMINAL:
        return row
    invoice = (
        await session.execute(select(Invoice).where(Invoice.id == row.invoice_id))
    ).scalar_one()
    adapter = eta_adapter.get_adapter()
    row.submission_attempts = row.submission_attempts + 1
    try:
        payload = await build_payload(session, invoice)
        signed = adapter.sign(payload)
        result = adapter.submit(signed_payload=signed, payload=payload)
        row.payload = payload
        row.signed_payload = signed
        row.eta_uuid = result.eta_uuid
        row.qr_data = result.qr_data
        row.submitted_at = dt.datetime.now(dt.UTC)
        row.status = "accepted" if result.accepted else "rejected"
        if result.accepted:
            row.accepted_at = dt.datetime.now(dt.UTC)
        row.last_error = None
        await audit_service.record(
            session,
            AuditAction.ERECEIPT_SUBMITTED,
            actor=actor,
            branch_id=row.branch_id,
            entity_type="ereceipt_queue",
            entity_id=row.id,
            metadata={
                "invoice_id": str(row.invoice_id),
                "eta_uuid": row.eta_uuid,
                "status": row.status,
                "simulated": adapter.is_simulator,
            },
        )
        await session.commit()
        await session.refresh(row)
        return row
    except eta_adapter.EtaAdapterError as exc:
        row.status = "failed"
        row.last_error = str(exc)
        await session.commit()
        await audit_service.record_independent(
            AuditAction.ERECEIPT_FAILED,
            actor=actor,
            branch_id=row.branch_id,
            entity_type="ereceipt_queue",
            entity_id=row.id,
            metadata={"invoice_id": str(row.invoice_id), "error": str(exc)},
        )
        await session.refresh(row)
        return row


async def drain(
    session: AsyncSession, *, branch_id: uuid.UUID, actor: User | None = None, limit: int = 50
) -> dict[str, int]:
    """Process pending/failed e-receipts for a branch (oldest first)."""
    rows = (
        (
            await session.execute(
                select(EReceiptQueue)
                .where(
                    EReceiptQueue.branch_id == branch_id,
                    EReceiptQueue.status.in_(_NEEDS_WORK),
                    EReceiptQueue.is_deleted.is_(False),
                )
                .order_by(EReceiptQueue.created_at)
                .limit(min(max(limit, 1), MAX_PAGE_SIZE))
            )
        )
        .scalars()
        .all()
    )
    accepted = failed = 0
    for row in rows:
        processed = await process_one(session, row=row, actor=actor)
        if processed.status == "accepted":
            accepted += 1
        elif processed.status in ("failed", "rejected"):
            failed += 1
    return {"processed": len(rows), "accepted": accepted, "failed": failed}


def _out(row: EReceiptQueue) -> dict[str, object]:
    return {
        "id": str(row.id),
        "invoice_id": str(row.invoice_id),
        "status": row.status,
        "eta_uuid": row.eta_uuid,
        "qr_data": row.qr_data,
        "submission_attempts": row.submission_attempts,
        "last_error": row.last_error,
        "submitted_at": row.submitted_at.isoformat() if row.submitted_at else None,
        "created_at": row.created_at.isoformat(),
    }


async def list_queue(
    session: AsyncSession,
    *,
    branch_id: uuid.UUID,
    status: str | None = None,
    skip: int = 0,
    limit: int = 50,
) -> tuple[list[dict[str, object]], int]:
    capped = min(max(limit, 1), MAX_PAGE_SIZE)
    conditions = [EReceiptQueue.branch_id == branch_id, EReceiptQueue.is_deleted.is_(False)]
    if status is not None:
        conditions.append(EReceiptQueue.status == status)
    total = (
        await session.execute(select(func.count(EReceiptQueue.id)).where(*conditions))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(EReceiptQueue)
                .where(*conditions)
                .order_by(EReceiptQueue.created_at.desc())
                .offset(max(skip, 0))
                .limit(capped)
            )
        )
        .scalars()
        .all()
    )
    return [_out(r) for r in rows], int(total)


async def get_for_invoice(session: AsyncSession, invoice_id: uuid.UUID) -> dict[str, object] | None:
    row = (
        await session.execute(
            select(EReceiptQueue).where(
                EReceiptQueue.invoice_id == invoice_id, EReceiptQueue.is_deleted.is_(False)
            )
        )
    ).scalar_one_or_none()
    return _out(row) if row is not None else None
