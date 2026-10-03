"""Prescription + controlled-substance register endpoints (P2-M8).

Permission tiers (packages/shared/permissions.ts — added this milestone, no
prior CLAUDE.md matrix entry existed for this domain):
  prescriptions.view/create/edit          -> super_admin, branch_manager, pharmacist
  controlled_substances.view (read-only)  -> super_admin, branch_manager, pharmacist
Mutations enforce CSRF. Items are immutable once created (see prescription_service).
"""

import datetime as dt
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.db import get_session
from pharmaos_api.deps import get_current_user, require_permission
from pharmaos_api.errors import success_envelope
from pharmaos_api.models import User
from pharmaos_api.security.csrf import enforce_csrf
from pharmaos_api.services import controlled_substance_service
from pharmaos_api.services import prescription_service as svc

router = APIRouter(prefix="/api/v1", tags=["prescriptions"])

_view = Depends(require_permission("prescriptions.view"))
_create = Depends(require_permission("prescriptions.create"))
_edit = Depends(require_permission("prescriptions.edit"))
_controlled_view = Depends(require_permission("controlled_substances.view"))


class PrescriptionItemIn(BaseModel):
    medication_id: uuid.UUID
    packaging_id: uuid.UUID
    quantity: Decimal = Field(gt=0, le=Decimal("100000"))


class PrescriptionIn(BaseModel):
    branch_id: uuid.UUID
    customer_id: uuid.UUID | None = None
    doctor_name: str = Field(min_length=1, max_length=160)
    doctor_license_no: str | None = Field(default=None, max_length=50)
    prescription_date: dt.date
    notes: str | None = Field(default=None, max_length=2000)
    items: list[PrescriptionItemIn] = Field(min_length=1, max_length=50)


class PrescriptionPatchIn(BaseModel):
    doctor_name: str | None = Field(default=None, min_length=1, max_length=160)
    doctor_license_no: str | None = Field(default=None, max_length=50)
    notes: str | None = Field(default=None, max_length=2000)
    status: str | None = Field(
        default=None, pattern="^(pending|partially_fulfilled|fulfilled|expired|cancelled)$"
    )


@router.get("/prescriptions")
async def list_prescriptions(
    branch_id: uuid.UUID = Query(),
    customer_id: uuid.UUID | None = Query(default=None),
    status: str | None = Query(default=None, max_length=20),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=svc.MAX_PAGE_SIZE),
    session: AsyncSession = Depends(get_session),
    _: None = _view,
) -> dict[str, object]:
    rows, total = await svc.list_prescriptions(
        session, branch_id=branch_id, customer_id=customer_id, status=status, skip=skip, limit=limit
    )
    return success_envelope(
        rows, meta={"page": skip // limit + 1, "total": total, "per_page": limit}
    )


@router.post("/prescriptions")
async def create_prescription(
    body: PrescriptionIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _create,
) -> dict[str, object]:
    enforce_csrf(request)
    prescription = await svc.create_prescription(
        session,
        actor=actor,
        branch_id=body.branch_id,
        customer_id=body.customer_id,
        doctor_name=body.doctor_name,
        doctor_license_no=body.doctor_license_no,
        prescription_date=body.prescription_date,
        notes=body.notes,
        items=[
            svc.NewPrescriptionItem(x.medication_id, x.packaging_id, x.quantity) for x in body.items
        ],
    )
    return success_envelope(await svc.get_prescription_out(session, prescription.id))


@router.get("/prescriptions/{prescription_id}")
async def get_prescription(
    prescription_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: None = _view,
) -> dict[str, object]:
    return success_envelope(await svc.get_prescription_out(session, prescription_id))


@router.patch("/prescriptions/{prescription_id}")
async def update_prescription(
    prescription_id: uuid.UUID,
    body: PrescriptionPatchIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _edit,
) -> dict[str, object]:
    enforce_csrf(request)
    prescription = await svc.get_prescription(session, prescription_id)
    await svc.update_prescription(
        session, actor=actor, prescription=prescription, updates=body.model_dump(exclude_unset=True)
    )
    return success_envelope(await svc.get_prescription_out(session, prescription_id))


@router.get("/controlled-substances/log")
async def controlled_substance_log(
    branch_id: uuid.UUID = Query(),
    medication_id: uuid.UUID | None = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=controlled_substance_service.MAX_PAGE_SIZE),
    session: AsyncSession = Depends(get_session),
    _: None = _controlled_view,
) -> dict[str, object]:
    """The append-only dispensing register (read-only — never manually created)."""
    rows, total = await controlled_substance_service.list_log(
        session, branch_id=branch_id, medication_id=medication_id, skip=skip, limit=limit
    )
    return success_envelope(
        rows, meta={"page": skip // limit + 1, "total": total, "per_page": limit}
    )
