"""Payments money ledger (P2-M7).

Signed amounts: +amount for a sale receipt, -amount for a refund. A payment
belongs to an invoice (sale) or a return (refund). The writer is no-commit — the
caller (create_sale / create_return) owns the transaction so the payment persists
or rolls back atomically with its sale/return.
"""

import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.models import Payment, User


async def record(
    session: AsyncSession,
    *,
    actor: User,
    branch_id: uuid.UUID,
    amount: Decimal,
    method: str,
    invoice_id: uuid.UUID | None = None,
    return_id: uuid.UUID | None = None,
    cash_session_id: uuid.UUID | None = None,
    reference: str | None = None,
) -> Payment:
    """Append a payment row (NO commit)."""
    payment = Payment(
        branch_id=branch_id,
        invoice_id=invoice_id,
        return_id=return_id,
        amount=amount,
        method=method,
        cash_session_id=cash_session_id,
        reference=reference,
        created_by=actor.id,
    )
    session.add(payment)
    return payment


async def net_for_invoice(session: AsyncSession, invoice_id: uuid.UUID) -> Decimal:
    """Net money booked against an invoice (sale receipt minus its refunds).
    Sums the invoice's own payment plus refunds on returns of that invoice."""
    total = (
        await session.execute(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.invoice_id == invoice_id, Payment.is_deleted.is_(False)
            )
        )
    ).scalar_one()
    return Decimal(total)
