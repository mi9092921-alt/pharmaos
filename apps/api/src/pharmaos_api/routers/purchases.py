"""Purchasing endpoints: supplier management (P2-M1) + purchase orders (P2-M2).

Permission tiers (CLAUDE.md matrix):
  view              = purchases.view    (super_admin, branch_manager, pharmacist)
  create/edit/submit/cancel = purchases.create (super_admin, branch_manager)
  approve           = purchases.approve (super_admin, branch_manager)
  receive (goods-in)= purchases.receive (super_admin, branch_manager, pharmacist)
Mutations enforce CSRF; lists are paginated (<=100).
"""

import uuid
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.db import get_session
from pharmaos_api.deps import get_current_user, require_permission
from pharmaos_api.errors import success_envelope
from pharmaos_api.models import User
from pharmaos_api.security.csrf import enforce_csrf
from pharmaos_api.services import purchase_service as posvc
from pharmaos_api.services import supplier_service as svc

router = APIRouter(prefix="/api/v1", tags=["purchases"])

_view = Depends(require_permission("purchases.view"))
_manage = Depends(require_permission("purchases.create"))
_approve = Depends(require_permission("purchases.approve"))
_receive = Depends(require_permission("purchases.receive"))

_MAX = Decimal("1000000")


# ------------------------------ suppliers (P2-M1) ------------------------------


class SupplierCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    contact_name: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=255)
    address: str | None = Field(default=None, max_length=500)
    tax_registration_no: str | None = Field(default=None, max_length=50)
    payment_terms: str | None = Field(default=None, max_length=120)
    notes: str | None = Field(default=None, max_length=2000)


class SupplierUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    contact_name: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=255)
    address: str | None = Field(default=None, max_length=500)
    tax_registration_no: str | None = Field(default=None, max_length=50)
    payment_terms: str | None = Field(default=None, max_length=120)
    is_active: bool | None = None
    notes: str | None = Field(default=None, max_length=2000)


