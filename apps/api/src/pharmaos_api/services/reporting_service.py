"""Reporting & analytics read models (Phase 3).

P3-M1 — sales reports (daily / monthly / annual).

Design (approved decisions):
- D2 — every report is an ON-DEMAND SQL aggregation (no snapshot/rollup tables);
  covering indexes (idx_invoices_branch_created) keep the daily report within the
  < 3s budget (CLAUDE.md perf targets). Introduce caching only if a report misses
  the target.
- Reports are READ-ONLY: no writes, no audit actions (D7). Every report is
  branch-scoped and filtered by a LOCAL-DAY date range — the device runs in the
  pharmacy's timezone, so comparing against created_at (a timestamptz) with a
  date bound resolves at local midnight, exactly like the Z-report
  (cashier_service.day_report) which the range generalizes.

Money is Decimal end to end, quantized to 2 places and returned as STRINGS in the
envelope (never floats). Quantities (smallest unit) are Numeric(12,3).
"""

import csv
import datetime as dt
import io
import uuid
from decimal import Decimal
from typing import cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.errors import ApiError, ErrorCode

_ZERO = Decimal("0.00")
_GRANULARITIES = frozenset({"day", "month", "year"})


def _q2(value: object) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def _q3(value: object) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.001"))


def _validate(date_from: dt.date, date_to: dt.date, granularity: str) -> str:
    if date_from > date_to:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED, 422, message="date_from must be on or before date_to."
        )
    if granularity not in _GRANULARITIES:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Unknown granularity.")
    return granularity


