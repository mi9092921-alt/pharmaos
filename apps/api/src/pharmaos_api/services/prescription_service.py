"""Prescriptions (P2-M8) — service-layer boundary for encrypted prescription
notes (CLAUDE.md: "prescriptions.notes" is sensitive medical data) and the
prescribed/dispensed quantity ledger that sales_service consults and updates.

Items are immutable once created (a sale may already reference an item's id);
only the prescription's header fields and manual status overrides (e.g.
cancelling) are editable. status is otherwise recomputed automatically from
the items whenever a sale dispenses against one (see recompute_status).
"""

import datetime as dt
import logging
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.errors import ApiError, ErrorCode
from pharmaos_api.models import (
    Medication,
    MedicationPackaging,
    Prescription,
    PrescriptionItem,
    User,
)
from pharmaos_api.security.crypto import DecryptionError, decrypt_field, encrypt_field

logger = logging.getLogger(__name__)

MAX_PAGE_SIZE = 100
NOTES_CONTEXT = "prescriptions.notes"
_STATUSES = {"pending", "partially_fulfilled", "fulfilled", "expired", "cancelled"}
_OPEN_STATUSES = ("pending", "partially_fulfilled")


def _encrypt_notes(notes: str | None) -> bytes | None:
    clean = notes.strip() if notes else ""
    return encrypt_field(clean, context=NOTES_CONTEXT) if clean else None


def _decrypt_notes(payload: bytes | None) -> str | None:
    if payload is None:
        return None
    try:
        return decrypt_field(bytes(payload), context=NOTES_CONTEXT)
    except DecryptionError:
        logger.warning("prescription notes decrypt failed")
        return None


def _item_out(
    item: PrescriptionItem, medication: Medication, packaging: MedicationPackaging
) -> dict[str, object]:
    return {
        "id": str(item.id),
        "medication_id": str(item.medication_id),
        "trade_name": medication.trade_name,
        "trade_name_ar": medication.trade_name_ar,
        "packaging_id": str(item.packaging_id),
        "packaging_name_ar": packaging.name_ar,
        "prescribed_qty": str(item.prescribed_qty),
        "prescribed_qty_smallest": str(item.prescribed_qty_smallest),
        "dispensed_qty_smallest": str(item.dispensed_qty_smallest),
        "remaining_qty_smallest": str(item.prescribed_qty_smallest - item.dispensed_qty_smallest),
    }


async def _serialize(session: AsyncSession, p: Prescription) -> dict[str, object]:
    rows = (
        await session.execute(
            select(PrescriptionItem, Medication, MedicationPackaging)
            .join(Medication, Medication.id == PrescriptionItem.medication_id)
            .join(MedicationPackaging, MedicationPackaging.id == PrescriptionItem.packaging_id)
            .where(PrescriptionItem.prescription_id == p.id, PrescriptionItem.is_deleted.is_(False))
            .order_by(PrescriptionItem.created_at)
        )
    ).all()
    return {
        "id": str(p.id),
        "branch_id": str(p.branch_id),
        "customer_id": str(p.customer_id) if p.customer_id else None,
        "doctor_name": p.doctor_name,
        "doctor_license_no": p.doctor_license_no,
        "prescription_date": p.prescription_date.isoformat(),
        "notes": _decrypt_notes(p.notes_encrypted),
        "status": p.status,
        "created_at": p.created_at.isoformat(),
        "items": [_item_out(i, m, pk) for i, m, pk in rows],
    }


async def get_prescription(session: AsyncSession, prescription_id: uuid.UUID) -> Prescription:
    p = (
        await session.execute(
            select(Prescription).where(
                Prescription.id == prescription_id, Prescription.is_deleted.is_(False)
            )
        )
    ).scalar_one_or_none()
    if p is None:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 404, message="Prescription not found.")
    return p


async def get_prescription_out(
    session: AsyncSession, prescription_id: uuid.UUID
) -> dict[str, object]:
    return await _serialize(session, await get_prescription(session, prescription_id))


class NewPrescriptionItem:
    """Plain input shape for create_prescription (avoids importing the router's
    pydantic model into the service layer)."""

    def __init__(
        self, medication_id: uuid.UUID, packaging_id: uuid.UUID, quantity: Decimal
    ) -> None:
        self.medication_id = medication_id
        self.packaging_id = packaging_id
        self.quantity = quantity


async def _smallest_unit_factor(
    session: AsyncSession, medication_id: uuid.UUID, level: int
) -> Decimal:
    """Mirrors sales_service._smallest_unit_factor (kept local — no cross-layer
    service dependency for a small, stable conversion helper)."""
    stmt = (
        select(MedicationPackaging.level, MedicationPackaging.qty_in_parent)
        .where(
            MedicationPackaging.medication_id == medication_id,
            MedicationPackaging.is_deleted.is_(False),
            MedicationPackaging.level > level,
        )
        .order_by(MedicationPackaging.level)
    )
    factor = Decimal(1)
    for _deeper_level, qty_in_parent in (await session.execute(stmt)).all():
        if qty_in_parent is None:
            raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="qty_in_parent missing.")
        factor *= Decimal(qty_in_parent)
    return factor


