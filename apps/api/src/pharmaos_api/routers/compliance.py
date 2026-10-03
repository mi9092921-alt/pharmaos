"""Egyptian compliance endpoints (P2-M10 ETA e-receipt; P2-M11 EDA T&T).

Read the outbox queues and trigger the drain worker on demand. Permissions:
  compliance.ereceipt  -> super_admin, branch_manager
  compliance.tt_report -> super_admin, branch_manager, pharmacist
Drains are CSRF-protected. The drain endpoints stand in for the background
worker in this environment (production also runs a scheduled Celery drain).
"""

import uuid

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.db import get_session
from pharmaos_api.deps import get_current_user, require_permission
from pharmaos_api.errors import ApiError, ErrorCode, success_envelope
from pharmaos_api.models import User
from pharmaos_api.security.csrf import enforce_csrf
from pharmaos_api.services.compliance import ereceipt_service, tt_service

router = APIRouter(prefix="/api/v1/compliance", tags=["compliance"])

_ereceipt = Depends(require_permission("compliance.ereceipt"))
_tt = Depends(require_permission("compliance.tt_report"))


class DrainIn(BaseModel):
    branch_id: uuid.UUID


class TtImportRow(BaseModel):
    gtin: str = Field(min_length=1, max_length=14)
    serial_number: str = Field(min_length=1, max_length=64)
    batch_number: str | None = Field(default=None, max_length=50)
    expiry_date: str | None = None


class TtImportIn(BaseModel):
    branch_id: uuid.UUID
    rows: list[TtImportRow] = Field(min_length=1, max_length=1000)


@router.get("/ereceipts")
async def list_ereceipts(
    branch_id: uuid.UUID = Query(),
    status: str | None = Query(default=None, max_length=20),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=ereceipt_service.MAX_PAGE_SIZE),
    session: AsyncSession = Depends(get_session),
    _: None = _ereceipt,
) -> dict[str, object]:
    rows, total = await ereceipt_service.list_queue(
        session, branch_id=branch_id, status=status, skip=skip, limit=limit
    )
    return success_envelope(
        rows, meta={"page": skip // limit + 1, "total": total, "per_page": limit}
    )


@router.get("/ereceipts/by-invoice/{invoice_id}")
async def ereceipt_for_invoice(
    invoice_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: None = _ereceipt,
) -> dict[str, object]:
    row = await ereceipt_service.get_for_invoice(session, invoice_id)
    if row is None:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 404, message="No e-receipt for this invoice.")
    return success_envelope(row)


@router.post("/ereceipts/drain")
async def drain_ereceipts(
    body: DrainIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _ereceipt,
) -> dict[str, object]:
    enforce_csrf(request)
    result = await ereceipt_service.drain(session, branch_id=body.branch_id, actor=actor)
    return success_envelope(result)


# --------------------------- EDA track & trace (P2-M11) ---------------------------


@router.get("/tt-events")
async def list_tt_events(
    branch_id: uuid.UUID = Query(),
    status: str | None = Query(default=None, max_length=20),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=tt_service.MAX_PAGE_SIZE),
    session: AsyncSession = Depends(get_session),
    _: None = _tt,
) -> dict[str, object]:
    rows, total = await tt_service.list_events(
        session, branch_id=branch_id, status=status, skip=skip, limit=limit
    )
    return success_envelope(
        rows, meta={"page": skip // limit + 1, "total": total, "per_page": limit}
    )


@router.post("/tt-events/drain")
async def drain_tt_events(
    body: DrainIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _tt,
) -> dict[str, object]:
    enforce_csrf(request)
    result = await tt_service.drain(session, branch_id=body.branch_id, actor=actor)
    return success_envelope(result)


@router.post("/tt-events/import")
async def import_tt_events(
    body: TtImportIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _tt,
) -> dict[str, object]:
    """Backfill pre-launch serial records (no data gap at go-live)."""
    enforce_csrf(request)
    count = await tt_service.import_events(
        session,
        actor=actor,
        branch_id=body.branch_id,
        rows=[r.model_dump() for r in body.rows],
    )
    return success_envelope({"imported": count})