async def sales_report(
    session: AsyncSession,
    *,
    branch_id: uuid.UUID,
    date_from: dt.date,
    date_to: dt.date,
    granularity: str = "day",
    top_limit: int = 10,
) -> dict[str, object]:
    """A branch's sales over an inclusive local-day range.

    Prices are VAT-inclusive (P2-M6): `total` is what the customer paid, `tax_amount`
    is the VAT extracted from it, `subtotal` is net-of-VAT. Refunds come from the
    returns credit-note ledger (by refund_method), so net_sales = gross − refunds
    mirrors the Z-report's net figure. The half-open bound `created_at < (to + 1
    day)` at local midnight captures the whole final day and stays index-friendly
    (a plain range on created_at, served by idx_invoices_branch_created).
    """
    gran = _validate(date_from, date_to, granularity)
    # Half-open local-day window: [date_from 00:00, (date_to + 1) 00:00). Bounds
    # are bound as dates (asyncpg -> DATE); compared to created_at (timestamptz)
    # they resolve at local midnight, keeping the range index-friendly.
    p: dict[str, object] = {
        "b": branch_id,
        "f": date_from,
        "t_excl": date_to + dt.timedelta(days=1),
    }

    # 1) Per-payment-method totals (also the source of the overall summary).
    pm_rows = (await session.execute(text("""
                SELECT payment_method,
                       COUNT(*)                        AS n,
                       COALESCE(SUM(total), 0)         AS total,
                       COALESCE(SUM(subtotal), 0)      AS subtotal,
                       COALESCE(SUM(discount_amount), 0) AS discount,
                       COALESCE(SUM(tax_amount), 0)    AS tax
                FROM invoices
                WHERE branch_id = :b AND NOT is_deleted AND status = 'completed'
                  AND created_at >= :f AND created_at < :t_excl
                GROUP BY payment_method
                ORDER BY payment_method
                """).bindparams(**p))).all()

    by_payment: list[dict[str, object]] = []
    gross = _ZERO
    subtotal_total = _ZERO
    discount_total = _ZERO
    tax_total = _ZERO
    invoice_count = 0
    for r in pm_rows:
        n = int(r[1])
        by_payment.append({"method": r[0], "count": n, "total": str(_q2(r[2]))})
        gross += Decimal(str(r[2]))
        subtotal_total += Decimal(str(r[3]))
        discount_total += Decimal(str(r[4]))
        tax_total += Decimal(str(r[5]))
        invoice_count += n

    # 2) Refunds (returns credit-note ledger, by refund_method).
    ref_rows = (await session.execute(text("""
                SELECT refund_method, COUNT(*) AS n, COALESCE(SUM(total), 0) AS total
                FROM returns
                WHERE branch_id = :b AND NOT is_deleted
                  AND created_at >= :f AND created_at < :t_excl
                GROUP BY refund_method
                ORDER BY refund_method
                """).bindparams(**p))).all()
    by_refund: list[dict[str, object]] = []
    refunds_total = _ZERO
    refund_count = 0
    for r in ref_rows:
        n = int(r[1])
        by_refund.append({"method": r[0], "count": n, "total": str(_q2(r[2]))})
        refunds_total += Decimal(str(r[2]))
        refund_count += n

    # 3) Time trend, bucketed by the requested granularity. date_trunc takes the
    # unit as a bound TEXT param (whitelisted above) — no SQL interpolation.
    trend_rows = (await session.execute(text("""
                SELECT date_trunc(:g, created_at)::date AS bucket,
                       COUNT(*)                AS n,
                       COALESCE(SUM(total), 0) AS total
                FROM invoices
                WHERE branch_id = :b AND NOT is_deleted AND status = 'completed'
                  AND created_at >= :f AND created_at < :t_excl
                GROUP BY bucket
                ORDER BY bucket
                """).bindparams(**p, g=gran))).all()
    trend = [
        {"bucket": r[0].isoformat(), "count": int(r[1]), "total": str(_q2(r[2]))}
        for r in trend_rows
    ]

    # 4) Top items by revenue over the range (join items -> invoices for the filter).
    top_items: list[dict[str, object]] = []
    if top_limit > 0:
        top_rows = (await session.execute(text("""
                    SELECT ii.medication_id, m.trade_name, m.trade_name_ar,
                           COALESCE(SUM(ii.qty_smallest), 0) AS qty,
                           COALESCE(SUM(ii.line_total), 0)   AS revenue
                    FROM invoice_items ii
                    JOIN invoices i   ON i.id = ii.invoice_id
                    JOIN medications m ON m.id = ii.medication_id
                    WHERE i.branch_id = :b AND NOT i.is_deleted AND i.status = 'completed'
                      AND i.created_at >= :f AND i.created_at < :t_excl
                    GROUP BY ii.medication_id, m.trade_name, m.trade_name_ar
                    ORDER BY revenue DESC, qty DESC
                    LIMIT :lim
                    """).bindparams(**p, lim=top_limit))).all()
        top_items = [
            {
                "medication_id": str(r[0]),
                "name": r[1],
                "name_ar": r[2],
                "qty_smallest": str(_q3(r[3])),
                "revenue": str(_q2(r[4])),
            }
            for r in top_rows
        ]

    net = gross - refunds_total
    avg_invoice = _q2(gross / invoice_count) if invoice_count else _ZERO

    return {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "granularity": gran,
        "summary": {
            "gross_sales": str(_q2(gross)),
            "subtotal": str(_q2(subtotal_total)),
            "discount_total": str(_q2(discount_total)),
            "tax_total": str(_q2(tax_total)),
            "refunds_total": str(_q2(refunds_total)),
            "net_sales": str(_q2(net)),
            "invoice_count": invoice_count,
            "refund_count": refund_count,
            "avg_invoice": str(avg_invoice),
        },
        "by_payment_method": by_payment,
        "by_refund_method": by_refund,
        "trend": trend,
        "top_items": top_items,
    }


async def sales_report_csv(
    session: AsyncSession,
    *,
    branch_id: uuid.UUID,
    date_from: dt.date,
    date_to: dt.date,
    granularity: str = "day",
) -> str:
    """The sales trend as a spreadsheet-ready CSV (one row per period bucket).

    Reuses sales_report as the single source of aggregation truth (top-items join
    skipped — top_limit=0). A UTF-8 BOM is prepended so Excel renders Arabic
    headers/currency correctly on double-click.
    """
    report = await sales_report(
        session,
        branch_id=branch_id,
        date_from=date_from,
        date_to=date_to,
        granularity=granularity,
        top_limit=0,
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["period", "invoice_count", "gross_total"])
    trend = cast("list[dict[str, object]]", report["trend"])
    for row in trend:
        writer.writerow([row["bucket"], row["count"], row["total"]])
    return "\ufeff" + buf.getvalue()