async def create_prescription(
    session: AsyncSession,
    *,
    actor: User,
    branch_id: uuid.UUID,
    customer_id: uuid.UUID | None,
    doctor_name: str,
    doctor_license_no: str | None,
    prescription_date: dt.date,
    notes: str | None,
    items: list[NewPrescriptionItem],
) -> Prescription:
    clean_doctor = doctor_name.strip()
    if not clean_doctor:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Doctor name is required.")
    if not items:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="At least one item is required.")

    prescription = Prescription(
        branch_id=branch_id,
        customer_id=customer_id,
        doctor_name=clean_doctor,
        doctor_license_no=(doctor_license_no.strip() or None) if doctor_license_no else None,
        prescription_date=prescription_date,
        notes_encrypted=_encrypt_notes(notes),
        created_by=actor.id,
    )
    session.add(prescription)
    await session.flush()

    for entry in items:
        if entry.quantity <= 0:
            raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Quantity must be positive.")
        packaging = await session.get(MedicationPackaging, entry.packaging_id)
        if (
            packaging is None
            or packaging.is_deleted
            or packaging.medication_id != entry.medication_id
        ):
            raise ApiError(
                ErrorCode.VALIDATION_FAILED, 422, message="Packaging does not match medication."
            )
        factor = await _smallest_unit_factor(session, entry.medication_id, packaging.level)
        smallest = (entry.quantity * factor).quantize(Decimal("0.001"))
        session.add(
            PrescriptionItem(
                branch_id=branch_id,
                prescription_id=prescription.id,
                medication_id=entry.medication_id,
                packaging_id=entry.packaging_id,
                prescribed_qty=entry.quantity,
                prescribed_qty_smallest=smallest,
                created_by=actor.id,
            )
        )
    await session.commit()
    await session.refresh(prescription)
    return prescription


async def update_prescription(
    session: AsyncSession, *, actor: User, prescription: Prescription, updates: dict[str, Any]
) -> Prescription:
    """Header-only PATCH (doctor info, notes, a manual status override such as
    'cancelled'). Items are immutable once created — see module docstring."""
    if "doctor_name" in updates:
        clean = str(updates["doctor_name"] or "").strip()
        if not clean:
            raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Doctor name is required.")
        prescription.doctor_name = clean
    if "doctor_license_no" in updates:
        val = updates["doctor_license_no"]
        prescription.doctor_license_no = str(val).strip() or None if val else None
    if "notes" in updates:
        prescription.notes_encrypted = _encrypt_notes(updates["notes"])
    if "status" in updates and updates["status"] is not None:
        status = str(updates["status"])
        if status not in _STATUSES:
            raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Unknown status.")
        prescription.status = status
    prescription.updated_by = actor.id
    await session.commit()
    await session.refresh(prescription)
    return prescription


async def list_prescriptions(
    session: AsyncSession,
    *,
    branch_id: uuid.UUID,
    customer_id: uuid.UUID | None = None,
    status: str | None = None,
    skip: int = 0,
    limit: int = 50,
) -> tuple[list[dict[str, object]], int]:
    capped = min(max(limit, 1), MAX_PAGE_SIZE)
    conditions = [Prescription.branch_id == branch_id, Prescription.is_deleted.is_(False)]
    if customer_id is not None:
        conditions.append(Prescription.customer_id == customer_id)
    if status is not None:
        conditions.append(Prescription.status == status)
    total = (
        await session.execute(select(func.count(Prescription.id)).where(*conditions))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(Prescription)
                .where(*conditions)
                .order_by(Prescription.created_at.desc())
                .offset(max(skip, 0))
                .limit(capped)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(p.id),
            "customer_id": str(p.customer_id) if p.customer_id else None,
            "doctor_name": p.doctor_name,
            "prescription_date": p.prescription_date.isoformat(),
            "status": p.status,
            "created_at": p.created_at.isoformat(),
        }
        for p in rows
    ], int(total)


# --------------------------- sale-flow integration (P2-M8) ---------------------------


async def get_item_for_update(session: AsyncSession, item_id: uuid.UUID) -> PrescriptionItem:
    """Row-locked read used by sales_service inside the sale transaction. Also
    validates the PARENT prescription is still open — a cancelled/expired
    prescription must block dispensing even though its items still show
    "remaining" quantity on paper (cancellation doesn't rewrite the items)."""
    item = (
        await session.execute(
            select(PrescriptionItem)
            .where(PrescriptionItem.id == item_id, PrescriptionItem.is_deleted.is_(False))
            .with_for_update()
        )
    ).scalar_one_or_none()
    if item is None:
        raise ApiError(ErrorCode.PRESCRIPTION_INVALID, 422, message="Prescription item not found.")
    prescription = await get_prescription(session, item.prescription_id)
    if prescription.status not in _OPEN_STATUSES:
        raise ApiError(
            ErrorCode.PRESCRIPTION_INVALID, 422, message="Prescription is no longer open."
        )
    return item


async def recompute_status(session: AsyncSession, prescription_id: uuid.UUID) -> None:
    """Re-derive pending/partially_fulfilled/fulfilled from the items' dispensed
    totals — NO commit (called from inside the sale transaction). A prescription
    already 'cancelled'/'expired' (a manual/administrative state) is left alone."""
    prescription = await get_prescription(session, prescription_id)
    if prescription.status not in _OPEN_STATUSES:
        return
    rows = (
        await session.execute(
            select(
                PrescriptionItem.prescribed_qty_smallest, PrescriptionItem.dispensed_qty_smallest
            ).where(
                PrescriptionItem.prescription_id == prescription_id,
                PrescriptionItem.is_deleted.is_(False),
            )
        )
    ).all()
    if not rows:
        return
    all_full = all(Decimal(d) >= Decimal(p) for p, d in rows)
    any_dispensed = any(Decimal(d) > 0 for _p, d in rows)
    prescription.status = (
        "fulfilled" if all_full else ("partially_fulfilled" if any_dispensed else "pending")
    )