@router.get("/purchases/suppliers")
async def list_suppliers(
    search: str | None = Query(default=None, max_length=120),
    active_only: bool = Query(default=False),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=svc.MAX_PAGE_SIZE),
    session: AsyncSession = Depends(get_session),
    _: None = _view,
) -> dict[str, object]:
    rows, total = await svc.list_suppliers(
        session, search=search, active_only=active_only, skip=skip, limit=limit
    )
    return success_envelope(
        rows, meta={"page": skip // limit + 1, "total": total, "per_page": limit}
    )


@router.get("/purchases/suppliers/{supplier_id}")
async def get_supplier(
    supplier_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: None = _view,
) -> dict[str, object]:
    return success_envelope(svc.to_dict(await svc.get_supplier(session, supplier_id)))


@router.post("/purchases/suppliers")
async def create_supplier(
    body: SupplierCreateIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _manage,
) -> dict[str, object]:
    enforce_csrf(request)
    supplier = await svc.create_supplier(
        session, actor=actor, **body.model_dump(exclude_unset=True)
    )
    return success_envelope(svc.to_dict(supplier))


@router.patch("/purchases/suppliers/{supplier_id}")
async def update_supplier(
    supplier_id: uuid.UUID,
    body: SupplierUpdateIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _manage,
) -> dict[str, object]:
    enforce_csrf(request)
    supplier = await svc.get_supplier(session, supplier_id)
    supplier = await svc.update_supplier(
        session, actor=actor, supplier=supplier, changes=body.model_dump(exclude_unset=True)
    )
    return success_envelope(svc.to_dict(supplier))


# ------------------------------ purchase orders (P2-M2) ------------------------------


class PurchaseLineIn(BaseModel):
    medication_id: uuid.UUID
    packaging_id: uuid.UUID
    qty_ordered: Decimal = Field(gt=0, le=_MAX)
    unit_cost: Decimal = Field(ge=0, le=_MAX)


class PurchaseOrderCreateIn(BaseModel):
    branch_id: uuid.UUID
    supplier_id: uuid.UUID
    expected_date: date | None = None
    notes: str | None = Field(default=None, max_length=2000)
    lines: list[PurchaseLineIn] = Field(min_length=1)


class ReceiptLineIn(BaseModel):
    purchase_item_id: uuid.UUID
    batch_number: str = Field(min_length=1, max_length=50)
    expiry_date: date
    quantity: Decimal = Field(gt=0, le=_MAX)
    # P2-M3: 2D-scanned pack serials + their GTIN (both optional).
    gtin: str | None = Field(default=None, max_length=14)
    serials: list[str] = Field(default_factory=list, max_length=1000)


class ReceiveIn(BaseModel):
    receipts: list[ReceiptLineIn] = Field(min_length=1)


async def _order_response(session: AsyncSession, po_id: uuid.UUID) -> dict[str, object]:
    po = await posvc.get_purchase_order(session, po_id)
    return success_envelope(posvc.to_dict(po, await posvc.get_items(session, po_id)))


@router.get("/purchases/orders")
async def list_orders(
    branch_id: uuid.UUID | None = Query(default=None),
    status: str | None = Query(default=None, max_length=20),
    supplier_id: uuid.UUID | None = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=posvc.MAX_PAGE_SIZE),
    session: AsyncSession = Depends(get_session),
    _: None = _view,
) -> dict[str, object]:
    rows, total = await posvc.list_purchase_orders(
        session, branch_id=branch_id, status=status, supplier_id=supplier_id, skip=skip, limit=limit
    )
    return success_envelope(
        rows, meta={"page": skip // limit + 1, "total": total, "per_page": limit}
    )


@router.get("/purchases/orders/{po_id}")
async def get_order(
    po_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: None = _view,
) -> dict[str, object]:
    return await _order_response(session, po_id)


@router.post("/purchases/orders")
async def create_order(
    body: PurchaseOrderCreateIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _manage,
) -> dict[str, object]:
    enforce_csrf(request)
    po, items = await posvc.create_purchase_order(
        session,
        actor=actor,
        branch_id=body.branch_id,
        supplier_id=body.supplier_id,
        expected_date=body.expected_date,
        notes=body.notes,
        lines=[
            posvc.PurchaseLineIn(
                medication_id=line.medication_id,
                packaging_id=line.packaging_id,
                qty_ordered=line.qty_ordered,
                unit_cost=line.unit_cost,
            )
            for line in body.lines
        ],
    )
    return success_envelope(posvc.to_dict(po, items))


@router.post("/purchases/orders/{po_id}/submit")
async def submit_order(
    po_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _manage,
) -> dict[str, object]:
    enforce_csrf(request)
    po = await posvc.get_purchase_order(session, po_id)
    await posvc.submit(session, actor=actor, po=po)
    return await _order_response(session, po_id)


@router.post("/purchases/orders/{po_id}/approve")
async def approve_order(
    po_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _approve,
) -> dict[str, object]:
    enforce_csrf(request)
    po = await posvc.get_purchase_order(session, po_id)
    await posvc.approve(session, actor=actor, po=po)
    return await _order_response(session, po_id)


@router.post("/purchases/orders/{po_id}/cancel")
async def cancel_order(
    po_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _manage,
) -> dict[str, object]:
    enforce_csrf(request)
    po = await posvc.get_purchase_order(session, po_id)
    await posvc.cancel(session, actor=actor, po=po)
    return await _order_response(session, po_id)


@router.post("/purchases/orders/{po_id}/receive")
async def receive_order(
    po_id: uuid.UUID,
    body: ReceiveIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _receive,
) -> dict[str, object]:
    enforce_csrf(request)
    po = await posvc.get_purchase_order(session, po_id)
    await posvc.receive(
        session,
        actor=actor,
        po=po,
        receipts=[
            posvc.ReceiptLineIn(
                purchase_item_id=line.purchase_item_id,
                batch_number=line.batch_number,
                expiry_date=line.expiry_date,
                quantity=line.quantity,
                gtin=line.gtin,
                serials=line.serials,
            )
            for line in body.receipts
        ],
    )
    return await _order_response(session, po_id)
