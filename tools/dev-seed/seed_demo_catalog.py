"""Seed a clearly-fictional demo catalog into a PharmaOS DEVELOPMENT database.

Adds N demo medications (default 2000) to the GLOBAL catalog tables —
medications + medication_packaging (box/strip/tablet) + medication_barcodes —
plus the 8 demo categories they need. No stock is touched: batches and
branch_inventory are operational tables (CLAUDE.md) and are out of scope.

Everything demo is marked so it can be removed again without touching real rows:
  * trade_name starts with 'DEMO-' (Arabic names start with 'ديمو')
  * barcodes use the GS1 Egypt prefix 622 + a zero-padded sequence
  * --remove soft-deletes demo rows only (never hard DELETE — rule #1)

Idempotent / top-up aware: each run continues the sequence after the highest
existing DEMO- number, and every row's random attributes are derived from a
per-row seed, so repeated runs are reproducible and never collide.

Usage (from the repo root, with the API venv):
  apps/api/.venv/Scripts/python.exe tools/dev-seed/seed_demo_catalog.py
  apps/api/.venv/Scripts/python.exe tools/dev-seed/seed_demo_catalog.py --count 500
  apps/api/.venv/Scripts/python.exe tools/dev-seed/seed_demo_catalog.py --stats
  apps/api/.venv/Scripts/python.exe tools/dev-seed/seed_demo_catalog.py --measure
  apps/api/.venv/Scripts/python.exe tools/dev-seed/seed_demo_catalog.py --remove

Target DB: --url argument > PHARMAOS_SEED_URL env > DATABASE_URL in the repo
.env (the local Docker Postgres, port 5432).
"""

from __future__ import annotations

import argparse
import asyncio
import io
import random
import statistics
import sys
import time
import uuid
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_COUNT = 2000

# ---------------------------------------------------------------------------
# Demo reference data — fictional brands (ديمو*), real generic INNs.
# ---------------------------------------------------------------------------

AR_ROOTS = [
    "فين", "مول", "ريل", "سيل", "توس", "فارما", "كير", "ميد", "دول", "زول",
    "فلكس", "برو", "هيس", "سير", "جلو", "كالس", "فيتو", "برون", "كور", "نور",
]
AR_ENDS = ["ادول", "انكس", "اريل", "ستال", "فار", "فين", "كوم", "زول", "بورين", "تيك"]
LATIN_ROOTS = [
    "Fen", "Mol", "Rel", "Sil", "Tuss", "Pharma", "Care", "Med", "Dol", "Zol",
    "Flex", "Pro", "Hist", "Ser", "Gluco", "Calc", "Vita", "Bron", "Cort", "Nur",
]
LATIN_ENDS = ["adol", "anx", "aryl", "stall", "phar", "fen", "com", "zol", "porin", "tech"]

# Parallel lists: (arabic form, route, dose strengths for solid/liquid forms).
FORMS = [
    ("أقراص فيلم مغلفة", "oral", ["100 مجم", "250 مجم", "500 مجم", "1 جم"]),
    ("كبسولات", "oral", ["100 مجم", "250 مجم", "500 مجم"]),
    ("شراب", "oral", ["120 مل", "60 مل", "100 مل"]),
    ("شراب للأطفال", "oral", ["60 مل", "100 مل"]),
    ("تحاميل", "rectal", ["100 مجم", "250 مجم", "500 مجم"]),
    ("امبولات عضلية", "intramuscular", ["500 مجم/مل", "250 مجم/مل", "1 جم"]),
    ("نقط للعين", "ophthalmic", ["5 مل", "10 مل"]),
    ("بخاخ استنشاق", "inhalation", ["100 مكروجرام", "250 مكروجرام"]),
    ("مرهم جلدي", "topical", ["5 جم", "15 جم", "30 جم"]),
    ("لوشن جلدي", "topical", ["60 مل", "120 مل"]),
]

