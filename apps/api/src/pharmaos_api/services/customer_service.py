"""Customer service (P2-M5) — service-layer boundary for encrypted customer PII
(national_id, insurance_number) and the minimal loyalty ledger.

CLAUDE.md rules:
- Encryption happens HERE before write; decryption only on authorized reads.
- name + phone are PLAINTEXT (the pharmacy's primary search/lookup keys).
- Loyalty is ledger-first: loyalty_transactions is the append-only truth;
  customers.loyalty_points is the derived balance, moved in the SAME transaction
  as each ledger row (same discipline as batches vs branch_inventory).
"""

import logging
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.errors import ApiError, ErrorCode
from pharmaos_api.models import Customer, Invoice, LoyaltyTransaction, User
from pharmaos_api.security.crypto import DecryptionError, decrypt_field, encrypt_field

logger = logging.getLogger(__name__)

MAX_PAGE_SIZE = 100
NATIONAL_ID_CONTEXT = "customers.national_id"
INSURANCE_CONTEXT = "customers.insurance_number"
# Minimal loyalty scheme: 1 point per whole currency unit of the invoice total.
LOYALTY_POINTS_PER_UNIT = 1


def points_for_amount(total: Decimal) -> int:
    """Points earned for a sale total — floor(total) x rate (minimal scheme)."""
    return int(total) * LOYALTY_POINTS_PER_UNIT


# --------------------------------- PII crypto ---------------------------------


def _encrypt(value: str | None, *, context: str) -> bytes | None:
    clean = value.strip() if value else ""
    return encrypt_field(clean, context=context) if clean else None


def _safe_decrypt(payload: bytes | None, *, context: str) -> str | None:
    """Decrypt for an authorized read; never raises (a key-rotation-orphaned row
    returns None and is logged, so one bad row can't 500 a detail response)."""
    if payload is None:
        return None
    try:
        return decrypt_field(bytes(payload), context=context)
    except DecryptionError:
        logger.warning("customer PII decrypt failed for context=%s", context)
        return None


# ------------------------------- serialization -------------------------------


def summary(c: Customer) -> dict[str, object]:
    """List view — NO PII (only presence flags)."""
    return {
        "id": str(c.id),
        "name": c.name,
        "phone": c.phone,
        "loyalty_points": int(c.loyalty_points),
        "is_active": c.is_active,
        "has_national_id": c.national_id_encrypted is not None,
        "has_insurance_number": c.insurance_number_encrypted is not None,
    }


def detail(c: Customer) -> dict[str, object]:
    """Single-customer authorized read — decrypts PII."""
    return {
        **summary(c),
        "national_id": _safe_decrypt(c.national_id_encrypted, context=NATIONAL_ID_CONTEXT),
        "insurance_number": _safe_decrypt(c.insurance_number_encrypted, context=INSURANCE_CONTEXT),
        "notes": c.notes,
        "created_at": c.created_at.isoformat(),
    }


# ------------------------------- CRUD --------------------------------


async def get_customer(session: AsyncSession, customer_id: uuid.UUID) -> Customer:
    c = (
        await session.execute(
            select(Customer).where(Customer.id == customer_id, Customer.is_deleted.is_(False))
        )
    ).scalar_one_or_none()
    if c is None:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 404, message="Customer not found.")
    return c


async def create_customer(
    session: AsyncSession,
    *,
    actor: User,
    name: str,
    phone: str | None = None,
    national_id: str | None = None,
    insurance_number: str | None = None,
    notes: str | None = None,
) -> Customer:
    clean_name = name.strip()
    if not clean_name:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Customer name is required.")
    customer = Customer(
        name=clean_name,
        phone=(phone.strip() or None) if phone else None,
        national_id_encrypted=_encrypt(national_id, context=NATIONAL_ID_CONTEXT),
        insurance_number_encrypted=_encrypt(insurance_number, context=INSURANCE_CONTEXT),
        notes=(notes.strip() or None) if notes else None,
        created_by=actor.id,
    )
    session.add(customer)
    await session.commit()
    await session.refresh(customer)
    return customer


