"""Supplier management (P2-M1): full CRUD over the suppliers master.

Phase 1 shipped a name-only supplier (migration 0900, still used by the
inventory receiving picker). P2-M1 extends the table (migration 1100) and adds
full management: contacts, tax registration, payment terms, active flag, notes.

Notes:
- Suppliers are GLOBAL (no branch scope).
- CLAUDE.md's AUDITED_OPERATIONS defines no supplier.* action, so supplier
  changes are intentionally NOT audited (suppliers are not a critical /
  append-only entity). Deactivate via is_active rather than deleting; there is
  no suppliers.delete permission in the matrix.
"""

import uuid

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.errors import ApiError, ErrorCode
from pharmaos_api.models import Supplier, User

MAX_PAGE_SIZE = 100

# Fields a client may set on create/update (name handled explicitly).
_EDITABLE = frozenset(
    {
        "name",
        "contact_name",
        "phone",
        "email",
        "address",
        "tax_registration_no",
        "payment_terms",
        "is_active",
        "notes",
    }
)


def to_dict(s: Supplier) -> dict[str, object]:
    return {
        "id": str(s.id),
        "name": s.name,
        "contact_name": s.contact_name,
        "phone": s.phone,
        "email": s.email,
        "address": s.address,
        "tax_registration_no": s.tax_registration_no,
        "payment_terms": s.payment_terms,
        "is_active": s.is_active,
        "notes": s.notes,
    }


async def list_suppliers(
    session: AsyncSession,
    *,
    search: str | None = None,
    active_only: bool = False,
    skip: int = 0,
    limit: int = 50,
) -> tuple[list[dict[str, object]], int]:
    """Suppliers, active first then by name. Search matches name / contact /
    phone / tax registration (parameterized ILIKE)."""
    capped = min(max(limit, 1), MAX_PAGE_SIZE)
    conditions: list[ColumnElement[bool]] = [Supplier.is_deleted.is_(False)]
    if active_only:
        conditions.append(Supplier.is_active.is_(True))
    if search and search.strip():
        like = f"%{search.strip()}%"
        conditions.append(
            or_(
                Supplier.name.ilike(like),
                Supplier.contact_name.ilike(like),
                Supplier.phone.ilike(like),
                Supplier.tax_registration_no.ilike(like),
            )
        )
    total = (await session.execute(select(func.count(Supplier.id)).where(*conditions))).scalar_one()
    rows = (
        (
            await session.execute(
                select(Supplier)
                .where(*conditions)
                .order_by(Supplier.is_active.desc(), Supplier.name)
                .offset(max(skip, 0))
                .limit(capped)
            )
        )
        .scalars()
        .all()
    )
    return [to_dict(s) for s in rows], int(total)


async def get_supplier(session: AsyncSession, supplier_id: uuid.UUID) -> Supplier:
    supplier = (
        await session.execute(
            select(Supplier).where(Supplier.id == supplier_id, Supplier.is_deleted.is_(False))
        )
    ).scalar_one_or_none()
    if supplier is None:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 404, message="Supplier not found.")
    return supplier


async def create_supplier(
    session: AsyncSession, *, actor: User, name: str, **fields: object
) -> Supplier:
    clean = name.strip()
    if not clean:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Supplier name is required.")
    supplier = Supplier(name=clean, created_by=actor.id, updated_by=actor.id)
    for key, value in fields.items():
        if key in _EDITABLE and key != "name":
            setattr(supplier, key, value)
    session.add(supplier)
    await session.commit()
    await session.refresh(supplier)
    return supplier


async def update_supplier(
    session: AsyncSession, *, actor: User, supplier: Supplier, changes: dict[str, object]
) -> Supplier:
    """Apply only the provided fields (PATCH semantics). Blank name is rejected."""
    if "name" in changes:
        name = str(changes["name"] or "").strip()
        if not name:
            raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Supplier name is required.")
        supplier.name = name
    for key, value in changes.items():
        if key in _EDITABLE and key != "name":
            setattr(supplier, key, value)
    supplier.updated_by = actor.id
    await session.commit()
    await session.refresh(supplier)
    return supplier