# (ingredient, drug_class, category, requires_prescription)
INGREDIENTS = [
    ("باراسيتامول", "مسكن غير أفيوني", "مسكنات وخوافض حرارة", False),
    ("إيبوبروفين", "مسكن غير ستيرويدي", "مسكنات وخوافض حرارة", False),
    ("ديكلوفيناك", "مسكن غير ستيرويدي", "مسكنات وخوافض حرارة", True),
    ("أموكسيسيلين", "مضاد حيوي بنسلين", "مضادات حيوية", True),
    ("أزيثروميسين", "مضاد حيوي ماكروليد", "مضادات حيوية", True),
    ("سيفترياكسون", "مضاد حيوي سيفالوسبورين", "مضادات حيوية", True),
    ("ميترونيدازول", "مضاد أميبي وميكروبي", "مضادات حيوية", True),
    ("فلوكونازول", "مضاد فطريات", "مضادات حيوية", True),
    ("أوميبرازول", "مثبط مضخة البروتون", "الجهاز الهضمي", True),
    ("رانيتيدين", "مضاد هيستامين H2", "الجهاز الهضمي", False),
    ("أتورفاستاتين", "خافض كوليسترول", "القلب والضغط والسكر", True),
    ("لوسارتان", "خافض ضريو (سارتان)", "القلب والضغط والسكر", True),
    ("أملوديبين", "حاصر قنوات الكالسيوم", "القلب والضغط والسكر", True),
    ("ميتفورمين", "خافض سكر دموي", "القلب والضغط والسكر", True),
    ("سيتيريزين", "مضاد هيستامين", "الحساسية والمناعة", False),
    ("كلورفينامين", "مضاد هيستامين", "الحساسية والمناعة", False),
    ("سالبوتامول", "موسع قصبي", "الجهاز التنفسي", True),
    ("بوديزونيد", "كورتيزون مستنشق", "الجهاز التنفسي", True),
    ("فيتامين ج", "فيتامين", "فيتامينات ومكملات", False),
    ("كالسيوم وفيتامين د", "مكمل غذائي", "فيتامينات ومكملات", False),
]

CATEGORIES = sorted({row[2] for row in INGREDIENTS})

MANUFACTURERS = [
    "ديمو فارما للصناعات الدوائية",
    "الشركة المصرية للأدوية التجريبية",
    "ديمو إنترناشونال فارما",
    "مصنع النيل للأدوية التجريبية",
    "الشرق الأوسط للأدوية التجريبية",
    "ديمو ميد للصناعات الدوائية",
]

STORAGE = [
    "يحفظ في مكان جاف بدرجة حرارة أقل من 30 مئوية",
    "يحفظ في الثلاجة من 2 إلى 8 مئوية",
    "يحفظ بعيداً عن الضوء والرطوبة",
]

NAMES_SPAN = len(AR_ROOTS) * len(AR_ENDS) * len(FORMS)  # unique (combo, variant) pairs


def _demo_row(seq: int) -> dict:
    """Deterministic synthetic catalog row for sequence number `seq`."""
    variant = seq % NAMES_SPAN
    combo, v = divmod(variant, len(FORMS))
    root_i, end_i = combo % len(AR_ROOTS), combo // len(AR_ROOTS)
    ar_name = f"ديمو{AR_ROOTS[root_i]}{AR_ENDS[end_i]}"
    latin = f"Demo{LATIN_ROOTS[root_i]}{LATIN_ENDS[end_i]}"
    form_ar, route, strengths = FORMS[v]
    ingredient, drug_class, category, rx = INGREDIENTS[(combo * 7 + v * 3) % len(INGREDIENTS)]

    r = random.Random(0xD5EED000 + seq)
    box_price = Decimal(r.randint(1050, 35000)) / 100  # 10.50 .. 350.00 EGP
    strips_per_box = r.choice([2, 3, 4])
    units_per_strip = r.choice([10, 12, 15, 20])
    strip_price = max((box_price / strips_per_box).quantize(Decimal("0.01"), ROUND_HALF_UP), Decimal("1.00"))
    tablet_price = max((strip_price / units_per_strip).quantize(Decimal("0.01"), ROUND_HALF_UP), Decimal("0.25"))

    return {
        "seq": seq,
        "trade_name": f"DEMO-{latin}-{seq:05d}",
        "trade_name_ar": f"{ar_name} {strengths[seq % len(strengths)]} {form_ar}",
        "scientific_name": ingredient,
        "drug_class": drug_class,
        "category": category,
        "route": route,
        "manufacturer": MANUFACTURERS[seq % len(MANUFACTURERS)],
        "requires_prescription": rx,
        "storage_conditions": STORAGE[seq % len(STORAGE)],
        "barcode": f"622{seq:010d}",  # GS1 Egypt prefix — demo, never real
        "prices": (box_price, strip_price, tablet_price),
        "packaging_factor": (strips_per_box, units_per_strip),
    }


