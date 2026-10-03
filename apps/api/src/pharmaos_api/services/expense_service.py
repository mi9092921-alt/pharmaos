"""Expenses + categories (P2-M9): full CRUD over both, gated by the single
`finance.expenses` permission (CLAUDE.md does not split this domain into
view/create/edit tiers, unlike prescriptions.*).

Notes:
- expense_categories is GLOBAL (see the migration's module comment — one
  shared chart of expense categories across all branches). Same
  deactivate-don't-delete convention as suppliers: is_active toggle, no
  dedicated delete route (existing expenses keep a valid FK regardless).
- currency_code is ALWAYS derived from the branch server-side (never trusted
  from client input) — same rule as sales_service/purchase_service.
- No CLAUDE.md AUDITED_OPERATIONS entry exists for this domain (same
  precedent as suppliers/customers/loyalty/purchase-orders), so expense
  writes are NOT routed through audit_service — created_by/updated_by are
  the accountability trail here, matching every other undocumented-audit
  domain in this codebase.
"""

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.errors import ApiError, ErrorCode
from pharmaos_api.models import Branch, Expense, ExpenseCategory, User
from pharmaos_api.services import cashier_service

MAX_PAGE_SIZE = 100
PAYMENT_METHODS = frozenset({"cash", "card", "bank_transfer"})


# --------------------------------- categories ---------------------------------


def category_out(c: ExpenseCategory) -> dict[str, object]:
    return {
        "id": str(c.id),
        "name_ar": c.name_ar,
        "name_en": c.name_en,
        "is_active": c.is_active,
    }


async def list_categories(
    session: AsyncSession, *, active_only: bool = False
) -> list[dict[str, object]]:
    conditions = [ExpenseCategory.is_deleted.is_(False)]
    if active_only:
        conditions.append(ExpenseCategory.is_active.is_(True))
    rows = (
        (
            await session.execute(
                select(ExpenseCategory).where(*conditions).order_by(ExpenseCategory.name_ar)
            )
        )
        .scalars()
        .all()
    )
    return [category_out(c) for c in rows]


async def get_category(session: AsyncSession, category_id: uuid.UUID) -> ExpenseCategory:
    category = (
        await session.execute(
            select(ExpenseCategory).where(
                ExpenseCategory.id == category_id, ExpenseCategory.is_deleted.is_(False)
            )
        )
    ).scalar_one_or_none()
    if category is None:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 404, message="Expense category not found.")
    return category


async def create_category(
    session: AsyncSession, *, actor: User, name_ar: str, name_en: str | None = None
) -> ExpenseCategory:
    clean = name_ar.strip()
    if not clean:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="name_ar is required.")
    category = ExpenseCategory(
        name_ar=clean,
        name_en=(name_en.strip() or None) if name_en else None,
        created_by=actor.id,
        updated_by=actor.id,
    )
    session.add(category)
    await session.commit()
    await session.refresh(category)
    return category


async def update_category(
    session: AsyncSession, *, actor: User, category: ExpenseCategory, changes: dict[str, Any]
) -> ExpenseCategory:
    """PATCH semantics — only the provided keys are applied."""
    if "name_ar" in changes:
        clean = str(changes["name_ar"] or "").strip()
        if not clean:
            raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="name_ar is required.")
        category.name_ar = clean
    if "name_en" in changes:
        val = changes["name_en"]
        category.name_en = str(val).strip() or None if val else None
    if "is_active" in changes:
        category.is_active = bool(changes["is_active"])
    category.updated_by = actor.id
    await session.commit()
    await session.refresh(category)
    return category


# ---------------------------------- expenses ----------------------------------


def _expense_out(e: Expense, category: ExpenseCategory) -> dict[str, object]:
    return {
        "id": str(e.id),
        "branch_id": str(e.branch_id),
        "expense_category_id": str(e.expense_category_id),
        "category_name_ar": category.name_ar,
        "category_name_en": category.name_en,
        "amount": str(e.amount),
        "currency_code": e.currency_code,
        "expense_date": e.expense_date.isoformat(),
        "description": e.description,
        "payment_method": e.payment_method,
        "created_at": e.created_at.isoformat(),
    }


async def _active_category_or_422(session: AsyncSession, category_id: uuid.UUID) -> ExpenseCategory:
    category = await get_category(session, category_id)
    if not category.is_active:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Expense category is not active.")
    return category


