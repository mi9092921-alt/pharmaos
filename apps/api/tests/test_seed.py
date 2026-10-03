"""Catalog seeding & import (P1-M6): mapping, idempotency, error reporting,
price provenance, and Arabic search on the seeded data."""

import uuid
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.services.seed_service import seed_catalog

HEADER = (
    "commercial_name_en,commercial_name_ar,scientific_name,manufacturer,drug_class,route,price_egp"
)


def _write_csv(tmp_path: Path, rows: list[str]) -> Path:
    path = tmp_path / "seed.csv"
    path.write_text("\n".join([HEADER, *rows]), encoding="utf-8")
    return path


async def test_seed_maps_rows_and_reports(tmp_path: Path, db_session: AsyncSession) -> None:
    marker = uuid.uuid4().hex[:6].upper()
    path = _write_csv(
        tmp_path,
        [
            f"SEEDMED-A {marker},سيدميد أ,PARACETAMOL,HIKMA,ANALGESIC,ORAL.SOLID,45.5",
            f"SEEDMED-B {marker},سيدميد ب,IBUPROFEN,EIPICO,NSAID,ORAL.SOLID,30",
            f"SEEDMED-A {marker},مكرر داخل الملف,X,Y,Z,ORAL,9",  # dup in file
            ",بلا اسم,X,Y,Z,ORAL,5",  # empty name -> error
            f"SEEDMED-C {marker},سيدميد ج,ASPIRIN,BAYER,NSAID,ORAL.SOLID,notanumber",  # bad price
        ],
    )
    report = await seed_catalog(db_session, file_path=path, price_source="seed")
    assert report.created == 2
    assert report.skipped_duplicate_in_file == 1
    assert len(report.errors) == 2

    row = (
        await db_session.execute(
            text(
                "SELECT m.trade_name_ar, p.selling_price, p.price_source, p.is_default_sale, "
                "u.name_ar FROM medications m "
                "JOIN medication_packaging p ON p.medication_id = m.id "
                "JOIN units u ON u.id = p.unit_id "
                "WHERE m.trade_name = :n"
            ).bindparams(n=f"SEEDMED-A {marker}")
        )
    ).one()
    assert row[0] == "سيدميد أ"
    assert str(row[1]) == "45.50" and row[2] == "seed"
    assert row[3] is True and row[4] == "علبة"

    # price history written with NULL old_price (first price)
    hist = (
        await db_session.execute(
            text(
                "SELECT COUNT(*) FROM medication_price_history h "
                "JOIN medications m ON m.id = h.medication_id "
                "WHERE m.trade_name LIKE :p AND h.old_price IS NULL AND h.price_source='seed'"
            ).bindparams(p=f"SEEDMED-%{marker}")
        )
    ).scalar_one()
    assert hist == 2

    # re-run: fully idempotent — the in-file duplicate now also matches
    # "existing" (A, B, and the second A row) -> 3 skipped, 0 created.
    report2 = await seed_catalog(db_session, file_path=path, price_source="seed")
    assert report2.created == 0
    assert report2.skipped_existing == 3


async def test_xlsx_import_same_pipeline(tmp_path: Path, db_session: AsyncSession) -> None:
    import openpyxl

    marker = uuid.uuid4().hex[:6].upper()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(HEADER.split(","))
    ws.append([f"XLMED {marker}", "إكسل دواء", "AMOXICILLIN", "GSK", "ANTIBIOTIC", "ORAL", 77.25])
    path = tmp_path / "import.xlsx"
    wb.save(path)

    report = await seed_catalog(db_session, file_path=path, price_source="import")
    assert report.created == 1 and not report.errors

    src = (
        await db_session.execute(
            text(
                "SELECT p.price_source FROM medication_packaging p "
                "JOIN medications m ON m.id = p.medication_id WHERE m.trade_name = :n"
            ).bindparams(n=f"XLMED {marker}")
        )
    ).scalar_one()
    assert src == "import"


async def test_seeded_data_searchable_arabic(tmp_path: Path, db_session: AsyncSession) -> None:
    marker = uuid.uuid4().hex[:6].upper()
    path = _write_csv(tmp_path, [f"SRCH {marker},أوجمنتين اختبار,AMOX,GSK,AB,ORAL,10"])
    await seed_catalog(db_session, file_path=path)
    # FTS with a different hamza form must hit through normalize_arabic
    found = (
        await db_session.execute(
            text(
                "SELECT COUNT(*) FROM medications WHERE NOT is_deleted AND "
                "search_vector @@ plainto_tsquery('arabic_simple', normalize_arabic('اوجمنتين'))"
            )
        )
    ).scalar_one()
    assert found >= 1