# ---------------------------------------------------------------------------
# DB plumbing (COPY text format — same pattern as tools/release-prep).
# ---------------------------------------------------------------------------


def _esc(v: object) -> str:
    if v is None:
        return "\\N"
    if isinstance(v, bool):
        return "t" if v else "f"
    return str(v)


async def _copy(conn, table: str, columns: list[str], records: list[tuple]) -> None:
    payload = "\n".join("\t".join(_esc(v) for v in row) for row in records) + "\n"
    await conn.copy_to_table(table, columns=columns, source=io.BytesIO(payload.encode("utf-8")))


def _demo_url_from_env_file() -> str | None:
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return None
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _device_url() -> str:
    """URL of the INSTALLED app's bundled Postgres (config.py production
    defaults: 127.0.0.1:5433/pharmaos, user pharmaos) with the password read
    from the OS keystore — same mechanism the runtime itself uses. The
    password is used in-process and never printed or logged."""
    src = REPO_ROOT / "apps" / "api" / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    from urllib.parse import quote

    from pharmaos_api.security.keystore import get_db_password

    password = get_db_password()
    if not password:
        sys.exit("DB_PASSWORD missing from the device keystore — first-run setup not completed")
    return f"postgresql://pharmaos:{quote(password, safe='')}@127.0.0.1:5433/pharmaos"


def resolve_url(args: argparse.Namespace) -> str:
    import os

    if args.device:
        return _device_url()
    url = args.url or os.environ.get("PHARMAOS_SEED_URL") or _demo_url_from_env_file()
    if not url:
        sys.exit("No DATABASE_URL found — pass --url or set PHARMAOS_SEED_URL")
    return url


async def _ensure_reference_rows(conn) -> tuple[dict[str, str], dict[str, str]]:
    """Find-or-create the demo categories; return {name: id} and {unit: id}."""
    cat_ids: dict[str, str] = {}
    for name in CATEGORIES:
        existing = await conn.fetchval(
            "SELECT id FROM categories WHERE name_ar = $1 AND NOT is_deleted", name
        )
        if existing:
            cat_ids[name] = str(existing)
        else:
            cat_ids[name] = str(
                await conn.fetchval("INSERT INTO categories (name_ar) VALUES ($1) RETURNING id", name)
            )

    unit_ids: dict[str, str] = {}
    for name in ("علبة", "شريط", "قرص"):
        row = await conn.fetchrow(
            """
            INSERT INTO units (name_ar) VALUES ($1)
            ON CONFLICT (name_ar) DO UPDATE SET name_ar = EXCLUDED.name_ar
            RETURNING id
            """,
            name,
        )
        unit_ids[name] = str(row["id"])
    return cat_ids, unit_ids


async def _demo_high_water(conn) -> int:
    row = await conn.fetchrow(
        "SELECT coalesce(max(substring(trade_name from '(\\d+)$')::bigint), 0) AS mx "
        "FROM medications WHERE trade_name LIKE 'DEMO-%'"
    )
    return int(row["mx"])


async def seed(conn, count: int) -> None:
    cat_ids, unit_ids = await _ensure_reference_rows(conn)
    start = await _demo_high_water(conn) + 1
    rows = [_demo_row(s) for s in range(start, start + count)]
    med_ids = [uuid.uuid4() for _ in rows]
    box_pkg_ids = [uuid.uuid4() for _ in rows]

    await _copy(
        conn,
        "medications",
        columns=[
            "id", "trade_name", "trade_name_ar", "scientific_name", "manufacturer",
            "category_id", "drug_class", "route", "requires_prescription",
            "controlled_substance", "storage_conditions",
        ],
        records=[
            (
                med_id, row["trade_name"], row["trade_name_ar"], row["scientific_name"],
                row["manufacturer"], cat_ids[row["category"]], row["drug_class"], row["route"],
                row["requires_prescription"], False, row["storage_conditions"],
            )
            for med_id, row in zip(med_ids, rows, strict=True)
        ],
    )
    # Three packaging levels per medication: 1=box (default sale level),
    # 2=strip, 3=tablet — priced per level (DECIMAL, never FLOAT).
    packaging_records: list[tuple] = []
    for i, (med_id, row) in enumerate(zip(med_ids, rows, strict=True)):
        strips_per_box, units_per_strip = row["packaging_factor"]
        for level, unit, name_ar, qty, price in (
            (1, "علبة", "علبة", None, row["prices"][0]),
            (2, "شريط", "شريط", strips_per_box, row["prices"][1]),
            (3, "قرص", "قرص", units_per_strip, row["prices"][2]),
        ):
            pkg_id = box_pkg_ids[i] if level == 1 else uuid.uuid4()
            packaging_records.append(
                (pkg_id, med_id, level, unit_ids[unit], name_ar, qty, True, price, level == 1)
            )
    await _copy(
        conn,
        "medication_packaging",
        columns=[
            "id", "medication_id", "level", "unit_id", "name_ar", "qty_in_parent",
            "is_sellable", "selling_price", "is_default_sale",
        ],
        records=packaging_records,
    )
    # Primary barcode on the box level (scan -> display < 50ms path).
    await _copy(
        conn,
        "medication_barcodes",
        columns=["id", "medication_id", "packaging_id", "barcode", "barcode_type", "is_primary"],
        records=[
            (uuid.uuid4(), med_id, pkg_id, row["barcode"], "EAN13", True)
            for med_id, pkg_id, row in zip(med_ids, box_pkg_ids, rows, strict=True)
        ],
    )
    await conn.execute("ANALYZE medications, medication_packaging, medication_barcodes")


