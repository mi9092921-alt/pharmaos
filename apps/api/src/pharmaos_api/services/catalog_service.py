"""Medications catalog (P1-M5).

Search (CLAUDE.md Arabic-search rules):
- The user's query passes through the SAME normalize_arabic as the index —
  we call the DB function so the rules can never drift.
- FTS (arabic_simple) first; if fewer than 3 hits, trigram fallback on the
  normalized Arabic trade name (typos/partials). Target < 100ms.

Prices: every price change on a packaging level writes a
medication_price_history row (old -> new, with provenance) and stamps
price_updated_at — Egyptian prices are government-set (CLAUDE.md).

Deletion is SOFT only; medication.deleted is an audited operation.
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.audit import AuditAction
from pharmaos_api.errors import ApiError, ErrorCode
from pharmaos_api.models import (
    Medication,
    MedicationBarcode,
    MedicationPackaging,
    MedicationPriceHistory,
    User,
)
from pharmaos_api.services import audit_service

MAX_PAGE_SIZE = 100
_TRIGRAM_MIN_RESULTS = 3

_MED_FIELDS = (
    "trade_name",
    "trade_name_ar",
    "scientific_name",
    "manufacturer",
    "drug_class",
    "route",
    "requires_prescription",
    "controlled_substance",
    "storage_conditions",
    "eda_registration_no",
    "gtin",
    "is_medicine",
    "is_active",
)


@dataclass(frozen=True)
class PackagingLevelIn:
    level: int
    unit_id: uuid.UUID
    name_ar: str
    qty_in_parent: Decimal | None
    selling_price: Decimal
    is_sellable: bool
    is_default_sale: bool


def _base_query() -> Select[tuple[Medication]]:
    return select(Medication).where(Medication.is_deleted.is_(False))


async def list_medications(
    session: AsyncSession, *, search: str | None, skip: int, limit: int
) -> tuple[list[Medication], int]:
    capped = min(max(limit, 1), MAX_PAGE_SIZE)
    if not search:
        total = (
            await session.execute(
                select(func.count(Medication.id)).where(Medication.is_deleted.is_(False))
            )
        ).scalar_one()
        rows = (
            (
                await session.execute(
                    _base_query().order_by(Medication.trade_name).offset(max(skip, 0)).limit(capped)
                )
            )
            .scalars()
            .all()
        )
        return list(rows), total

    # --- FTS first (query normalized IN the DB — single source of the rules) ---
    fts = (
        _base_query()
        .where(
            text(
                "search_vector @@ plainto_tsquery('arabic_simple', normalize_arabic(:q))"
            ).bindparams(q=search)
        )
        .order_by(Medication.trade_name)
        .limit(capped)
    )
    rows = list((await session.execute(fts)).scalars().all())

    if len(rows) < _TRIGRAM_MIN_RESULTS:
        trigram = (
            _base_query()
            .where(
                text(
                    "(normalize_arabic(trade_name_ar) % normalize_arabic(:q) "
                    "OR trade_name ILIKE '%' || :q || '%')"
                ).bindparams(q=search)
            )
            .order_by(Medication.trade_name)
            .limit(capped)
        )
        for med in (await session.execute(trigram)).scalars():
            if all(existing.id != med.id for existing in rows):
                rows.append(med)
        rows = rows[:capped]
    return rows, len(rows)


async def get_medication(session: AsyncSession, medication_id: uuid.UUID) -> Medication:
    med = (
        await session.execute(_base_query().where(Medication.id == medication_id))
    ).scalar_one_or_none()
    if med is None:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 404, message="Medication not found.")
    return med


async def get_packaging(
    session: AsyncSession, medication_id: uuid.UUID
) -> list[MedicationPackaging]:
    return list(
        (
            await session.execute(
                select(MedicationPackaging)
                .where(
                    MedicationPackaging.medication_id == medication_id,
                    MedicationPackaging.is_deleted.is_(False),
                )
                .order_by(MedicationPackaging.level)
            )
        )
        .scalars()
        .all()
    )


async def get_barcodes(session: AsyncSession, medication_id: uuid.UUID) -> list[MedicationBarcode]:
    return list(
        (
            await session.execute(
                select(MedicationBarcode)
                .where(
                    MedicationBarcode.medication_id == medication_id,
                    MedicationBarcode.is_deleted.is_(False),
                )
                .order_by(MedicationBarcode.created_at)
            )
        )
        .scalars()
        .all()
    )


def _apply_fields(med: Medication, values: dict[str, Any]) -> list[str]:
    changed = []
    for field in _MED_FIELDS:
        if field in values and values[field] is not None and getattr(med, field) != values[field]:
            setattr(med, field, values[field])
            changed.append(field)
    return changed


async def create_medication(
    session: AsyncSession, *, actor: User, values: dict[str, Any]
) -> Medication:
    if not values.get("trade_name"):
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="trade_name is required.")
    med = Medication(trade_name=values["trade_name"], created_by=actor.id, updated_by=actor.id)
    _apply_fields(med, values)
    session.add(med)
    await session.commit()
    await session.refresh(med)
    return med


async def update_medication(
    session: AsyncSession, *, actor: User, med: Medication, values: dict[str, Any]
) -> Medication:
    if _apply_fields(med, values):
        med.updated_by = actor.id
        await session.commit()
        await session.refresh(med)
    return med


async def soft_delete_medication(session: AsyncSession, *, actor: User, med: Medication) -> None:
    med.is_deleted = True
    med.updated_by = actor.id
    await audit_service.record(
        session,
        AuditAction.MEDICATION_DELETED,
        actor=actor,
        entity_type="medication",
        entity_id=med.id,
        metadata={"trade_name": med.trade_name},
    )
    await session.commit()


def _validate_levels(levels: list[PackagingLevelIn]) -> None:
    if not levels:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="At least one level.")
    nums = [level.level for level in levels]
    if len(set(nums)) != len(nums):
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Duplicate level.")
    defaults = [level for level in levels if level.is_default_sale]
    if len(defaults) > 1:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="One default sale level only.")
    deepest = max(nums)
    for level in levels:
        if level.selling_price < 0:
            raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Negative price.")
        # Conversion math needs qty_in_parent on every level below the top one.
        if level.level != min(nums) and level.qty_in_parent is None:
            raise ApiError(
                ErrorCode.VALIDATION_FAILED,
                422,
                message=f"qty_in_parent required for level {level.level}.",
            )
        if level.level == deepest and not level.is_sellable and len(levels) == 1:
            raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="No sellable level.")


async def upsert_packaging(
    session: AsyncSession,
    *,
    actor: User,
    med: Medication,
    levels: list[PackagingLevelIn],
    price_source: str = "manual",
) -> list[MedicationPackaging]:
    """Create/update the medication's levels; price changes write history rows."""
    _validate_levels(levels)
    existing = {p.level: p for p in await get_packaging(session, med.id)}

    for level_in in levels:
        row = existing.get(level_in.level)
        if row is None:
            row = MedicationPackaging(
                medication_id=med.id,
                level=level_in.level,
                unit_id=level_in.unit_id,
                name_ar=level_in.name_ar,
                qty_in_parent=level_in.qty_in_parent,
                selling_price=level_in.selling_price,
                is_sellable=level_in.is_sellable,
                is_default_sale=level_in.is_default_sale,
                price_source=price_source,
                created_by=actor.id,
            )
            session.add(row)
            await session.flush()
            session.add(
                MedicationPriceHistory(
                    medication_id=med.id,
                    packaging_id=row.id,
                    old_price=None,
                    new_price=level_in.selling_price,
                    price_source=price_source,
                    created_by=actor.id,
                )
            )
        else:
            if row.selling_price != level_in.selling_price:
                session.add(
                    MedicationPriceHistory(
                        medication_id=med.id,
                        packaging_id=row.id,
                        old_price=row.selling_price,
                        new_price=level_in.selling_price,
                        price_source=price_source,
                        created_by=actor.id,
                    )
                )
                row.selling_price = level_in.selling_price
                row.price_source = price_source
                row.price_updated_at = func.now()
            row.unit_id = level_in.unit_id
            row.name_ar = level_in.name_ar
            row.qty_in_parent = level_in.qty_in_parent
            row.is_sellable = level_in.is_sellable
            row.is_default_sale = level_in.is_default_sale
            row.updated_by = actor.id

    await session.commit()
    return await get_packaging(session, med.id)


