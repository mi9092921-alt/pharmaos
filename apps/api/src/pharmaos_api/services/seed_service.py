"""Catalog seeding & import (P1-M6).

One pipeline, two entry points (CLAUDE.md: initial seed from the CC0 Egyptian
drug dataset + Excel import):

- CSV  (the CC0 file: commercial_name_en, commercial_name_ar, scientific_name,
        manufacturer, drug_class, route, price_egp)
- XLSX (same column template — documented for pharmacy staff)

Every row becomes: a medication + ONE level-1 (علبة/box) sellable packaging at
price_egp + a price-history row. The dataset carries NO barcodes (documented in
CLAUDE.md) — barcodes accumulate via scanning at receiving or a commercial
provider later. Deeper levels (strip/tablet) are configured per-medication in
the catalog editor.

Idempotent: rows whose trade_name already exists in the catalog are skipped
(safe to re-run). Rows with a missing name or an unparsable price are reported
as errors with their row numbers, never silently dropped.

With update_prices=True the SAME file format also UPDATES the level-1 price of
existing medications (the monthly price-list refresh): each matched row sets
the packaging's selling_price and writes a price-history row carrying
old_price -> new_price with the file's price_source. Unmatched names are
still created; rows whose name exists WITHOUT a level-1 packaging are errors
(nothing to update on).

Performance: batched Core inserts (25k rows in seconds, not ORM-per-row).
"""

import csv
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.models import Medication, MedicationPackaging, MedicationPriceHistory

_COLUMNS = (
    "commercial_name_en",
    "commercial_name_ar",
    "scientific_name",
    "manufacturer",
    "drug_class",
    "route",
    "price_egp",
)
_BATCH = 1000
_BOX_UNIT_NAME = "علبة"


@dataclass
class SeedReport:
    created: int = 0
    updated: int = 0
    skipped_existing: int = 0
    skipped_duplicate_in_file: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "created": self.created,
            "updated": self.updated,
            "skipped_existing": self.skipped_existing,
            "skipped_duplicate_in_file": self.skipped_duplicate_in_file,
            "errors": self.errors[:50],
            "error_count": len(self.errors),
        }


def _rows_from_csv(path: Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in _COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"missing columns: {missing}")
        return list(reader)


def _rows_from_xlsx(path: Path) -> list[dict[str, str]]:
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows_iter = ws.iter_rows(values_only=True)
    header = [str(h).strip() if h is not None else "" for h in next(rows_iter)]
    missing = [c for c in _COLUMNS if c not in header]
    if missing:
        raise ValueError(f"missing columns: {missing}")
    idx = {name: header.index(name) for name in _COLUMNS}
    out: list[dict[str, str]] = []
    for raw in rows_iter:
        out.append(
            {
                name: ("" if raw[i] is None else str(raw[i]).strip())
                for name, i in idx.items()
                if i < len(raw)
            }
        )
    wb.close()
    return out


async def _box_unit_id(session: AsyncSession) -> str:
    uid = (
        await session.execute(
            text(
                "INSERT INTO units (name_ar) VALUES (:n) "
                "ON CONFLICT (name_ar) DO UPDATE SET name_ar=EXCLUDED.name_ar RETURNING id"
            ).bindparams(n=_BOX_UNIT_NAME)
        )
    ).scalar_one()
    await session.commit()  # release the upsert row lock immediately
    return str(uid)


