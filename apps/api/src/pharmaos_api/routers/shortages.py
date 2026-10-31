"""Shortage list endpoints (النواقص).

Permissions: view/list/export/substitutes = inventory.view; all mutations
(create/patch/ask-again/bulk/sync) = inventory.edit (sync writes data — the
owner explicitly moved it OUT of GET); create-po = purchases.create (it
creates a real PO). Mutations enforce CSRF; every mutation is audited.
"""

import uuid

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.db import get_session
from pharmaos_api.deps import get_current_user, require_any_permission, require_permission
from pharmaos_api.errors import success_envelope
from pharmaos_api.models import User
from pharmaos_api.security.csrf import enforce_csrf
from pharmaos_api.services import shortage_service as svc

router = APIRouter(prefix="/api/v1/shortages", tags=["shortages"])

_view = Depends(require_permission("inventory.view"))
_edit = Depends(require_permission("inventory.edit"))
_po = Depends(require_permission("purchases.create"))
# POS "customer asked": a cashier (sales.create) may record a market request —
# staff with inventory.edit may create anything.
_create = Depends(require_any_permission("inventory.edit", "sales.create"))


class CreateShortageIn(BaseModel):
    branch_id: uuid.UUID
    kind: str = Field(pattern="^(pharmacy|market)$")
    source: str = Field(pattern="^(manual|customer_request)$")
    medication_id: uuid.UUID | None = None
    name_text: str | None = Field(default=None, max_length=255)
    qty_needed: str = Field(default="1", pattern=r"^\d+(\.\d{1,3})?$")
    customer_name: str | None = Field(default=None, max_length=255)
    customer_phone: str | None = Field(default=None, max_length=30)
    notes: str | None = Field(default=None, max_length=300)


class PatchShortageIn(BaseModel):
    status: str | None = Field(
        default=None, pattern="^(open|ordered|received|unavailable|cancelled)$"
    )
    supplier_id: uuid.UUID | None = None
    qty_needed: str | None = Field(default=None, pattern=r"^\d+(\.\d{1,3})?$")
    customer_name: str | None = Field(default=None, max_length=255)
    customer_phone: str | None = Field(default=None, max_length=30)
    notes: str | None = Field(default=None, max_length=300)


class BulkIn(BaseModel):
    ids: list[uuid.UUID]
    action: str = Field(pattern="^(mark_ordered|mark_unavailable|cancel|mark_received)$")
    supplier_id: uuid.UUID | None = None


class CreatePoIn(BaseModel):
    branch_id: uuid.UUID
    ids: list[uuid.UUID]
    supplier_id: uuid.UUID
    notes: str | None = Field(default=None, max_length=300)


@router.get("")
async def list_shortages(
    branch_id: uuid.UUID = Query(),
    kind: str | None = Query(default=None),
    status: str | None = Query(default=None),
    search: str | None = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    _: None = _view,
) -> dict[str, object]:
    rows, total = await svc.list_shortages(
        session,
        branch_id=branch_id,
        kind=kind,
        status=status,
        search=search,
        skip=skip,
        limit=limit,
    )
    return success_envelope({"items": rows, "total": total})


@router.post("")
async def create_shortage(
    body: CreateShortageIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _create,
) -> dict[str, object]:
    enforce_csrf(request)
    from decimal import Decimal

    return success_envelope(
        await svc.create_shortage(
            session,
            actor=actor,
            branch_id=body.branch_id,
            kind=body.kind,
            source=body.source,
            medication_id=body.medication_id,
            name_text=body.name_text,
            qty_needed=Decimal(body.qty_needed),
            customer_name=body.customer_name,
            customer_phone=body.customer_phone,
            notes=body.notes,
        )
    )


@router.patch("/{shortage_id}")
async def patch_shortage(
    shortage_id: uuid.UUID,
    body: PatchShortageIn,
    request: Request,
    branch_id: uuid.UUID = Query(),
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _edit,
) -> dict[str, object]:
    enforce_csrf(request)
    from decimal import Decimal

    return success_envelope(
        await svc.patch_shortage(
            session,
            actor=actor,
            branch_id=branch_id,
            shortage_id=shortage_id,
            status=body.status,
            supplier_id=body.supplier_id,
            qty_needed=Decimal(body.qty_needed) if body.qty_needed else None,
            customer_name=body.customer_name,
            customer_phone=body.customer_phone,
            notes=body.notes,
        )
    )


@router.post("/{shortage_id}/ask-again")
async def ask_again(
    shortage_id: uuid.UUID,
    request: Request,
    branch_id: uuid.UUID = Query(),
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _edit,
) -> dict[str, object]:
    enforce_csrf(request)
    return success_envelope(
        await svc.ask_again(session, actor=actor, branch_id=branch_id, shortage_id=shortage_id)
    )


@router.post("/bulk")
async def bulk(
    body: BulkIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _edit,
) -> dict[str, object]:
    """Bulk status transitions (mark_ordered needs a supplier in the payload)."""
    enforce_csrf(request)
    return success_envelope(
        await svc.bulk_transition(
            session, actor=actor, ids=body.ids, action=body.action, supplier_id=body.supplier_id
        )
    )


@router.post("/create-po")
async def create_po(
    body: CreatePoIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _po,
) -> dict[str, object]:
    enforce_csrf(request)
    return success_envelope(
        await svc.create_purchase_order_from_shortages(
            session,
            actor=actor,
            branch_id=body.branch_id,
            shortage_ids=body.ids,
            supplier_id=body.supplier_id,
            notes=body.notes,
        )
    )


@router.post("/sync")
async def sync_auto(
    request: Request,
    branch_id: uuid.UUID = Query(),
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _edit,
) -> dict[str, object]:
    """Idempotent auto-feed of pharmacy shortages from the current low/out
    state. inventory.edit on purpose — this WRITES data (owner point 2)."""
    enforce_csrf(request)
    return success_envelope(await svc.sync_auto(session, branch_id=branch_id))


@router.get("/{shortage_id}/substitutes")
async def substitutes(
    shortage_id: uuid.UUID,
    branch_id: uuid.UUID = Query(),
    session: AsyncSession = Depends(get_session),
    _: None = _view,
) -> dict[str, object]:
    """Same active ingredient, in stock — with the pharmacist warning; never
    auto-inserted into a sale."""
    return success_envelope(
        await svc.substitutes(session, branch_id=branch_id, shortage_id=shortage_id)
    )


@router.get("/export.csv")
async def export_csv(
    branch_id: uuid.UUID = Query(),
    kind: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    _: None = _view,
) -> Response:
    filename, content = await svc.export_csv(session, branch_id=branch_id, kind=kind)
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