async def add_barcode(
    session: AsyncSession,
    *,
    actor: User,
    med: Medication,
    barcode: str,
    barcode_type: str,
    packaging_id: uuid.UUID | None,
    is_primary: bool,
) -> MedicationBarcode:
    taken = (
        await session.execute(
            select(MedicationBarcode.id).where(MedicationBarcode.barcode == barcode)
        )
    ).scalar_one_or_none()
    if taken is not None:
        raise ApiError(ErrorCode.BARCODE_TAKEN, 409)
    row = MedicationBarcode(
        medication_id=med.id,
        barcode=barcode,
        barcode_type=barcode_type,
        packaging_id=packaging_id,
        is_primary=is_primary,
        created_by=actor.id,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def remove_barcode(
    session: AsyncSession, *, actor: User, med: Medication, barcode_id: uuid.UUID
) -> None:
    row = (
        await session.execute(
            select(MedicationBarcode).where(
                MedicationBarcode.id == barcode_id,
                MedicationBarcode.medication_id == med.id,
                MedicationBarcode.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 404, message="Barcode not found.")
    row.is_deleted = True
    row.updated_by = actor.id
    await session.commit()


async def find_by_gtin(session: AsyncSession, gtin: str) -> Medication | None:
    """Resolve a medication by GTIN (2D code) — medications.gtin OR a stored barcode."""
    med = (await session.execute(_base_query().where(Medication.gtin == gtin))).scalar_one_or_none()
    if med is not None:
        return med
    via_barcode = (
        await session.execute(
            select(MedicationBarcode.medication_id).where(
                MedicationBarcode.barcode == gtin, MedicationBarcode.is_deleted.is_(False)
            )
        )
    ).scalar_one_or_none()
    if via_barcode is None:
        return None
    return await get_medication(session, via_barcode)