async def update_customer(
    session: AsyncSession, *, actor: User, customer: Customer, updates: dict[str, Any]
) -> Customer:
    """Apply only the fields present in `updates` (PATCH semantics). PII fields
    are re-encrypted; an empty/None value clears the field."""
    if "name" in updates:
        clean = str(updates["name"] or "").strip()
        if not clean:
            raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Customer name is required.")
        customer.name = clean
    if "phone" in updates:
        phone = updates["phone"]
        customer.phone = str(phone).strip() or None if phone else None
    if "national_id" in updates:
        customer.national_id_encrypted = _encrypt(
            updates["national_id"], context=NATIONAL_ID_CONTEXT
        )
    if "insurance_number" in updates:
        customer.insurance_number_encrypted = _encrypt(
            updates["insurance_number"], context=INSURANCE_CONTEXT
        )
    if "notes" in updates:
        notes = updates["notes"]
        customer.notes = str(notes).strip() or None if notes else None
    if "is_active" in updates:
        customer.is_active = bool(updates["is_active"])
    customer.updated_by = actor.id
    await session.commit()
    await session.refresh(customer)
    return customer


async def delete_customer(session: AsyncSession, *, actor: User, customer: Customer) -> None:
    """Soft-delete (super_admin only). History rows keep their FK for audit."""
    customer.is_deleted = True
    customer.is_active = False
    customer.updated_by = actor.id
    await session.commit()


async def list_customers(
    session: AsyncSession,
    *,
    search: str | None = None,
    active_only: bool = False,
    skip: int = 0,
    limit: int = 50,
) -> tuple[list[dict[str, object]], int]:
    """Customers with Arabic-name (trigram/ILIKE) + phone search; no PII."""
    capped = min(max(limit, 1), MAX_PAGE_SIZE)
    conditions: list[Any] = [Customer.is_deleted.is_(False)]
    if active_only:
        conditions.append(Customer.is_active.is_(True))
    if search and search.strip():
        conditions.append(
            text(
                "(normalize_arabic(customers.name) % normalize_arabic(:q) "
                "OR customers.name ILIKE '%' || :q || '%' "
                "OR customers.phone ILIKE '%' || :q || '%')"
            ).bindparams(q=search.strip())
        )
    total = (await session.execute(select(func.count(Customer.id)).where(*conditions))).scalar_one()
    rows = (
        (
            await session.execute(
                select(Customer)
                .where(*conditions)
                .order_by(Customer.name)
                .offset(max(skip, 0))
                .limit(capped)
            )
        )
        .scalars()
        .all()
    )
    return [summary(c) for c in rows], int(total)


# ------------------------------- loyalty --------------------------------


async def _apply_loyalty(
    session: AsyncSession,
    *,
    actor: User,
    customer: Customer,
    points_delta: int,
    txn_type: str,
    reason: str | None = None,
    reference_type: str = "manual",
    reference_id: uuid.UUID | None = None,
) -> None:
    """Append a ledger row and move the derived balance — NO commit (the caller
    commits, so accrual is atomic with the sale)."""
    new_balance = int(customer.loyalty_points) + points_delta
    if new_balance < 0:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED, 422, message="Loyalty balance cannot go negative."
        )
    session.add(
        LoyaltyTransaction(
            customer_id=customer.id,
            points_delta=points_delta,
            txn_type=txn_type,
            reference_type=reference_type,
            reference_id=reference_id,
            reason=reason,
            created_by=actor.id,
        )
    )
    customer.loyalty_points = new_balance
    customer.updated_by = actor.id


async def accrue_for_sale(
    session: AsyncSession,
    *,
    cashier: User,
    customer_id: uuid.UUID,
    invoice_id: uuid.UUID,
    total: Decimal,
) -> int:
    """Earn loyalty points for a completed sale — NO commit (runs inside the sale
    transaction, so points and the invoice persist or roll back together).
    Returns the points earned; rejects an unknown/inactive customer."""
    customer = (
        await session.execute(
            select(Customer)
            .where(Customer.id == customer_id, Customer.is_deleted.is_(False))
            .with_for_update()
        )
    ).scalar_one_or_none()
    if customer is None:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Unknown customer.")
    if not customer.is_active:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Customer is inactive.")
    points = points_for_amount(total)
    if points > 0:
        await _apply_loyalty(
            session,
            actor=cashier,
            customer=customer,
            points_delta=points,
            txn_type="earn",
            reference_type="invoice",
            reference_id=invoice_id,
        )
    return points