async def remove(conn) -> None:
    """Soft-delete everything demo (rule #1: no hard deletes)."""
    demo_meds = await conn.fetchval("SELECT count(*) FROM medications WHERE trade_name LIKE 'DEMO-%' AND NOT is_deleted")
    if not demo_meds:
        print("no active DEMO rows found — nothing to remove")
        return
    await conn.execute(
        """
        UPDATE medication_barcodes SET is_deleted = TRUE, updated_at = NOW()
        WHERE NOT is_deleted AND medication_id IN (
            SELECT id FROM medications WHERE trade_name LIKE 'DEMO-%')
        """
    )
    await conn.execute(
        """
        UPDATE medication_packaging SET is_deleted = TRUE, updated_at = NOW()
        WHERE NOT is_deleted AND medication_id IN (
            SELECT id FROM medications WHERE trade_name LIKE 'DEMO-%')
        """
    )
    await conn.execute(
        "UPDATE medications SET is_deleted = TRUE, updated_at = NOW() WHERE NOT is_deleted AND trade_name LIKE 'DEMO-%'"
    )
    # Drop demo categories only once nothing references them anymore.
    deleted_cats = await conn.fetchval(
        """
        WITH upd AS (
            UPDATE categories SET is_deleted = TRUE, updated_at = NOW()
            WHERE name_ar = ANY($1::text[]) AND NOT is_deleted AND NOT EXISTS (
                SELECT 1 FROM medications m
                WHERE m.category_id = categories.id AND NOT m.is_deleted)
            RETURNING 1)
        SELECT count(*) FROM upd
        """,
        CATEGORIES,
    )
    print(f"soft-deleted: {demo_meds} demo medications (+ their packaging & barcodes), {deleted_cats} demo categories")


async def stats(conn) -> None:
    row = await conn.fetchrow(
        """
        SELECT
          (SELECT count(*) FROM medications WHERE trade_name LIKE 'DEMO-%' AND NOT is_deleted) AS demo_meds,
          (SELECT count(*) FROM medications WHERE NOT is_deleted) AS all_meds,
          (SELECT count(*) FROM medication_packaging p
             JOIN medications m ON m.id = p.medication_id
             WHERE m.trade_name LIKE 'DEMO-%' AND NOT p.is_deleted AND NOT m.is_deleted) AS demo_pkg,
          (SELECT count(*) FROM medication_barcodes b
             JOIN medications m ON m.id = b.medication_id
             WHERE m.trade_name LIKE 'DEMO-%' AND NOT b.is_deleted AND NOT m.is_deleted) AS demo_barcodes,
          (SELECT count(*) FROM categories WHERE name_ar = ANY($1::text[]) AND NOT is_deleted) AS demo_cats
        """,
        CATEGORIES,
    )
    print(f"demo medications : {row['demo_meds']} (of {row['all_meds']} total active)")
    print(f"demo packaging   : {row['demo_pkg']} levels (box/strip/tablet)")
    print(f"demo barcodes    : {row['demo_barcodes']}")
    print(f"demo categories  : {row['demo_cats']}")


