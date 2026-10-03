"""Cashier endpoints (P1-M10): cash sessions + Z-report.

Permission tiers per the CLAUDE.md matrix:
  open/current = cashier.open_session (super_admin, branch_manager, cashier)
  close        = cashier.close_session (super_admin, branch_manager — the
                 manager verifies the count and closes the cashier's drawer)
  lists/report = cashier.view_cash (super_admin, branch_manager)
Mutations enforce CSRF. Audited: cash_session.opened / .closed / .discrepancy.
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
from pharmaos_api.models import CashSession, User
from pharmaos_api.security.csrf import enforce_csrf
from pharmaos_api.services import cashier_service as svc

router = APIRouter(prefix="/api/v1/cashier", tags=["cashier"])

_open = Depends(require_permission("cashier.open_session"))
_close = Depends(require_permission("cashier.close_session"))
_view = Depends(require_permission("cashier.view_cash"))


def _session_json(s: CashSession) -> dict[str, object]:
    return {
        "id": str(s.id),
        "branch_id": str(s.branch_id),
        "cashier_id": str(s.cashier_id),
        "status": s.status,
        "opening_float": str(s.opening_float),
        "opened_at": s.opened_at.isoformat(),
        "closed_at": s.closed_at.isoformat() if s.closed_at else None,
        "expected_cash": str(s.expected_cash) if s.expected_cash is not None else None,
        "counted_cash": str(s.counted_cash) if s.counted_cash is not None else None,
        "discrepancy": str(s.discrepancy) if s.discrepancy is not None else None,
        "closing_notes": s.closing_notes,
    }


class OpenIn(BaseModel):
    branch_id: uuid.UUID
    opening_float: Decimal = Field(default=Decimal(0), ge=0, le=Decimal("1000000"))


class CloseIn(BaseModel):
    counted_cash: Decimal = Field(ge=0, le=Decimal("100000000"))
    notes: str | None = Field(default=None, max_length=500)


@router.post("/sessions/open")
async def open_session(
    body: OpenIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _open,
) -> dict[str, object]:
    """Open the cashier's drawer session (one open session per cashier/branch)."""
    enforce_csrf(request)
    row = await svc.open_session(
        session, actor=actor, branch_id=body.branch_id, opening_float=body.opening_float
    )
    return success_envelope(_session_json(row))


@router.get("/sessions/current")
async def current_session(
    branch_id: uuid.UUID = Query(),
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _open,
) -> dict[str, object]:
    """The caller's open session in this branch (+ live drawer summary), or null."""
    row = await svc.get_open_session(session, branch_id=branch_id, cashier_id=actor.id)
    if row is None:
        return success_envelope({"session": None, "summary": None})
    return success_envelope(
        {"session": _session_json(row), "summary": await svc.session_summary(session, row)}
    )


@router.post("/sessions/{session_id}/close")
async def close_session(
    session_id: uuid.UUID,
    body: CloseIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _close,
) -> dict[str, object]:
    """Close a drawer with the physical count — freezes the Z numbers + audits."""
    enforce_csrf(request)
    row = await svc.get_session(session, session_id)
    row = await svc.close_session(
        session, actor=actor, cash_session=row, counted_cash=body.counted_cash, notes=body.notes
    )
    return success_envelope(_session_json(row))


@router.get("/sessions")
async def list_sessions(
    branch_id: uuid.UUID = Query(),
    day: dt.date | None = Query(default=None),
    status: str | None = Query(default=None, pattern="^(open|closed)$"),
    session: AsyncSession = Depends(get_session),
    _: None = _view,
) -> dict[str, object]:
    return success_envelope(
        await svc.list_sessions(session, branch_id=branch_id, day=day, status=status)
    )


@router.get("/sessions/{session_id}/report")
async def session_report(
    session_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: None = _view,
) -> dict[str, object]:
    """Z snapshot for one session: frozen numbers once closed, live while open."""
    row = await svc.get_session(session, session_id)
    return success_envelope(
        {"session": _session_json(row), "summary": await svc.session_summary(session, row)}
    )


@router.get("/z-report")
async def z_report(
    branch_id: uuid.UUID = Query(),
    day: dt.date | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    _: None = _view,
) -> dict[str, object]:
    """End-of-day totals for the branch (defaults to the local today)."""
    report_day = day or dt.date.today()
    return success_envelope(await svc.day_report(session, branch_id=branch_id, day=report_day))
