"""Synthetic release-scale benchmark for PharmaOS.

The benchmark owns a temporary PostgreSQL database named
``pharmaos_release_perf``. It creates fictional rows only, measures the real
catalog/POS/reporting service paths, writes a JSON result, and drops the
temporary database in a finally block.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import statistics
import time
from decimal import Decimal
from pathlib import Path

import asyncpg
from sqlalchemy import text

DB_NAME = "pharmaos_release_perf"
ADMIN_URL = os.environ.get("PERF_ADMIN_DATABASE_URL", "postgresql://postgres@127.0.0.1:5433/postgres")
DB_URL = ADMIN_URL.rsplit("/", 1)[0] + f"/{DB_NAME}"
RESULTS_DIR = Path(__file__).resolve().parent / "results"


async def _reset_db() -> None:
    conn = await asyncpg.connect(ADMIN_URL)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{DB_NAME}"')
    finally:
        await conn.close()


async def _seed() -> tuple[str, str]:
    conn = await asyncpg.connect(DB_URL)
    try:
        await conn.execute(
            """
            INSERT INTO branches (name, country_code, currency_code)
            VALUES ('GEN-PERF-BRANCH', 'EG', 'EGP')
            """
        )
        branch_id = str(await conn.fetchval("SELECT id FROM branches WHERE name='GEN-PERF-BRANCH'"))
        role_id = await conn.fetchval("SELECT id FROM roles WHERE code='cashier'")
        user_id = str(
            await conn.fetchval(
                """
                INSERT INTO users (username, full_name, password_hash, role_id)
                VALUES ('gen_perf_cashier', 'GEN PERF CASHIER', 'GEN-TEST-ONLY', $1)
                RETURNING id
                """,
                role_id,
            )
        )
        await conn.execute(
            """
            INSERT INTO medications (trade_name, trade_name_ar)
            SELECT 'GEN-PERF-' || lpad(g::text, 5, '0'),
                   'GEN-PERF-AR-' || lpad(g::text, 5, '0')
            FROM generate_series(1, 10000) AS g
            """
        )
        await conn.execute(
            """
            UPDATE medications
            SET trade_name_ar = CASE
                WHEN trade_name = 'GEN-PERF-09999' THEN 'كونجيستال'
                WHEN trade_name = 'GEN-PERF-10000' THEN 'إيبوبروفين'
                ELSE trade_name_ar
            END
            WHERE trade_name IN ('GEN-PERF-09999', 'GEN-PERF-10000')
            """
        )
        med_id = str(
            await conn.fetchval("SELECT id FROM medications WHERE trade_name='GEN-PERF-00001'")
        )
        unit_id = await conn.fetchval("SELECT id FROM units ORDER BY name_ar LIMIT 1")
        packaging_id = str(
            await conn.fetchval(
                """
                INSERT INTO medication_packaging
                    (medication_id, level, unit_id, name_ar, selling_price, is_default_sale)
                VALUES ($1, 1, $2, 'GEN-TEST-UNIT', 5.00, TRUE)
                RETURNING id
                """,
                med_id,
                unit_id,
            )
        )
        await conn.execute(
            """
            INSERT INTO medication_barcodes (medication_id, packaging_id, barcode, is_primary)
            VALUES ($1, $2, 'GEN-PERF-BARCODE', TRUE)
            """,
            med_id,
            packaging_id,
        )
        batch_id = str(
            await conn.fetchval(
                """
                INSERT INTO medication_batches
                    (branch_id, medication_id, batch_number, expiry_date, quantity, purchase_price)
                VALUES ($1, $2, 'GEN-PERF-BATCH', CURRENT_DATE + 730, 900000000, 2.00)
                RETURNING id
                """,
                branch_id,
                med_id,
            )
        )
        await conn.execute(
            """
            INSERT INTO branch_inventory (branch_id, medication_id, cached_quantity, reorder_point)
            SELECT $1, id, 1000, 10
            FROM medications
            WHERE trade_name LIKE 'GEN-PERF-%'
            """,
            branch_id,
        )
        await conn.execute(
            """
            INSERT INTO invoices
                (branch_id, invoice_number, currency_code, subtotal, total, created_by)
            SELECT $1, 'GEN-PERF-INV-' || lpad(g::text, 5, '0'), 'EGP', 5.00, 5.00, $2
            FROM generate_series(1, 50000) AS g
            """,
            branch_id,
            user_id,
        )
        await conn.execute(
            """
            INSERT INTO invoice_items
                (branch_id, invoice_id, medication_id, packaging_id, batch_id,
                 quantity, qty_smallest, unit_price, line_total, created_by)
            SELECT $1, i.id, $2, $3, $4, 1, 1, 5.00, 5.00, $5
            FROM invoices i
            WHERE i.invoice_number LIKE 'GEN-PERF-INV-%'
            """,
            branch_id,
            med_id,
            packaging_id,
            batch_id,
            user_id,
        )
        await conn.execute(
            """
            INSERT INTO audit_logs (action, actor_user_id, actor_username, branch_id, entity_type)
            SELECT 'gen.perf', $1, 'gen_perf_cashier', $2, 'invoice'
            FROM generate_series(1, 50000)
            """,
            user_id,
            branch_id,
        )
        await conn.execute("ANALYZE")
        return branch_id, user_id
    finally:
        await conn.close()


async def _measure(branch_id: str, user_id: str) -> dict[str, object]:
    os.environ["PHARMAOS_ENV"] = "test"
    os.environ["DATABASE_URL"] = DB_URL
    from pharmaos_api import db
    from pharmaos_api.config import get_settings
    from pharmaos_api.models import Branch, User
    from pharmaos_api.services import catalog_service, inventory_service, reporting_service, sales_service
    from pharmaos_api.services.sales_service import SaleLine

    get_settings.cache_clear()
    factory = db.get_session_factory()
    async with factory() as session:
        branch = await session.get(Branch, branch_id)
        cashier = await session.get(User, user_id)
        assert branch is not None and cashier is not None

        async def search() -> None:
            await catalog_service.list_medications(
                session, search="GEN-PERF-09999", skip=0, limit=20
            )

        async def arabic_exact_search() -> None:
            await catalog_service.list_medications(
                session, search="كونجيستال", skip=0, limit=20
            )

        async def arabic_typo_search() -> None:
            await catalog_service.list_medications(
                session, search="كونجستال", skip=0, limit=20
            )

        async def sale() -> None:
            await sales_service.create_sale(
                session,
                branch_id=branch.id,
                lines=[SaleLine(barcode="GEN-PERF-BARCODE", quantity=Decimal("1"))],
                cashier=cashier,
            )

        async def sales_report() -> None:
            await reporting_service.sales_report(
                session,
                branch_id=branch.id,
                date_from=dt.date.today() - dt.timedelta(days=365),
                date_to=dt.date.today(),
                granularity="day",
                top_limit=10,
            )

        async def inventory_report() -> None:
            await inventory_service.stock_level_report(
                session, branch_id=branch.id, skip=0, limit=50
            )

        async def timed(operation, repeats: int = 5) -> list[float]:
            await operation()
            values: list[float] = []
            for _ in range(repeats):
                start = time.perf_counter()
                await operation()
                values.append((time.perf_counter() - start) * 1000)
            return values

        measurements: dict[str, list[float]] = {
            "search_medication_ms": await timed(search),
            "arabic_exact_search_ms": await timed(arabic_exact_search),
            "arabic_typo_search_ms": await timed(arabic_typo_search),
            "daily_sales_report_ms": await timed(sales_report),
            "inventory_report_ms": await timed(inventory_report),
        }

        async def explain(query: str) -> list[str]:
            rows = (
                await session.execute(
                    text("EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) " + query)
                )
            ).scalars().all()
            return [str(row) for row in rows]

        explain_plans = {
            "arabic_exact": await explain(
                "SELECT id FROM medications WHERE NOT is_deleted "
                "AND search_vector @@ plainto_tsquery('arabic_simple', normalize_arabic('كونجيستال'))"
            ),
            "arabic_typo_trigram": await explain(
                "SELECT id FROM medications WHERE NOT is_deleted "
                "AND normalize_arabic(trade_name_ar) % normalize_arabic('كونجستال')"
            ),
            "arabic_typo_levenshtein_fallback": await explain(
                "SELECT id FROM medications WHERE NOT is_deleted "
                "AND levenshtein_less_equal(normalize_arabic(trade_name_ar), "
                "normalize_arabic('كونجستال'), 1) <= 1"
            ),
        }
        sale_times: list[float] = []
        for _ in range(5):
            start = time.perf_counter()
            await sale()
            sale_times.append((time.perf_counter() - start) * 1000)
        measurements["complete_sale_ms"] = sale_times
        return {
            "database": DB_NAME,
            "fictional_medications": 10000,
            "fictional_invoices": 50000,
            "search_queries": {
                "arabic_exact": "كونجيستال",
                "arabic_typo": "كونجستال",
            },
            "explain_analyze": explain_plans,
            "measurements_ms": {
                key: {
                    "samples": [round(value, 2) for value in values],
                    "median": round(statistics.median(values), 2),
                    "max": round(max(values), 2),
                }
                for key, values in measurements.items()
            },
        }


async def _drop_db() -> None:
    conn = await asyncpg.connect(ADMIN_URL)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)')
    finally:
        await conn.close()


async def main() -> None:
    from pharmaos_api.migrations_runner import run_migrations

    await _reset_db()
    try:
        os.environ["PHARMAOS_ENV"] = "test"
        os.environ["DATABASE_URL"] = DB_URL
        run_migrations(DB_URL)
        branch_id, user_id = await _seed()
        result = await _measure(branch_id, user_id)
    finally:
        await _drop_db()
    RESULTS_DIR.mkdir(exist_ok=True)
    path = RESULTS_DIR / f"volume_results_{time.strftime('%Y%m%dT%H%M%S')}.json"
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    print(f"results written to {path}")


if __name__ == "__main__":
    asyncio.run(main())
