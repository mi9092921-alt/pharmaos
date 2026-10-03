"""Customer endpoints (P2-M5): CRUD with the customers.* permission tiers, the
loyalty ledger (view + manual adjust), and purchase history.

Permission tiers (CLAUDE.md matrix):
  view   = customers.view   (all roles)
  create = customers.create (super_admin, branch_manager, pharmacist, cashier)
  edit   = customers.edit   (super_admin, branch_manager, pharmacist) — also gates
           manual loyalty adjustments
  delete = customers.delete (super_admin)
Mutations enforce CSRF; lists are paginated (<=100). PII (national_id,
insurance_number) is decrypted only on the single-customer detail read.
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
from pharmaos_api.services import customer_service as svc

router = APIRouter(prefix="/api/v1", tags=["customers"])

_view = Depends(require_permission("customers.view"))
_create = Depends(require_permission("customers.create"))
_edit = Depends(require_permission("customers.edit"))
_delete = Depends(require_permission("customers.delete"))


class CustomerIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    phone: str | None = Field(default=None, max_length=32)
    national_id: str | None = Field(default=None, max_length=32)
    insurance_number: str | None = Field(default=None, max_length=64)
    notes: str | None = Field(default=None, max_length=2000)


class CustomerUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    phone: str | None = Field(default=None, max_length=32)
    national_id: str | None = Field(default=None, max_length=32)
    insurance_number: str | None = Field(default=None, max_length=64)
    notes: str | None = Field(default=None, max_length=2000)
    is_active: bool | None = None


class LoyaltyAdjustIn(BaseModel):
    points_delta: int = Field(ge=-1_000_000, le=1_000_000)
    reason: str = Field(min_length=1, max_length=500)


@router.get("/customers")
async def list_customers(
    search: str | None = Query(default=None, max_length=120),
    active_only: bool = Query(default=False),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=svc.MAX_PAGE_SIZE),
    session: AsyncSession = Depends(get_session),
    _: None = _view,
) -> dict[str, object]:
    rows, total = await svc.list_customers(
        session, search=search, active_only=active_only, skip=skip, limit=limit
    )
    return success_envelope(
        rows, meta={"page": skip // limit + 1, "total": total, "per_page": limit}
    )


@router.post("/customers")
async def create_customer(
    body: CustomerIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _create,
) -> dict[str, object]:
    enforce_csrf(request)
    customer = await svc.create_customer(
        session,
        actor=actor,
        name=body.name,
        phone=body.phone,
        national_id=body.national_id,
        insurance_number=body.insurance_number,
        notes=body.notes,
    )
    return success_envelope(svc.detail(customer))


@router.get("/customers/{customer_id}")
async def get_customer(
    customer_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: None = _view,
) -> dict[str, object]:
    """Single-customer authorized read — includes decrypted PII."""
    customer = await svc.get_customer(session, customer_id)
    return success_envelope(svc.detail(customer))


@router.patch("/customers/{customer_id}")
async def update_customer(
    customer_id: uuid.UUID,
    body: CustomerUpdateIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _edit,
) -> dict[str, object]:
    enforce_csrf(request)
    customer = await svc.get_customer(session, customer_id)
    customer = await svc.update_customer(
        session, actor=actor, customer=customer, updates=body.model_dump(exclude_unset=True)
    )
    return success_envelope(svc.detail(customer))


@router.delete("/customers/{customer_id}")
async def delete_customer(
    customer_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _delete,
) -> dict[str, object]:
    enforce_csrf(request)
    customer = await svc.get_customer(session, customer_id)
    await svc.delete_customer(session, actor=actor, customer=customer)
    return success_envelope({"deleted": True})


@router.get("/customers/{customer_id}/loyalty")
async def list_loyalty(
    customer_id: uuid.UUID,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=svc.MAX_PAGE_SIZE),
    session: AsyncSession = Depends(get_session),
    _: None = _view,
) -> dict[str, object]:
    customer = await svc.get_customer(session, customer_id)
    rows, total = await svc.list_loyalty(session, customer_id=customer.id, skip=skip, limit=limit)
    return success_envelope(
        {"balance": int(customer.loyalty_points), "transactions": rows},
        meta={"page": skip // limit + 1, "total": total, "per_page": limit},
    )


@router.post("/customers/{customer_id}/loyalty")
async def adjust_loyalty(
    customer_id: uuid.UUID,
    body: LoyaltyAdjustIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _edit,
) -> dict[str, object]:
    """Manual loyalty adjustment (customers.edit). Cannot go below zero."""
    enforce_csrf(request)
    customer = await svc.get_customer(session, customer_id)
    customer = await svc.adjust_points(
        session, actor=actor, customer=customer, points_delta=body.points_delta, reason=body.reason
    )
    return success_envelope(svc.detail(customer))


@router.get("/customers/{customer_id}/history")
async def customer_history(
    customer_id: uuid.UUID,
    limit: int = Query(default=20, ge=1, le=svc.MAX_PAGE_SIZE),
    session: AsyncSession = Depends(get_session),
    _: None = _view,
) -> dict[str, object]:
    customer = await svc.get_customer(session, customer_id)
    history = await svc.customer_history(session, customer_id=customer.id, limit=limit)
    return success_envelope(history)
