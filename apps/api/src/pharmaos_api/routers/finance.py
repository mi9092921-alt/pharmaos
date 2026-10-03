"""Expenses + categories endpoints (P2-M9).

A SINGLE permission (`finance.expenses`) gates every route here — CLAUDE.md's
matrix does not split this domain into view/create/edit tiers the way it does
for prescriptions.*, so there is only one dependency to apply.
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
from pharmaos_api.services import expense_service as svc

router = APIRouter(prefix="/api/v1", tags=["finance"])

_finance = Depends(require_permission("finance.expenses"))

_PAYMENT_METHOD_PATTERN = "^(cash|card|bank_transfer)$"


class ExpenseCategoryIn(BaseModel):
    name_ar: str = Field(min_length=1, max_length=120)
    name_en: str | None = Field(default=None, max_length=120)


class ExpenseCategoryPatchIn(BaseModel):
    name_ar: str | None = Field(default=None, min_length=1, max_length=120)
    name_en: str | None = Field(default=None, max_length=120)
    is_active: bool | None = None


class ExpenseIn(BaseModel):
    branch_id: uuid.UUID
    expense_category_id: uuid.UUID
    amount: Decimal = Field(gt=0, le=Decimal("10000000"))
    expense_date: dt.date
    description: str | None = Field(default=None, max_length=500)
    payment_method: str = Field(default="cash", pattern=_PAYMENT_METHOD_PATTERN)


class ExpensePatchIn(BaseModel):
    expense_category_id: uuid.UUID | None = None
    amount: Decimal | None = Field(default=None, gt=0, le=Decimal("10000000"))
    expense_date: dt.date | None = None
    description: str | None = Field(default=None, max_length=500)
    payment_method: str | None = Field(default=None, pattern=_PAYMENT_METHOD_PATTERN)


# --------------------------------- categories ---------------------------------


@router.get("/finance/expense-categories")
async def list_expense_categories(
    active_only: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
    _: None = _finance,
) -> dict[str, object]:
    rows = await svc.list_categories(session, active_only=active_only)
    return success_envelope(rows)


@router.post("/finance/expense-categories")
async def create_expense_category(
    body: ExpenseCategoryIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _finance,
) -> dict[str, object]:
    enforce_csrf(request)
    category = await svc.create_category(
        session, actor=actor, name_ar=body.name_ar, name_en=body.name_en
    )
    return success_envelope(svc.category_out(category))


@router.patch("/finance/expense-categories/{category_id}")
async def update_expense_category(
    category_id: uuid.UUID,
    body: ExpenseCategoryPatchIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _finance,
) -> dict[str, object]:
    enforce_csrf(request)
    category = await svc.get_category(session, category_id)
    updated = await svc.update_category(
        session, actor=actor, category=category, changes=body.model_dump(exclude_unset=True)
    )
    return success_envelope(svc.category_out(updated))


# ---------------------------------- expenses ----------------------------------


@router.get("/finance/expenses")
async def list_expenses(
    branch_id: uuid.UUID = Query(),
    category_id: uuid.UUID | None = Query(default=None),
    date_from: dt.date | None = Query(default=None),
    date_to: dt.date | None = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=svc.MAX_PAGE_SIZE),
    session: AsyncSession = Depends(get_session),
    _: None = _finance,
) -> dict[str, object]:
    rows, total = await svc.list_expenses(
        session,
        branch_id=branch_id,
        category_id=category_id,
        date_from=date_from,
        date_to=date_to,
        skip=skip,
        limit=limit,
    )
    return success_envelope(
        rows, meta={"page": skip // limit + 1, "total": total, "per_page": limit}
    )


@router.post("/finance/expenses")
async def create_expense(
    body: ExpenseIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _finance,
) -> dict[str, object]:
    enforce_csrf(request)
    expense = await svc.create_expense(
        session,
        actor=actor,
        branch_id=body.branch_id,
        expense_category_id=body.expense_category_id,
        amount=body.amount,
        expense_date=body.expense_date,
        description=body.description,
        payment_method=body.payment_method,
    )
    return success_envelope(await svc.get_expense_out(session, expense.id))


@router.get("/finance/expenses/{expense_id}")
async def get_expense(
    expense_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: None = _finance,
) -> dict[str, object]:
    return success_envelope(await svc.get_expense_out(session, expense_id))


@router.patch("/finance/expenses/{expense_id}")
async def update_expense(
    expense_id: uuid.UUID,
    body: ExpensePatchIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _finance,
) -> dict[str, object]:
    enforce_csrf(request)
    expense = await svc.get_expense(session, expense_id)
    await svc.update_expense(
        session, actor=actor, expense=expense, changes=body.model_dump(exclude_unset=True)
    )
    return success_envelope(await svc.get_expense_out(session, expense_id))


@router.delete("/finance/expenses/{expense_id}")
async def delete_expense(
    expense_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _finance,
) -> dict[str, object]:
    enforce_csrf(request)
    expense = await svc.get_expense(session, expense_id)
    await svc.delete_expense(session, actor=actor, expense=expense)
    return success_envelope({"deleted": True})
