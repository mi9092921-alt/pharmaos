"""The controlled-substance dispensing register (P2-M8).

Written automatically by sales_service — one row per FEFO batch slice of a
controlled_substance=true medication, regardless of prescription linkage. The
table itself is append-only at the DB level (migration 1800's trigger); there
is deliberately no update/delete function here.
"""

import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.audit import AuditAction
from pharmaos_api.models import (
    ControlledSubstanceLog,
    Invoice,
    Medication,
    MedicationBatch,
    User,
)
from pharmaos_api.services import audit_service

MAX_PAGE_SIZE = 100


async def record_dispense(
    session: AsyncSession,
    *,
    actor: User,
    branch_id: uuid.UUID,
    medication_id: uuid.UUID,
    batch_id: uuid.UUID,
    invoice_id: uuid.UUID,
    invoice_item_id: uuid.UUID,
    quantity_dispensed: Decimal,
    prescription_id: uuid.UUID | None = None,
) -> ControlledSubstanceLog:
    """Append one register row + audit controlled_substance.dispensed — NO
    commit (runs inside the sale's transaction, atomic with the sale)."""
    log = ControlledSubstanceLog(
        branch_id=branch_id,
        medication_id=medication_id,
        batch_id=batch_id,
        invoice_id=invoice_id,
        invoice_item_id=invoice_item_id,
        prescription_id=prescription_id,
        quantity_dispensed=quantity_dispensed,
        dispensed_by=actor.id,
    )
    session.add(log)
    await audit_service.record(
        session,
        AuditAction.CONTROLLED_SUBSTANCE_DISPENSED,
        actor=actor,
        branch_id=branch_id,
        entity_type="controlled_substance_log",
        # log.id is unresolved pre-flush; invoice_item_id in metadata already
        # identifies the exact event.
        entity_id=None,
        metadata={
            "medication_id": str(medication_id),
            "batch_id": str(batch_id),
            "invoice_id": str(invoice_id),
            "invoice_item_id": str(invoice_item_id),
            "quantity_dispensed": str(quantity_dispensed),
            "prescription_id": str(prescription_id) if prescription_id else None,
        },
    )
    return log


async def list_log(
    session: AsyncSession,
    *,
    branch_id: uuid.UUID,
    medication_id: uuid.UUID | None = None,
    skip: int = 0,
    limit: int = 50,
) -> tuple[list[dict[str, object]], int]:
    """Read model for the register view (controlled_substances.view). Joins in
    the human-facing references (batch number, invoice number, dispensing
    user's name) — a compliance register showing raw UUIDs would be useless to
    the pharmacist reviewing it. Every joined FK is NOT NULL and never
    hard-deleted, so a plain INNER JOIN is safe."""
    capped = min(max(limit, 1), MAX_PAGE_SIZE)
    conditions = [ControlledSubstanceLog.branch_id == branch_id]
    if medication_id is not None:
        conditions.append(ControlledSubstanceLog.medication_id == medication_id)
    total = (
        await session.execute(select(func.count(ControlledSubstanceLog.id)).where(*conditions))
    ).scalar_one()
    rows = (
        await session.execute(
            select(ControlledSubstanceLog, Medication, User, Invoice, MedicationBatch)
            .join(Medication, Medication.id == ControlledSubstanceLog.medication_id)
            .join(User, User.id == ControlledSubstanceLog.dispensed_by)
            .join(Invoice, Invoice.id == ControlledSubstanceLog.invoice_id)
            .join(MedicationBatch, MedicationBatch.id == ControlledSubstanceLog.batch_id)
            .where(*conditions)
            .order_by(ControlledSubstanceLog.created_at.desc())
            .offset(max(skip, 0))
            .limit(capped)
        )
    ).all()
    return [
        {
            "id": str(log.id),
            "medication_id": str(log.medication_id),
            "trade_name": medication.trade_name,
            "trade_name_ar": medication.trade_name_ar,
            "batch_id": str(log.batch_id),
            "batch_number": batch.batch_number,
            "invoice_id": str(log.invoice_id),
            "invoice_number": invoice.invoice_number,
            "prescription_id": str(log.prescription_id) if log.prescription_id else None,
            "quantity_dispensed": str(log.quantity_dispensed),
            "dispensed_by": str(log.dispensed_by),
            "dispensed_by_name": user.full_name,
            "created_at": log.created_at.isoformat(),
        }
        for log, medication, user, invoice, batch in rows
    ], int(total)
