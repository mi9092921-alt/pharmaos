"""Expenses + categories (P2-M9).

ExpenseCategory is a GLOBAL/catalog-style lookup (no branch_id) — see the
migration's module comment for the rationale (one shared chart of expense
categories across all branches, same shape as `categories` for medications).
Expense is operational (branch_id NOT NULL).
"""

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import Boolean, Date, ForeignKey, Numeric, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from pharmaos_api.models.base import Base, MandatoryColumnsMixin


class ExpenseCategory(MandatoryColumnsMixin, Base):
    __tablename__ = "expense_categories"

    name_ar: Mapped[str] = mapped_column(String(120), nullable=False)
    name_en: Mapped[str | None] = mapped_column(String(120), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("TRUE"))


class Expense(MandatoryColumnsMixin, Base):
    __tablename__ = "expenses"

    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id"), nullable=False
    )
    expense_category_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("expense_categories.id"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency_code: Mapped[str] = mapped_column(
        String(3), ForeignKey("currencies.code"), nullable=False
    )
    expense_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    payment_method: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'cash'")
    )
    # P2 review C5: a cash expense taken from the drawer links to the open
    # cashier session so it reduces that session's expected cash.
    cash_session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cash_sessions.id"), nullable=True
    )
