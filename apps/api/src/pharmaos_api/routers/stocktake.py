"""Stocktake endpoints (الجرد): session lifecycle + batch-level counting.

Permissions: create/list-detail/lines/count/submit = inventory.count (the
service additionally rejects cashier counts on controlled-substance lines);
approve/cancel = inventory.stocktake_approve (manager-only). CSV export =
inventory.view (blind-masked until review by the shared serializer).
Mutations enforce CSRF; every mutation is audited.
"""

import uuid

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.db import get_session
from pharmaos_api.deps import get_current_user, require_permission
from pharmaos_api.errors import success_envelope
from pharmaos_api.models import User
from pharmaos_api.security.csrf import enforce_csrf
from pharmaos_api.services import stocktake_service as svc

router = APIRouter(prefix="/api/v1/stocktake", tags=["stocktake"])

_count = Depends(require_permission("inventory.count"))
_approve = Depends(require_permission("inventory.stocktake_approve"))
_view = Depends(require_permission("inventory.view"))


class CreateSessionIn(BaseModel):
    branch_id: uuid.UUID
    session_type: str = Field(pattern="^(daily_quick|weekly_cycle|custom)$")
    blind: bool | None = None
    zones: list[str] | None = None
    movers_limit: int = Field(default=30, ge=1, le=200)
    value_limit: int = Field(default=30, ge=1, le=200)
    notes: str | None = Field(default=None, max_length=500)


class CountIn(BaseModel):
    counted_qty: str = Field(pattern=r"^\d+(\.\d{1,3})?$", description="decimal as string")
    reason_code: str | None = Field(
        default=None, pattern="^(miscount|breakage|theft|found|unknown)$"
    )
    note: str | None = Field(default=None, max_length=300)


class ReasonIn(BaseModel):
    reason: str = Field(min_length=2, max_length=300)


@router.post("/sessions")
async def create_session(
    body: CreateSessionIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _count,
) -> dict[str, object]:
    """Create a stocktake session (scope materialized; counting starts with
    the first recorded count)."""
    enforce_csrf(request)
    return success_envelope(
        await svc.create_session(
            session,
            actor=actor,
            branch_id=body.branch_id,
            session_type=body.session_type,
            blind=body.blind,
            zones=body.zones,
            movers_limit=body.movers_limit,
            value_limit=body.value_limit,
            notes=body.notes,
        )
    )


@router.get("/sessions")
async def list_sessions(
    branch_id: uuid.UUID = Query(),
    status: str | None = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    _: None = _view,
) -> dict[str, object]:
    rows, total = await svc.list_sessions(
        session, branch_id=branch_id, status=status, skip=skip, limit=limit
    )
    return success_envelope({"items": rows, "total": total})


@router.get("/sessions/{session_id}")
async def get_session_detail(
    session_id: uuid.UUID,
    branch_id: uuid.UUID = Query(),
    session: AsyncSession = Depends(get_session),
    _: None = _view,
) -> dict[str, object]:
    return success_envelope(
        await svc.get_session_detail(session, branch_id=branch_id, session_id=session_id)
    )


@router.get("/sessions/{session_id}/lines")
async def list_lines(
    session_id: uuid.UUID,
    branch_id: uuid.UUID = Query(),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    _: None = _view,
) -> dict[str, object]:
    """Lines in shelf-walk order. Blind sessions hide system_qty_at_count/diff
    until review — one serializer for the list, count response and CSV."""
    rows, total = await svc.list_lines(
        session, branch_id=branch_id, session_id=session_id, skip=skip, limit=limit
    )
    return success_envelope({"items": rows, "total": total})


@router.post("/lines/{line_id}/count")
async def record_count(
    line_id: uuid.UUID,
    body: CountIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _count,
) -> dict[str, object]:
    """Record (or re-record) one batch count — batch locked, quantity
    snapshotted under the lock. Controlled lines: pharmacist+ only."""
    enforce_csrf(request)
    from decimal import Decimal

    return success_envelope(
        await svc.record_count(
            session,
            actor=actor,
            line_id=line_id,
            counted_qty=Decimal(body.counted_qty),
            reason_code=body.reason_code,
            note=body.note,
        )
    )


@router.post("/sessions/{session_id}/submit")
async def submit_session(
    session_id: uuid.UUID,
    request: Request,
    branch_id: uuid.UUID = Query(),
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _count,
) -> dict[str, object]:
    """counting -> review (the review screen settles stale lines by recount)."""
    enforce_csrf(request)
    return success_envelope(
        await svc.submit(session, actor=actor, branch_id=branch_id, session_id=session_id)
    )


@router.post("/sessions/{session_id}/approve")
async def approve_session(
    session_id: uuid.UUID,
    body: ReasonIn,
    request: Request,
    branch_id: uuid.UUID = Query(),
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _approve,
) -> dict[str, object]:
    """Apply all counted diffs in ONE transaction (deadlock-safe, one retry);
    stale lines flag needs_recount instead of failing the session."""
    enforce_csrf(request)
    return success_envelope(
        await svc.approve(
            session, actor=actor, branch_id=branch_id, session_id=session_id, reason=body.reason
        )
    )


@router.post("/sessions/{session_id}/cancel")
async def cancel_session(
    session_id: uuid.UUID,
    body: ReasonIn,
    request: Request,
    branch_id: uuid.UUID = Query(),
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _approve,
) -> dict[str, object]:
    enforce_csrf(request)
    return success_envelope(
        await svc.cancel(
            session, actor=actor, branch_id=branch_id, session_id=session_id, reason=body.reason
        )
    )


@router.get("/sessions/{session_id}/export.csv")
async def export_csv(
    session_id: uuid.UUID,
    branch_id: uuid.UUID = Query(),
    session: AsyncSession = Depends(get_session),
    _: None = _view,
) -> object:
    """Lines CSV (csv_safe); blind-masked until review like every surface."""
    from fastapi.responses import Response

    filename, content = await svc.export_csv(session, branch_id=branch_id, session_id=session_id)
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