async def seed_catalog(
    session: AsyncSession,
    *,
    file_path: Path,
    price_source: str = "seed",
    update_prices: bool = False,
) -> SeedReport:
    """Load a CSV/XLSX catalog file. Returns a full report; commits in batches."""
    report = SeedReport()
    rows = (
        _rows_from_xlsx(file_path)
        if file_path.suffix.lower() in {".xlsx", ".xlsm"}
        else _rows_from_csv(file_path)
    )

    existing: set[str] = set((await session.execute(select(Medication.trade_name))).scalars().all())
    # Level-1 packaging per existing name — the price-update target (release-
    # prep phase 5: the monthly price list refreshes via the same file format).
    existing_l1: dict[str, tuple[Any, Any, Decimal]] = {
        r[0]: (r[1], r[2], r[3])
        for r in (
            await session.execute(
                select(
                    Medication.trade_name,
                    Medication.id,
                    MedicationPackaging.id,
                    MedicationPackaging.selling_price,
                )
                .join(MedicationPackaging, MedicationPackaging.medication_id == Medication.id)
                .where(MedicationPackaging.level == 1, MedicationPackaging.is_deleted.is_(False))
            )
        ).all()
    }
    unit_id = await _box_unit_id(session)
    seen: set[str] = set()
    pending: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []

    async def flush_updates() -> None:
        if not updates:
            return
        await session.execute(
            text("UPDATE medication_packaging SET selling_price = :price WHERE id = :pkg_id"),
            [{"pkg_id": u["pkg_id"], "price": u["price"]} for u in updates],
        )
        await session.execute(
            insert(MedicationPriceHistory),
            [
                {
                    "medication_id": u["med_id"],
                    "packaging_id": u["pkg_id"],
                    "old_price": u["old_price"],
                    "new_price": u["price"],
                    "price_source": price_source,
                }
                for u in updates
            ],
        )
        await session.commit()
        report.updated += len(updates)
        updates.clear()

    async def flush() -> None:
        if not pending:
            return
        med_rows = (
            await session.execute(
                insert(Medication).returning(Medication.id, Medication.trade_name),
                [p["med"] for p in pending],
            )
        ).all()
        by_name = {name: mid for mid, name in med_rows}
        pkg_rows = (
            await session.execute(
                insert(MedicationPackaging).returning(
                    MedicationPackaging.id, MedicationPackaging.medication_id
                ),
                [
                    {
                        "medication_id": by_name[p["med"]["trade_name"]],
                        "level": 1,
                        "unit_id": unit_id,
                        "name_ar": _BOX_UNIT_NAME,
                        "qty_in_parent": None,
                        "is_sellable": True,
                        "selling_price": p["price"],
                        "is_default_sale": True,
                        "price_source": price_source,
                    }
                    for p in pending
                ],
            )
        ).all()
        price_by_med = {by_name[p["med"]["trade_name"]]: p["price"] for p in pending}
        await session.execute(
            insert(MedicationPriceHistory),
            [
                {
                    "medication_id": mid,
                    "packaging_id": pkg_id,
                    "old_price": None,
                    "new_price": price_by_med[mid],
                    "price_source": price_source,
                }
                for pkg_id, mid in pkg_rows
            ],
        )
        await session.commit()
        report.created += len(pending)
        pending.clear()

    for lineno, row in enumerate(rows, start=2):  # header is line 1
        name = (row.get("commercial_name_en") or "").strip()
        if not name:
            report.errors.append(f"row {lineno}: empty commercial_name_en")
            continue
        if name in seen:
            report.skipped_duplicate_in_file += 1
            continue
        try:
            price = Decimal((row.get("price_egp") or "").strip() or "0").quantize(Decimal("0.01"))
            if price < 0:
                raise InvalidOperation
        except InvalidOperation:
            report.errors.append(f"row {lineno}: bad price {row.get('price_egp')!r}")
            continue
        if name in existing:
            if not update_prices:
                report.skipped_existing += 1
                continue
            target = existing_l1.get(name)
            if target is None:
                report.errors.append(f"row {lineno}: {name} has no level-1 packaging to update")
                continue
            seen.add(name)
            updates.append(
                {
                    "med_id": target[0],
                    "pkg_id": target[1],
                    "old_price": target[2],
                    "price": price,
                }
            )
            if len(updates) >= _BATCH:
                await flush_updates()
            continue
        seen.add(name)
        pending.append(
            {
                "med": {
                    "trade_name": name[:255],
                    "trade_name_ar": (row.get("commercial_name_ar") or "").strip()[:255] or None,
                    "scientific_name": (row.get("scientific_name") or "").strip()[:255] or None,
                    "manufacturer": (row.get("manufacturer") or "").strip()[:255] or None,
                    "drug_class": (row.get("drug_class") or "").strip()[:100] or None,
                    "route": (row.get("route") or "").strip()[:50] or None,
                },
                "price": price,
            }
        )
        if len(pending) >= _BATCH:
            await flush()
    await flush()
    await flush_updates()
    return report