# ---------------------------------------------------------------------------
# Timing via the REAL service layer (what the UI actually calls) — mirrors
# tools/release-prep/scale_test.py but against the dev database.
# ---------------------------------------------------------------------------


def _pct(values: list[float]) -> dict[str, float]:
    s = sorted(values)
    return {
        "p50": round(statistics.median(s) * 1000, 1),
        "p95": round(s[max(0, int(len(s) * 0.95) - 1)] * 1000, 1),
        "max": round(s[-1] * 1000, 1),
    }


async def measure(url: str, samples: int) -> None:
    import os

    os.environ["PHARMAOS_ENV"] = "test"
    os.environ["DATABASE_URL"] = url
    src = REPO_ROOT / "apps" / "api" / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))

    from pharmaos_api.config import get_settings
    from pharmaos_api.db import get_session_factory
    from pharmaos_api.services.catalog_service import list_medications
    from pharmaos_api.services.sales_service import resolve_scan_code

    get_settings.cache_clear()

    async def run() -> dict[str, list[float]]:
        factory = get_session_factory()
        async with factory() as session:
            _, total = await list_medications(session, search=None, skip=0, limit=1)
            if total == 0:
                sys.exit("no medications found — seed first")

            first_demo = await session.execute(
                __import__("sqlalchemy").text(
                    "SELECT barcode FROM medication_barcodes b "
                    "JOIN medications m ON m.id = b.medication_id "
                    "WHERE m.trade_name LIKE 'DEMO-%' AND NOT b.is_deleted "
                    "ORDER BY b.barcode LIMIT 1"
                )
            )
            barcode = first_demo.scalar_one_or_none()

            async def timed(op) -> list[float]:
                out = []
                for _ in range(samples):
                    t0 = time.perf_counter()
                    await op()
                    out.append(time.perf_counter() - t0)
                return out

            out: dict[str, list[float]] = {
                "catalog_page_ms": await timed(
                    lambda: list_medications(session, search=None, skip=0, limit=20)
                ),
                "search_arabic_exact_ms": await timed(
                    lambda: list_medications(session, search="ديموفينادول", skip=0, limit=20)
                ),
                "search_arabic_typo_ms": await timed(
                    lambda: list_medications(session, search="ديموفنادول", skip=0, limit=20)
                ),
                "search_english_prefix_ms": await timed(
                    lambda: list_medications(session, search="DEMO-", skip=0, limit=20)
                ),
            }
            if barcode:
                out["scan_barcode_ms"] = await timed(
                    lambda: resolve_scan_code(session, barcode)
                )
            return out

    results = {key: _pct(values) for key, values in (await run()).items()}
    print(f"\n{'operation':<26} {'p50(ms)':>9} {'p95(ms)':>9} {'max(ms)':>9}")
    for key, val in results.items():
        print(f"{key:<26} {val['p50']:>9.1f} {val['p95']:>9.1f} {val['max']:>9.1f}")


# ---------------------------------------------------------------------------


async def main_async(args: argparse.Namespace) -> None:
    import asyncpg

    url = resolve_url(args)
    conn = await asyncpg.connect(url, timeout=10)
    try:
        if args.remove:
            await remove(conn)
        elif args.stats:
            await stats(conn)
        elif args.measure:
            await measure(url, args.samples)
        else:
            t0 = time.perf_counter()
            await seed(conn, args.count)
            took = time.perf_counter() - t0
            print(f"seeded {args.count} demo medications in {took:.2f}s")
            await stats(conn)
            if args.measure:
                print()
                await measure(url, args.samples)
    finally:
        await conn.close()


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="Seed/remove a fictional demo catalog (dev DB only)")
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT, help=f"demo medications to add (default {DEFAULT_COUNT})")
    parser.add_argument("--url", help="database URL override (default: repo .env DATABASE_URL)")
    parser.add_argument("--device", action="store_true",
                        help="seed the INSTALLED app's bundled Postgres (127.0.0.1:5433, "
                             "password from the OS keystore) instead of the dev Docker DB")
    parser.add_argument("--remove", action="store_true", help="soft-delete all DEMO rows")
    parser.add_argument("--stats", action="store_true", help="show demo row counts only")
    parser.add_argument("--measure", action="store_true", help="time real catalog/search/scan calls after seeding")
    parser.add_argument("--samples", type=int, default=30, help="samples per measured operation (default 30)")
    args = parser.parse_args()
    asyncio.run(main_async(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