async def redeem_for_sale(
    session: AsyncSession,
    *,
    cashier: User,
    customer_id: uuid.UUID,
    points: int,
    invoice_id: uuid.UUID,
) -> None:
    """Redeem loyalty points as a sale discount — NO commit (runs inside the sale
    transaction, so the redemption and the invoice persist or roll back together).
    Rejects an unknown/inactive customer or an insufficient balance (1 pt = 1
    currency unit; the caller has already turned `points` into the discount)."""
    if points <= 0:
        return
    customer = (
        await session.execute(
            select(Customer)
            .where(Customer.id == customer_id, Customer.is_deleted.is_(False))
            .with_for_update()
        )
    ).scalar_one_or_none()
    if customer is None:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Unknown customer.")
    if not customer.is_active:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Customer is inactive.")
    if points > int(customer.loyalty_points):
        raise ApiError(
            ErrorCode.VALIDATION_FAILED, 422, message="Not enough loyalty points to redeem."
        )
    await _apply_loyalty(
        session,
        actor=cashier,
        customer=customer,
        points_delta=-points,
        txn_type="redeem",
        reference_type="invoice",
        reference_id=invoice_id,
    )


async def reverse_for_return(
    session: AsyncSession,
    *,
    actor: User,
    customer_id: uuid.UUID,
    refunded_total: Decimal,
    return_id: uuid.UUID,
) -> int:
    """Reverse loyalty points earned on a sale when part/all of it is returned —
    NO commit (runs inside the return transaction). Mirrors the accrual rate
    (points_for_amount) and is CLAMPED so it never drives the balance below zero
    (the customer may already have spent points). A missing/inactive customer is
    a no-op. Returns the points reversed (>= 0)."""
    customer = (
        await session.execute(
            select(Customer)
            .where(Customer.id == customer_id, Customer.is_deleted.is_(False))
            .with_for_update()
        )
    ).scalar_one_or_none()
    if customer is None:
        return 0
    reverse = min(points_for_amount(refunded_total), int(customer.loyalty_points))
    if reverse <= 0:
        return 0
    await _apply_loyalty(
        session,
        actor=actor,
        customer=customer,
        points_delta=-reverse,
        txn_type="adjust",
        reason="return reversal",
        reference_type="return",
        reference_id=return_id,
    )
    return reverse


async def adjust_points(
    session: AsyncSession, *, actor: User, customer: Customer, points_delta: int, reason: str
) -> Customer:
    """Manual loyalty correction (customers.edit). Cannot drive the balance below
    zero. Positive or negative; recorded as an 'adjust' ledger row."""
    if points_delta == 0:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Zero adjustment.")
    if not reason.strip():
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Reason is required.")
    await _apply_loyalty(
        session,
        actor=actor,
        customer=customer,
        points_delta=points_delta,
        txn_type="adjust",
        reason=reason.strip(),
        reference_type="manual",
    )
    await session.commit()
    await session.refresh(customer)
    return customer


async def recompute_points(session: AsyncSession, customer_id: uuid.UUID) -> int:
    """Re-derive the balance from the ledger (integrity check)."""
    total = (
        await session.execute(
            text(
                "SELECT COALESCE(SUM(points_delta), 0) FROM loyalty_transactions "
                "WHERE customer_id = :c AND NOT is_deleted"
            ).bindparams(c=customer_id)
        )
    ).scalar_one()
    return int(total)


async def list_loyalty(
    session: AsyncSession, *, customer_id: uuid.UUID, skip: int = 0, limit: int = 50
) -> tuple[list[dict[str, object]], int]:
    capped = min(max(limit, 1), MAX_PAGE_SIZE)
    conditions = [
        LoyaltyTransaction.customer_id == customer_id,
        LoyaltyTransaction.is_deleted.is_(False),
    ]
    total = (
        await session.execute(select(func.count(LoyaltyTransaction.id)).where(*conditions))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(LoyaltyTransaction)
                .where(*conditions)
                .order_by(LoyaltyTransaction.created_at.desc())
                .offset(max(skip, 0))
                .limit(capped)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(t.id),
            "points_delta": int(t.points_delta),
            "txn_type": t.txn_type,
            "reference_type": t.reference_type,
            "reference_id": str(t.reference_id) if t.reference_id else None,
            "reason": t.reason,
            "created_at": t.created_at.isoformat(),
        }
        for t in rows
    ], int(total)


async def customer_history(
    session: AsyncSession, *, customer_id: uuid.UUID, limit: int = 20
) -> list[dict[str, object]]:
    """Recent invoices for a customer (purchase-history foundation)."""
    capped = min(max(limit, 1), MAX_PAGE_SIZE)
    rows = (
        (
            await session.execute(
                select(Invoice)
                .where(Invoice.customer_id == customer_id, Invoice.is_deleted.is_(False))
                .order_by(Invoice.created_at.desc())
                .limit(capped)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "invoice_id": str(i.id),
            "invoice_number": i.invoice_number,
            "created_at": i.created_at.isoformat(),
            "total": str(i.total),
            "currency_code": i.currency_code,
            "status": i.status,
        }
        for i in rows
    ]
