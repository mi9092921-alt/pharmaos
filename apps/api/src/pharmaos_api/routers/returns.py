"""Returns / credit notes (P2-M7).

  create   = sales.return (super_admin, branch_manager, pharmacist) + CSRF
  view     = sales.view (all roles)
A return never modifies the original invoice (CLAUDE.md rule 14); it puts stock
back and records a negative payment (refund) — all in the service layer.
"""

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
from pharmaos_api.services import return_service as svc

router = APIRouter(prefix="/api/v1", tags=["returns"])

_view = Depends(require_permission("sales.view"))
_return = Depends(require_permission("sales.return"))


class ReturnLineIn(BaseModel):
    invoice_item_id: uuid.UUID
    quantity: Decimal = Field(gt=0, le=Decimal("100000"))


class ReturnIn(BaseModel):
    original_invoice_id: uuid.UUID
    lines: list[ReturnLineIn] = Field(min_length=1, max_length=200)
    reason: str | None = Field(default=None, max_length=500)
    refund_method: str = Field(default="cash", pattern="^(cash|card|store_credit)$")


@router.get("/invoices/lookup")
async def lookup_invoice(
    branch_id: uuid.UUID = Query(),
    invoice_number: str = Query(min_length=1, max_length=30),
    session: AsyncSession = Depends(get_session),
    _: None = _view,
) -> dict[str, object]:
    """Resolve a human-facing invoice number (as printed on the receipt) and
    return its returnable lines — the entry point for the returns UI."""
    return success_envelope(
        await svc.get_returnable_by_number(
            session, branch_id=branch_id, invoice_number=invoice_number
        )
    )


@router.get("/invoices/{invoice_id}/returnable")
async def returnable(
    invoice_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: None = _view,
) -> dict[str, object]:
    """Per-line sold / returned / still-returnable quantities for the return UI."""
    return success_envelope(await svc.get_returnable(session, invoice_id))


@router.post("/returns")
async def create_return(
    body: ReturnIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _return,
) -> dict[str, object]:
    enforce_csrf(request)
    credit_note = await svc.create_return(
        session,
        actor=actor,
        original_invoice_id=body.original_invoice_id,
        lines=[
            svc.ReturnLine(invoice_item_id=x.invoice_item_id, quantity=x.quantity)
            for x in body.lines
        ],
        reason=body.reason,
        refund_method=body.refund_method,
    )
    return success_envelope(await svc.get_return(session, credit_note.id))


@router.get("/returns")
async def list_returns(
    branch_id: uuid.UUID = Query(),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=svc.MAX_PAGE_SIZE),
    session: AsyncSession = Depends(get_session),
    _: None = _view,
) -> dict[str, object]:
    rows, total = await svc.list_returns(session, branch_id=branch_id, skip=skip, limit=limit)
    return success_envelope(
        rows, meta={"page": skip // limit + 1, "total": total, "per_page": limit}
    )


@router.get("/returns/{return_id}")
async def get_return(
    return_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: None = _view,
) -> dict[str, object]:
    return success_envelope(await svc.get_return(session, return_id))