async def create_expense(
    session: AsyncSession,
    *,
    actor: User,
    branch_id: uuid.UUID,
    expense_category_id: uuid.UUID,
    amount: Decimal,
    expense_date: dt.date,
    description: str | None = None,
    payment_method: str = "cash",
) -> Expense:
    if amount <= 0:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Amount must be positive.")
    if payment_method not in PAYMENT_METHODS:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Unknown payment method.")
    branch = await session.get(Branch, branch_id)
    if branch is None or branch.is_deleted:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Unknown branch.")
    await _active_category_or_422(session, expense_category_id)

    # C5: a CASH expense taken from the drawer links to the actor's open session
    # (if any) so it reduces that session's expected cash. Card/bank-transfer
    # expenses never touch the drawer, so they carry no session link.
    cash_session_id: uuid.UUID | None = None
    if payment_method == "cash":
        open_session = await cashier_service.get_open_session(
            session, branch_id=branch_id, cashier_id=actor.id
        )
        cash_session_id = open_session.id if open_session is not None else None

    expense = Expense(
        branch_id=branch_id,
        expense_category_id=expense_category_id,
        amount=amount,
        currency_code=branch.currency_code,
        expense_date=expense_date,
        description=(description.strip() or None) if description else None,
        payment_method=payment_method,
        cash_session_id=cash_session_id,
        created_by=actor.id,
        updated_by=actor.id,
    )
    session.add(expense)
    await session.commit()
    await session.refresh(expense)
    return expense


async def get_expense(session: AsyncSession, expense_id: uuid.UUID) -> Expense:
    expense = (
        await session.execute(
            select(Expense).where(Expense.id == expense_id, Expense.is_deleted.is_(False))
        )
    ).scalar_one_or_none()
    if expense is None:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 404, message="Expense not found.")
    return expense


async def get_expense_out(session: AsyncSession, expense_id: uuid.UUID) -> dict[str, object]:
    row = (
        await session.execute(
            select(Expense, ExpenseCategory)
            .join(ExpenseCategory, ExpenseCategory.id == Expense.expense_category_id)
            .where(Expense.id == expense_id, Expense.is_deleted.is_(False))
        )
    ).first()
    if row is None:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 404, message="Expense not found.")
    expense, category = row
    return _expense_out(expense, category)


async def update_expense(
    session: AsyncSession, *, actor: User, expense: Expense, changes: dict[str, Any]
) -> Expense:
    """PATCH semantics — only the provided keys are applied."""
    if "expense_category_id" in changes:
        category_id = uuid.UUID(str(changes["expense_category_id"]))
        await _active_category_or_422(session, category_id)
        expense.expense_category_id = category_id
    if "amount" in changes:
        amount = Decimal(str(changes["amount"]))
        if amount <= 0:
            raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Amount must be positive.")
        expense.amount = amount
    if "expense_date" in changes:
        expense.expense_date = changes["expense_date"]
    if "description" in changes:
        val = changes["description"]
        expense.description = str(val).strip() or None if val else None
    if "payment_method" in changes:
        method = str(changes["payment_method"])
        if method not in PAYMENT_METHODS:
            raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Unknown payment method.")
        expense.payment_method = method
    expense.updated_by = actor.id
    await session.commit()
    await session.refresh(expense)
    return expense


async def delete_expense(session: AsyncSession, *, actor: User, expense: Expense) -> None:
    expense.is_deleted = True
    expense.updated_by = actor.id
    await session.commit()


async def list_expenses(
    session: AsyncSession,
    *,
    branch_id: uuid.UUID,
    category_id: uuid.UUID | None = None,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    skip: int = 0,
    limit: int = 50,
) -> tuple[list[dict[str, object]], int]:
    capped = min(max(limit, 1), MAX_PAGE_SIZE)
    conditions = [Expense.branch_id == branch_id, Expense.is_deleted.is_(False)]
    if category_id is not None:
        conditions.append(Expense.expense_category_id == category_id)
    if date_from is not None:
        conditions.append(Expense.expense_date >= date_from)
    if date_to is not None:
        conditions.append(Expense.expense_date <= date_to)
    total = (await session.execute(select(func.count(Expense.id)).where(*conditions))).scalar_one()
    rows = (
        await session.execute(
            select(Expense, ExpenseCategory)
            .join(ExpenseCategory, ExpenseCategory.id == Expense.expense_category_id)
            .where(*conditions)
            .order_by(Expense.expense_date.desc(), Expense.created_at.desc())
            .offset(max(skip, 0))
            .limit(capped)
        )
    ).all()
    return [_expense_out(e, c) for e, c in rows], int(total)
