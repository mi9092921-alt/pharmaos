"""Release-prep scale harness (Phase 3 of the go-live plan).

Seeds a clearly-fictional catalog (10,000 medications) and history
(50,000 invoices over the last 60 days, 1-3 items each) into a DEDICATED
throwaway database (pharmaos_scale on the harness cluster), then measures the
REAL service-layer call paths:

  - POS scan          resolve_scan_code (indexed barcode path)
  - Arabic search     list_medications (FTS + trigram fallback)
  - POS sale          create_sale (the full atomic transaction + commit)
  - daily sales       reporting_service.sales_report (today)
  - 60-day sales      reporting_service.sales_report (full history)
  - stock level       inventory_service.stock_level_report

All timings are wall-clock around the service call (HTTP/JWT/CSRF excluded —
those add single-digit milliseconds; the targets are seconds).

Usage:
  python tools/release-prep/scale_test.py --port 5434 --invoices 50000 --meds 10000
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import random
import statistics
import time
import uuid
from decimal import Decimal
from pathlib import Path

DB_NAME = "pharmaos_scale"
MED_COUNT_DEFAULT = 10_000
INVOICE_COUNT_DEFAULT = 50_000
BRANCH_NAME = "فرع اختبار الحجم"
CASHIER_NAME = "scale_cashier"
RESULTS_DIR = Path(__file__).resolve().parent / "results"


def db_url(port: int, db: str = DB_NAME) -> str:
    return f"postgresql://postgres@127.0.0.1:{port}/{db}"


def pgdata_default() -> Path:
    base = Path(
        __import__("os").environ.get(
            "LOCALAPPDATA", str(Path.home() / "AppData" / "Local")
        )
    )
    return base / "Temp" / "pharmaos-relprep" / "pgcrash"


# ---------------------------------------------------------------------------
# COPY helper (asyncpg takes a source stream, not row objects)
# ---------------------------------------------------------------------------


def _esc(v: object) -> str:
    if v is None:
        return "\\N"
    if isinstance(v, bool):
        return "t" if v else "f"
    return str(v)


async def _copy(conn: object, table: str, columns: list[str], records: list[tuple]) -> None:
    """COPY ... FROM STDIN in text format. Values are canonical PG literals —
    seeded strings contain no tabs/newlines/backslashes by construction."""
    import io

    payload = "\n".join("\t".join(_esc(v) for v in row) for row in records) + "\n"
    buf = io.BytesIO(payload.encode("utf-8"))
    await conn.copy_to_table(table, columns=columns, source=buf)


# ---------------------------------------------------------------------------
# fresh database
# ---------------------------------------------------------------------------


def reset_db(port: int) -> None:
    import asyncpg

    async def _reset() -> None:
        conn = await asyncpg.connect(db_url(port, "postgres"))
        try:
            await conn.execute(f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)')
            await conn.execute(f'CREATE DATABASE "{DB_NAME}"')
        finally:
            await conn.close()

    asyncio.run(_reset())
    from pharmaos_api.migrations_runner import run_migrations

    applied = run_migrations(db_url(port))
    print(f"migrations applied fresh: {len(applied['applied'])} (seeds: {applied['seeds']})")


# ---------------------------------------------------------------------------
# seed
# ---------------------------------------------------------------------------


async def _seed_async(port: int, med_count: int, invoice_count: int) -> None:
    import asyncpg

    conn = await asyncpg.connect(db_url(port))
    try:
        branch_id, cashier_id, unit_id = await conn.fetchrow(
            """
            WITH br AS (
                INSERT INTO branches (name, country_code, currency_code)
                VALUES ($1, 'EG', 'EGP') RETURNING id
            ), r AS (
                INSERT INTO roles (code, name_ar, is_system)
                VALUES ('cashier', 'كاشير', TRUE)
                ON CONFLICT (code) DO UPDATE SET name_ar = EXCLUDED.name_ar
                RETURNING id
            ), u AS (
                INSERT INTO users (username, full_name, password_hash, role_id)
                SELECT $2, 'كاشير الحجم (وهمي)', 'x', r.id FROM r
                RETURNING id
            ), un AS (
                INSERT INTO units (name_ar) VALUES ('علبة')
                ON CONFLICT (name_ar) DO UPDATE SET name_ar = EXCLUDED.name_ar
                RETURNING id
            )
            SELECT br.id, u.id, un.id FROM br, u, un
            """,
            BRANCH_NAME,
            CASHIER_NAME,
        )

        rng = random.Random(20261027)
        med_ids = [uuid.uuid4() for _ in range(med_count)]
        pkg_ids = [uuid.uuid4() for _ in range(med_count)]
        batch_ids = [uuid.uuid4() for _ in range(med_count)]

        # --- catalog ------------------------------------------------------
        await _copy(conn, 
            "medications",
            columns=["id", "trade_name", "trade_name_ar"],
            records=[
                (
                    m,
                    f"GEN-SCALE-{i:05d}",
                    f"دواء اختبار حجم {i:05d} (وهمي تماماً)",
                )
                for i, m in enumerate(med_ids)
            ],
        )
        prices = [Decimal(rng.randint(500, 20000)) / 100 for _ in range(med_count)]
        await _copy(conn, 
            "medication_packaging",
            columns=[
                "id",
                "medication_id",
                "level",
                "unit_id",
                "name_ar",
                "selling_price",
                "is_default_sale",
                "is_sellable",
            ],
            records=[
                (p, m, 1, unit_id, "علبة", pr, True, True)
                for p, m, pr in zip(pkg_ids, med_ids, prices, strict=True)
            ],
        )
        await _copy(conn, 
            "medication_barcodes",
            columns=["id", "medication_id", "packaging_id", "barcode", "is_primary"],
            records=[
                (uuid.uuid4(), m, p, f"62{i:011d}", True)
                for i, (m, p) in enumerate(zip(med_ids, pkg_ids, strict=True))
            ],
        )
        today = dt.date.today()
        await _copy(conn, 
            "medication_batches",
            columns=[
                "id",
                "branch_id",
                "medication_id",
                "batch_number",
                "expiry_date",
                "quantity",
                "purchase_price",
                "status",
            ],
            records=[
                (b, branch_id, m, f"SC-{i:05d}", today + dt.timedelta(days=720),
                 Decimal(1_000_000), Decimal("5.00"), "active")
                for i, (b, m) in enumerate(zip(batch_ids, med_ids, strict=True))
            ],
        )

        # --- history: invoices + items + movements + audit ------------------
        invoices: list[tuple[object, ...]] = []
        items: list[tuple[object, ...]] = []
        movements: list[tuple[object, ...]] = []
        audit: list[tuple[object, ...]] = []
        sold_per_med: dict[uuid.UUID, int] = {}
        day_seq: dict[dt.date, int] = {}
        now = dt.datetime.now(dt.UTC)
        for n in range(invoice_count):
            inv_id = uuid.uuid4()
            days_ago = rng.randint(0, 59)
            day = today - dt.timedelta(days=days_ago)
            seq = day_seq.get(day, 0) + 1
            day_seq[day] = seq
            created = dt.datetime.combine(
                day, dt.time(rng.randint(9, 22), rng.randint(0, 59)), tzinfo=dt.UTC
            )
            if days_ago == 0 and created > now:  # keep timestamps in the past
                created = now - dt.timedelta(minutes=rng.randint(1, 300))
            total = Decimal(rng.randint(1000, 50000)) / 100
            invoices.append(
                (
                    inv_id,
                    branch_id,
                    f"INV-{day.strftime('%Y%m%d')}-{seq:05d}",
                    "retail",
                    "completed",
                    "EGP",
                    total,
                    Decimal("0.00"),
                    Decimal("0.00"),
                    total,
                    "cash",
                    created,
                    created,
                    cashier_id,
                )
            )
            for _ in range(rng.randint(1, 3)):
                idx = rng.randrange(med_count)
                med_id, pkg_id, batch_id, price = (
                    med_ids[idx],
                    pkg_ids[idx],
                    batch_ids[idx],
                    prices[idx],
                )
                qty = rng.randint(1, 5)
                item_id = uuid.uuid4()
                line_total = price * qty
                items.append(
                    (
                        item_id,
                        branch_id,
                        inv_id,
                        med_id,
                        pkg_id,
                        batch_id,
                        Decimal(qty),
                        Decimal(qty),
                        price,
                        line_total,
                        created,
                        cashier_id,
                    )
                )
                movements.append(
                    (
                        uuid.uuid4(),
                        branch_id,
                        batch_id,
                        "sale_out",
                        Decimal(-qty),
                        "invoice",
                        inv_id,
                        created,
                        cashier_id,
                    )
                )
                audit.append(
                    (
                        "invoice.created",
                        cashier_id,
                        CASHIER_NAME,
                        branch_id,
                        "invoice",
                        inv_id,
                        "{}",
                        created,
                    )
                )
                sold_per_med[med_id] = sold_per_med.get(med_id, 0) + qty

        await _copy(conn, 
            "invoices",
            columns=[
                "id", "branch_id", "invoice_number", "invoice_type", "status",
                "currency_code", "subtotal", "discount_amount", "tax_amount",
                "total", "payment_method", "created_at", "updated_at", "created_by",
            ],
            records=invoices,
        )
        await _copy(conn, 
            "invoice_items",
            columns=[
                "id", "branch_id", "invoice_id", "medication_id", "packaging_id",
                "batch_id", "quantity", "qty_smallest", "unit_price", "line_total",
                "created_at", "created_by",
            ],
            records=items,
        )
        await _copy(conn, 
            "stock_movements",
            columns=[
                "id", "branch_id", "batch_id", "movement_type", "quantity_delta",
                "reference_type", "reference_id", "created_at", "created_by",
            ],
            records=movements,
        )
        await _copy(conn, 
            "audit_logs",
            columns=[
                "action", "actor_user_id", "actor_username", "branch_id",
                "entity_type", "entity_id", "metadata", "created_at",
            ],
            records=audit,
        )
        await _copy(conn, 
            "branch_inventory",
            columns=["branch_id", "medication_id", "cached_quantity"],
            records=[
                (branch_id, m, Decimal(1_000_000 - sold_per_med.get(m, 0)))
                for m in med_ids
            ],
        )
        await conn.execute("ANALYZE")
        counts = await conn.fetchrow(
            "SELECT (SELECT count(*) FROM medications) AS meds, "
            "(SELECT count(*) FROM invoices) AS inv, "
            "(SELECT count(*) FROM invoice_items) AS items, "
            "(SELECT count(*) FROM stock_movements) AS mov"
        )
        print(
            f"seeded: meds={counts['meds']} invoices={counts['inv']} "
            f"items={counts['items']} movements={counts['mov']}"
        )
        globals()["_SCALE_BRANCH_ID"] = str(branch_id)
        globals()["_SCALE_CASHIER_ID"] = str(cashier_id)
    finally:
        await conn.close()


def seed(port: int, med_count: int, invoice_count: int) -> None:
    import os

    os.environ["PHARMAOS_ENV"] = "test"
    os.environ["DATABASE_URL"] = db_url(port)
    asyncio.run(_seed_async(port, med_count, invoice_count))


# ---------------------------------------------------------------------------
# measurement
# ---------------------------------------------------------------------------


def _pct(values: list[float]) -> dict[str, float]:
    s = sorted(values)
    return {
        "p50": round(statistics.median(s) * 1000, 1),
        "p95": round(s[max(0, int(len(s) * 0.95) - 1)] * 1000, 1),
        "max": round(s[-1] * 1000, 1),
        "n": len(s),
    }


async def _measure_async(args: argparse.Namespace) -> dict[str, object]:
    from sqlalchemy import select, text

    from pharmaos_api.db import get_session_factory
    from pharmaos_api.models import Branch, User
    from pharmaos_api.services import reporting_service
    from pharmaos_api.services.catalog_service import list_medications
    from pharmaos_api.services.inventory_service import stock_level_report
    from pharmaos_api.services.sales_service import SaleLine, create_sale, resolve_scan_code

    factory = get_session_factory()
    results: dict[str, object] = {}
    today = dt.date.today()

    async with factory() as session:
        branch = (
            await session.execute(select(Branch).where(Branch.name == BRANCH_NAME))
        ).scalar_one()
        branch_id = branch.id
        cashier = (
            await session.execute(select(User).where(User.username == CASHIER_NAME))
        ).scalar_one()
        barcodes = [
            r[0]
            for r in (
                await session.execute(
                    text(
                        "SELECT barcode FROM medication_barcodes ORDER BY barcode "
                        "LIMIT 200"
                    )
                )
            ).all()
        ]

        # warm-up
        for b in barcodes[:5]:
            await resolve_scan_code(session, b)

        # 1) POS scan (exact barcode path)
        times: list[float] = []
        for _ in range(100):
            b = random.choice(barcodes)
            t0 = time.perf_counter()
            await resolve_scan_code(session, b)
            times.append(time.perf_counter() - t0)
        results["scan_ms"] = _pct(times)

        # 2) Arabic search — exact FTS + typo (trigram fallback)
        times = []
        for i in random.sample(range(10_000), 50):
            t0 = time.perf_counter()
            await list_medications(session, search=f"اختبار حجم {i:05d}", skip=0, limit=20)
            times.append(time.perf_counter() - t0)
        results["search_exact_ms"] = _pct(times)
        # Realistic typo: one Arabic letter dropped ("دواء" -> "دوا") — must be
        # served by the indexed trigram-similarity branch.
        times = []
        for i in random.sample(range(10_000), 20):
            t0 = time.perf_counter()
            await list_medications(session, search=f"دوا اختبار حجم {i:05d}", skip=0, limit=20)
            times.append(time.perf_counter() - t0)
        results["search_fuzzy_ms"] = _pct(times)
        # Documented worst case: nothing can match (every branch misses) — the
        # levenshtein deep sweep runs once over the catalog. Not an interactive
        # target; measured to bound it.
        times = []
        for i in random.sample(range(10_000), 5):
            t0 = time.perf_counter()
            await list_medications(session, search=f"لا يوجد شئ بهذا الاسم {i:05d}", skip=0, limit=20)
            times.append(time.perf_counter() - t0)
        results["search_miss_ms"] = _pct(times)

        # 3) POS sale — the FULL atomic transaction incl. commit
        times = []
        for _ in range(30):
            lines = [
                SaleLine(barcode=random.choice(barcodes), quantity=Decimal(random.randint(1, 3)))
                for _ in range(random.randint(1, 3))
            ]
            t0 = time.perf_counter()
            await create_sale(session, branch_id=branch_id, lines=lines, cashier=cashier)
            times.append(time.perf_counter() - t0)
        results["sale_ms"] = _pct(times)

        # 4) daily sales report (today's ~800 invoices)
        times = []
        for _ in range(10):
            t0 = time.perf_counter()
            await reporting_service.sales_report(session, branch_id=branch_id,
                                                 date_from=today, date_to=today)
            times.append(time.perf_counter() - t0)
        results["report_daily_ms"] = _pct(times)

        # 5) 60-day sales report (the whole 50k-invoice history)
        times = []
        for _ in range(5):
            t0 = time.perf_counter()
            await reporting_service.sales_report(
                session, branch_id=branch_id,
                date_from=today - dt.timedelta(days=59), date_to=today,
            )
            times.append(time.perf_counter() - t0)
        results["report_60d_ms"] = _pct(times)

        # 6) stock-level report (10k SKUs, first page + summary)
        times = []
        for _ in range(10):
            t0 = time.perf_counter()
            await stock_level_report(session, branch_id=branch_id)
            times.append(time.perf_counter() - t0)
        results["report_stock_ms"] = _pct(times)

    return results


TARGETS = {  # seconds -> compared against p95 (the honest single-number)
    "scan_ms": 50.0,          # product guard: scan->display < 50ms
    "search_exact_ms": 100.0,
    "search_fuzzy_ms": 100.0,
    "search_miss_ms": 500.0,  # worst case bound (deep sweep), not interactive
    "sale_ms": 1000.0,        # go-live target: sale well under 1s
    "report_daily_ms": 3000.0,  # product guard: < 3s daily report
    "report_60d_ms": 3000.0,
    "report_stock_ms": 3000.0,
}


def measure(args: argparse.Namespace) -> dict[str, object]:
    import os

    os.environ["PHARMAOS_ENV"] = "test"
    os.environ["DATABASE_URL"] = db_url(args.port)
    results = asyncio.run(_measure_async(args))
    print(f"{'metric':<18} {'p50(ms)':>9} {'p95(ms)':>9} {'max(ms)':>9} {'n':>4}  target_p95")
    verdicts: dict[str, bool] = {}
    for key, val in results.items():
        target = TARGETS.get(key)
        ok = "" if target is None else ("PASS" if val["p95"] <= target else "FAIL")
        verdicts[key] = ok != "FAIL"
        print(
            f"{key:<18} {val['p50']:>9.1f} {val['p95']:>9.1f} {val['max']:>9.1f} "
            f"{val['n']:>4}  {target if target else '-'} {ok}"
        )
    return {"results": results, "verdicts": verdicts}


def main() -> int:
    parser = argparse.ArgumentParser(description="PharmaOS scale harness (phase 3)")
    parser.add_argument("--port", type=int, default=5434)
    parser.add_argument("--meds", type=int, default=MED_COUNT_DEFAULT)
    parser.add_argument("--invoices", type=int, default=INVOICE_COUNT_DEFAULT)
    parser.add_argument("--skip-seed", action="store_true")
    parser.add_argument("--measure-only", action="store_true")
    args = parser.parse_args()
    if not args.measure_only:
        reset_db(args.port)
        t0 = time.perf_counter()
        seed(args.port, args.meds, args.invoices)
        print(f"seed took {time.perf_counter() - t0:.1f}s")
    report = measure(args)
    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"scale_results_{time.strftime('%Y%m%dT%H%M%S')}.json"
    out.write_text(
        json.dumps({"args": vars(args), **report}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(f"results written to {out}")
    return 0 if all(report["verdicts"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
