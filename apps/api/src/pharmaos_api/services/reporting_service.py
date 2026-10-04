"""Reporting & analytics read models (Phase 3).

P3-M1 — sales reports (daily / monthly / annual).
P3-M4 — profit & loss analytics (COGS from the batch, margins, operating net).
P3-M5 — supplier performance + customer analytics.

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
- D3 — COGS basis is the batch's own purchase_price at sale time, via
  invoice_items.batch_id ("batches are the single source of truth"); no separate
  cost-ledger. line_total is VAT-INCLUSIVE (sales_service), so per-line NET
  revenue is line_total − tax_amount — the exact decomposition invoices.subtotal
  is derived from. Returns (credit notes) net out symmetrically: returns.subtotal
  reduces revenue, and return_items reference a batch carrying the ORIGIN batch's
  purchase_price (return_service copies it), so returned cost uses the same unit
  cost the sale was costed at.

Money is Decimal end to end, quantized to 2 places and returned as STRINGS in the
envelope (never floats). Quantities (smallest unit) are Numeric(12,3).
"""

import csv
import datetime as dt
import io
import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.errors import ApiError, ErrorCode
from pharmaos_api.services.csv_safe import csv_safe

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


def _margin_pct(profit: Decimal, revenue: Decimal) -> str | None:
    """Margin percent as a quantized string, or None when there is no revenue to
    express a margin against (an empty range, or fully refunded sales)."""
    if revenue == 0:
        return None
    return str(_q2(profit / revenue * 100))


async def profit_loss_report(
    session: AsyncSession,
    *,
    branch_id: uuid.UUID,
    date_from: dt.date,
    date_to: dt.date,
    granularity: str = "day",
    top_limit: int = 10,
) -> dict[str, object]:
    """A branch's profit & loss over an inclusive local-day range.

    P&L chain (per the approved D3 basis): net revenue = Σ invoices.subtotal
    (net of VAT) − Σ returns.subtotal (credit notes); COGS = Σ
    invoice_items.qty_smallest × batch.purchase_price − the same product over
    return_items; gross profit = net revenue − net COGS; operating profit =
    gross profit − expenses (by expense_date, a local DATE — no time-of-day
    semantics). Margins are per-row profit/revenue, None where revenue is 0.

    Every aggregation follows the same access pattern as sales_report: a branch
    equality plus the half-open created_at window, served by
    idx_invoices_branch_created / idx_returns_branch, so the daily P&L sits
    inside the same < 3s budget as the daily sales report.
    """
    gran = _validate(date_from, date_to, granularity)
    p: dict[str, object] = {
        "b": branch_id,
        "f": date_from,
        "t_excl": date_to + dt.timedelta(days=1),
    }

    def _bucket(row: Sequence[object]) -> str:
        return cast("dt.date", row[0]).isoformat()

    # 1) Sales revenue per bucket (also the summary's revenue side): subtotal is
    #    net of VAT; refunded credit notes are subtracted from it (query 2).
    sales_rows = (await session.execute(text("""
                SELECT date_trunc(:g, created_at)::date AS bucket,
                       COUNT(*)                  AS n,
                       COALESCE(SUM(subtotal), 0) AS subtotal
                FROM invoices
                WHERE branch_id = :b AND NOT is_deleted AND status = 'completed'
                  AND created_at >= :f AND created_at < :t_excl
                GROUP BY bucket
                """).bindparams(**p, g=gran))).all()
    sales_by_bucket = {_bucket(r): (int(r[1]), Decimal(str(r[2]))) for r in sales_rows}

    # 2) Refunds per bucket (returns credit-note ledger).
    refund_rows = (await session.execute(text("""
                SELECT date_trunc(:g, created_at)::date AS bucket,
                       COUNT(*)                  AS n,
                       COALESCE(SUM(subtotal), 0) AS subtotal
                FROM returns
                WHERE branch_id = :b AND NOT is_deleted
                  AND created_at >= :f AND created_at < :t_excl
                GROUP BY bucket
                """).bindparams(**p, g=gran))).all()
    refunds_by_bucket = {_bucket(r): (int(r[1]), Decimal(str(r[2]))) for r in refund_rows}

    # 3) COGS of sold goods per bucket: each invoice_item is a BATCH SLICE whose
    #    batch carries the unit purchase price the sale was costed at (D3).
    cogs_rows = (await session.execute(text("""
                SELECT date_trunc(:g, i.created_at)::date AS bucket,
                       COALESCE(SUM(ii.qty_smallest * b.purchase_price), 0) AS cogs
                FROM invoice_items ii
                JOIN invoices i ON i.id = ii.invoice_id
                JOIN medication_batches b ON b.id = ii.batch_id
                WHERE i.branch_id = :b AND NOT i.is_deleted AND i.status = 'completed'
                  AND NOT ii.is_deleted
                  AND i.created_at >= :f AND i.created_at < :t_excl
                GROUP BY bucket
                """).bindparams(**p, g=gran))).all()
    sold_cogs_by_bucket = {_bucket(r): Decimal(str(r[1])) for r in cogs_rows}

    # 4) COGS given back per bucket: return_items land in a batch that carries
    #    the ORIGIN batch's purchase_price (return_service), so the returned
    #    units cost exactly what the original sale costed them at.
    ret_cogs_rows = (await session.execute(text("""
                SELECT date_trunc(:g, r.created_at)::date AS bucket,
                       COALESCE(SUM(ri.qty_smallest * b.purchase_price), 0) AS cogs
                FROM return_items ri
                JOIN returns r ON r.id = ri.return_id
                JOIN medication_batches b ON b.id = ri.batch_id
                WHERE r.branch_id = :b AND NOT r.is_deleted AND NOT ri.is_deleted
                  AND r.created_at >= :f AND r.created_at < :t_excl
                GROUP BY bucket
                """).bindparams(**p, g=gran))).all()
    returned_cogs_by_bucket = {_bucket(r): Decimal(str(r[1])) for r in ret_cogs_rows}

    # 5) Expenses per bucket. expense_date is a local DATE (no timezone), so the
    #    inclusive BETWEEN needs no half-open dance.
    exp_rows = (await session.execute(text("""
                SELECT date_trunc(:g, expense_date)::date AS bucket,
                       COALESCE(SUM(amount), 0) AS total
                FROM expenses
                WHERE branch_id = :b AND NOT is_deleted
                  AND expense_date >= :f AND expense_date <= :t
                GROUP BY bucket
                """).bindparams(b=branch_id, f=date_from, t=date_to, g=gran))).all()
    expenses_by_bucket = {_bucket(r): Decimal(str(r[1])) for r in exp_rows}

    # 6) Sold-side per-item economics (top-N by profit happens in Python after
    #    returns are netted in — SQL-side LIMIT would miss high-profit items
    #    whose gross revenue is small). Per-line net revenue is
    #    line_total − tax_amount, the exact decomposition of invoices.subtotal.
    sold_item_rows = (await session.execute(text("""
                SELECT ii.medication_id, m.trade_name, m.trade_name_ar, m.category_id,
                       SUM(ii.qty_smallest)              AS qty,
                       SUM(ii.line_total - ii.tax_amount) AS revenue,
                       SUM(ii.qty_smallest * b.purchase_price) AS cogs
                FROM invoice_items ii
                JOIN invoices i ON i.id = ii.invoice_id
                JOIN medications m ON m.id = ii.medication_id
                JOIN medication_batches b ON b.id = ii.batch_id
                WHERE i.branch_id = :b AND NOT i.is_deleted AND i.status = 'completed'
                  AND NOT ii.is_deleted
                  AND i.created_at >= :f AND i.created_at < :t_excl
                GROUP BY ii.medication_id, m.trade_name, m.trade_name_ar, m.category_id
                """).bindparams(**p))).all()

    ret_item_rows = (await session.execute(text("""
                SELECT ri.medication_id,
                       SUM(ri.qty_smallest)               AS qty,
                       SUM(ri.line_total - ri.tax_amount) AS refunded,
                       SUM(ri.qty_smallest * b.purchase_price) AS cogs
                FROM return_items ri
                JOIN returns r ON r.id = ri.return_id
                JOIN medication_batches b ON b.id = ri.batch_id
                WHERE r.branch_id = :b AND NOT r.is_deleted AND NOT ri.is_deleted
                  AND r.created_at >= :f AND r.created_at < :t_excl
                GROUP BY ri.medication_id
                """).bindparams(**p))).all()
    returned_by_med: dict[str, tuple[Decimal, Decimal, Decimal]] = {
        str(r[0]): (Decimal(str(r[1])), Decimal(str(r[2])), Decimal(str(r[3])))
        for r in ret_item_rows
    }

    # Net per-medication economics (sold minus returned), shared by the per-item
    # AND per-category views — both group the same facts under different keys.
    net_by_med: dict[str, dict[str, object]] = {}
    for r in sold_item_rows:
        net_by_med[str(r[0])] = {
            "name": r[1],
            "name_ar": r[2],
            "category_id": r[3],
            "qty": Decimal(str(r[4])),
            "revenue": Decimal(str(r[5])),
            "cogs": Decimal(str(r[6])),
        }
    for med_key, (qty, refunded, ret_cogs) in returned_by_med.items():
        entry = net_by_med.setdefault(
            med_key,
            {
                "name": None,
                "name_ar": None,
                "category_id": None,
                "qty": Decimal(0),
                "revenue": _ZERO,
                "cogs": _ZERO,
            },
        )
        entry["qty"] = cast("Decimal", entry["qty"]) - qty
        entry["revenue"] = cast("Decimal", entry["revenue"]) - refunded
        entry["cogs"] = cast("Decimal", entry["cogs"]) - ret_cogs

    # Category names for the per-category view (cheap single lookup by ids).
    cat_ids = {e["category_id"] for e in net_by_med.values() if e["category_id"]}
    cat_names: dict[str, tuple[object, object]] = {}
    if cat_ids:
        name_rows = (await session.execute(text("""
                    SELECT id, name_ar, name_en FROM categories WHERE id = ANY(:ids)
                    """).bindparams(ids=list(cat_ids)))).all()
        cat_names = {str(r[0]): (r[1], r[2]) for r in name_rows}

    # ---- per-item (top N by NET profit, returns already netted) ----
    item_rows: list[dict[str, object]] = []
    for med_key, entry in net_by_med.items():
        revenue = cast("Decimal", entry["revenue"])
        cogs = cast("Decimal", entry["cogs"])
        profit = revenue - cogs
        item_rows.append(
            {
                "medication_id": med_key,
                "name": entry["name"],
                "name_ar": entry["name_ar"],
                "qty_smallest": str(_q3(cast("Decimal", entry["qty"]))),
                "revenue": str(_q2(revenue)),
                "cogs": str(_q2(cogs)),
                "profit": str(_q2(profit)),
                "margin_percent": _margin_pct(profit, revenue),
            }
        )
    item_rows.sort(
        key=lambda row: (Decimal(str(row["profit"])), Decimal(str(row["revenue"]))),
        reverse=True,
    )
    if top_limit >= 0:
        item_rows = item_rows[:top_limit]

    # ---- per-category (medications.category_id; NULL → uncategorized) ----
    by_cat: dict[str, dict[str, Decimal]] = {}
    for entry in net_by_med.values():
        cat_id = entry["category_id"]
        key = str(cat_id) if cat_id else ""
        agg = by_cat.setdefault(key, {"revenue": _ZERO, "cogs": _ZERO})
        agg["revenue"] += cast("Decimal", entry["revenue"])
        agg["cogs"] += cast("Decimal", entry["cogs"])
    category_rows: list[dict[str, object]] = []
    for key, agg in by_cat.items():
        names = cat_names.get(key, (None, None)) if key else (None, None)
        profit = agg["revenue"] - agg["cogs"]
        category_rows.append(
            {
                "category_id": key or None,
                "name_ar": names[0],
                "name_en": names[1],
                "revenue": str(_q2(agg["revenue"])),
                "cogs": str(_q2(agg["cogs"])),
                "profit": str(_q2(profit)),
                "margin_percent": _margin_pct(profit, agg["revenue"]),
            }
        )
    category_rows.sort(key=lambda row: (row["name_ar"] is None, row["name_ar"] or ""))

    # ---- expenses by category (names joined for the UI table) ----
    exp_cat_rows = (await session.execute(text("""
                SELECT ec.id, ec.name_ar, ec.name_en,
                       COALESCE(SUM(e.amount), 0) AS total
                FROM expenses e
                JOIN expense_categories ec ON ec.id = e.expense_category_id
                WHERE e.branch_id = :b AND NOT e.is_deleted
                  AND e.expense_date >= :f AND e.expense_date <= :t
                GROUP BY ec.id, ec.name_ar, ec.name_en
                ORDER BY total DESC, ec.name_ar
                """).bindparams(b=branch_id, f=date_from, t=date_to))).all()
    by_expense_category = [
        {
            "expense_category_id": str(r[0]),
            "name_ar": r[1],
            "name_en": r[2],
            "total": str(_q2(r[3])),
        }
        for r in exp_cat_rows
    ]

    # ---- assemble summary + merged trend ----
    gross_sales_subtotal = sum((v[1] for v in sales_by_bucket.values()), _ZERO)
    refunds_subtotal = sum((v[1] for v in refunds_by_bucket.values()), _ZERO)
    cogs_sold = sum(sold_cogs_by_bucket.values(), _ZERO)
    cogs_returned = sum(returned_cogs_by_bucket.values(), _ZERO)
    expenses_total = sum(expenses_by_bucket.values(), _ZERO)

    net_revenue = gross_sales_subtotal - refunds_subtotal
    net_cogs = cogs_sold - cogs_returned
    gross_profit = net_revenue - net_cogs
    operating_profit = gross_profit - expenses_total

    invoice_count = sum(v[0] for v in sales_by_bucket.values())
    refund_count = sum(v[0] for v in refunds_by_bucket.values())

    buckets = sorted(
        set(sales_by_bucket)
        | set(refunds_by_bucket)
        | set(sold_cogs_by_bucket)
        | set(returned_cogs_by_bucket)
        | set(expenses_by_bucket)
    )
    trend: list[dict[str, object]] = []
    for b in buckets:
        revenue = sales_by_bucket.get(b, (0, _ZERO))[1] - refunds_by_bucket.get(b, (0, _ZERO))[1]
        cogs = sold_cogs_by_bucket.get(b, _ZERO) - returned_cogs_by_bucket.get(b, _ZERO)
        profit = revenue - cogs
        trend.append(
            {
                "bucket": b,
                "revenue": str(_q2(revenue)),
                "cogs": str(_q2(cogs)),
                "gross_profit": str(_q2(profit)),
                "expenses": str(_q2(expenses_by_bucket.get(b, _ZERO))),
            }
        )

    return {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "granularity": gran,
        "summary": {
            "gross_sales_subtotal": str(_q2(gross_sales_subtotal)),
            "refunds_subtotal": str(_q2(refunds_subtotal)),
            "net_revenue": str(_q2(net_revenue)),
            "cogs_sold": str(_q2(cogs_sold)),
            "cogs_returned": str(_q2(cogs_returned)),
            "net_cogs": str(_q2(net_cogs)),
            "gross_profit": str(_q2(gross_profit)),
            "gross_margin_percent": _margin_pct(gross_profit, net_revenue),
            "expenses_total": str(_q2(expenses_total)),
            "operating_profit": str(_q2(operating_profit)),
            "invoice_count": invoice_count,
            "refund_count": refund_count,
        },
        "by_expense_category": by_expense_category,
        "by_category": category_rows,
        "top_items": item_rows,
        "trend": trend,
    }


async def profit_loss_csv(
    session: AsyncSession,
    *,
    branch_id: uuid.UUID,
    date_from: dt.date,
    date_to: dt.date,
    granularity: str = "day",
) -> str:
    """The P&L trend as a spreadsheet-ready CSV (one row per period bucket).

    Reuses profit_loss_report as the single source of aggregation truth
    (top-items join skipped — top_limit=0). A UTF-8 BOM is prepended so Excel
    renders Arabic headers/currency correctly on double-click.
    """
    report = await profit_loss_report(
        session,
        branch_id=branch_id,
        date_from=date_from,
        date_to=date_to,
        granularity=granularity,
        top_limit=0,
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["period", "revenue", "cogs", "gross_profit", "expenses"])
    trend = cast("list[dict[str, object]]", report["trend"])
    for row in trend:
        writer.writerow(
            [row["bucket"], row["revenue"], row["cogs"], row["gross_profit"], row["expenses"]]
        )
    return "\ufeff" + buf.getvalue()


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


# ============================== P3-M5 — suppliers ==============================
#
# Definitions (kept explicit because "fill rate" means different things):
#   * po_count / ordered_value      — NON-CANCELLED orders in range (any status):
#                                     the purchasing activity level.
#   * fill_rate / full_supply /     — computed ONLY over orders whose delivery has
#     avg_lead_time                   actually begun or finished (status in
#                                     'received' | 'partially_received'). A pending
#                                     or awaiting-receipt order says nothing about
#                                     the supplier's delivery quality yet.
#   * full_supply_rate              — share of those delivered orders that arrived
#                                     COMPLETE (status = 'received').
#   * fill_rate                     — Σ qty_received / Σ qty_ordered (line level,
#                                     smallest units) over the same orders.
#   * avg_lead_time_days            — approval → LAST purchase_in receipt movement
#                                     (stock_movements.reference_type='purchase_
#                                     order'), averaged per order that has one.


async def supplier_performance_report(
    session: AsyncSession,
    *,
    branch_id: uuid.UUID,
    date_from: dt.date,
    date_to: dt.date,
    limit: int = 0,
) -> dict[str, object]:
    """A branch's purchasing activity and supplier delivery performance.

    POs are filtered by ORDER_DATE (the business date, a local DATE like
    expense_date — not created_at). Reuse: the receipt moment comes from the
    append-only stock_movements ledger (reference_type='purchase_order'), NOT
    from any report-side bookkeeping. `limit` caps the ranked supplier rows
    (the ≤100 page-size convention); 0 = unlimited — the CSV export path.
    """
    _validate(date_from, date_to, "day")
    p: dict[str, object] = {"b": branch_id, "f": date_from, "t": date_to}

    # 1) Per-supplier order activity + delivered-order counts.
    po_rows = (await session.execute(text("""
                SELECT po.supplier_id, s.name,
                       COUNT(*)                                  AS po_count,
                       COALESCE(SUM(po.total), 0)                AS ordered_value,
                       COUNT(*) FILTER (WHERE po.status = 'received')            AS full_count,
                       COUNT(*) FILTER (WHERE po.status = 'partially_received')  AS partial_count
                FROM purchase_orders po
                JOIN suppliers s ON s.id = po.supplier_id
                WHERE po.branch_id = :b AND NOT po.is_deleted AND po.status <> 'cancelled'
                  AND po.order_date >= :f AND po.order_date <= :t
                GROUP BY po.supplier_id, s.name
                """).bindparams(**p))).all()

    # 2) Line-level ordered/received quantities and received value, same scope.
    line_rows = (await session.execute(text("""
                SELECT po.supplier_id,
                       COALESCE(SUM(pi.qty_ordered), 0)   AS qty_ordered,
                       COALESCE(SUM(pi.qty_received), 0)  AS qty_received,
                       COALESCE(SUM(pi.qty_received * pi.unit_cost), 0) AS received_value
                FROM purchase_orders po
                JOIN purchase_items pi ON pi.purchase_order_id = po.id
                WHERE po.branch_id = :b AND NOT po.is_deleted AND NOT pi.is_deleted
                  AND po.status IN ('received', 'partially_received')
                  AND po.order_date >= :f AND po.order_date <= :t
                GROUP BY po.supplier_id
                """).bindparams(**p))).all()
    lines_by_supplier = {str(r[0]): r for r in line_rows}

    # 3) Lead time per supplier: approval → last receipt movement for that PO.
    #    SUM + COUNT (not AVG) so the summary can compute the ORDER-WEIGHTED
    #    portfolio average instead of an average-of-supplier-averages, which a
    #    one-order supplier would dominate.
    lead_rows = (await session.execute(text("""
                SELECT po.supplier_id,
                       SUM(EXTRACT(EPOCH FROM (m.last_ts - po.approved_at)) / 86400) AS lead_sum,
                       COUNT(*) AS lead_count
                FROM purchase_orders po
                JOIN (
                    SELECT sm.reference_id AS po_id, MAX(sm.created_at) AS last_ts
                    FROM stock_movements sm
                    WHERE sm.branch_id = :b AND sm.movement_type = 'purchase_in'
                      AND sm.reference_type = 'purchase_order' AND NOT sm.is_deleted
                    GROUP BY sm.reference_id
                ) m ON m.po_id = po.id
                WHERE po.branch_id = :b AND NOT po.is_deleted
                  AND po.status IN ('received', 'partially_received')
                  AND po.approved_at IS NOT NULL
                  AND po.order_date >= :f AND po.order_date <= :t
                GROUP BY po.supplier_id
                """).bindparams(**p))).all()
    lead_by_supplier = {str(r[0]): (Decimal(str(r[1])), int(r[2])) for r in lead_rows}

    def _pct(numerator: Decimal, denominator: Decimal) -> str | None:
        return None if denominator == 0 else str(_q2(numerator / denominator * 100))

    suppliers: list[dict[str, object]] = []
    for r in po_rows:
        key = str(r[0])
        lines = lines_by_supplier.get(key)
        qty_ordered = Decimal(str(lines[1])) if lines else Decimal(0)
        qty_received = Decimal(str(lines[2])) if lines else Decimal(0)
        received_value = Decimal(str(lines[3])) if lines else Decimal(0)
        full_count = int(r[4])
        partial_count = int(r[5])
        delivered = Decimal(full_count + partial_count)
        lead_pair = lead_by_supplier.get(key)
        lead_str = (
            None
            if lead_pair is None
            else str((lead_pair[0] / Decimal(lead_pair[1])).quantize(Decimal("0.1")))
        )
        suppliers.append(
            {
                "supplier_id": key,
                "name": r[1],
                "po_count": int(r[2]),
                "ordered_value": str(_q2(r[3])),
                "received_value": str(_q2(received_value)),
                "fill_rate_percent": _pct(qty_received, qty_ordered),
                "full_supply_rate_percent": _pct(Decimal(full_count), delivered),
                "avg_lead_time_days": lead_str,
            }
        )
    suppliers.sort(key=lambda row: Decimal(str(row["ordered_value"])), reverse=True)
    if limit > 0:
        suppliers = suppliers[:limit]

    total_ordered = sum((Decimal(str(r[3])) for r in po_rows), _ZERO)
    total_received = sum((Decimal(str(lines[3])) for lines in lines_by_supplier.values()), _ZERO)
    full_total = sum(int(r[4]) for r in po_rows)
    partial_total = sum(int(r[5]) for r in po_rows)
    delivered_total = full_total + partial_total

    # Portfolio-wide rates: quantity-weighted fill, order-weighted full supply,
    # and an ORDER-weighted lead-time average (Σ sums / Σ counts — not an
    # average of the per-supplier averages above).
    all_qty_ordered = sum((Decimal(str(v[1])) for v in lines_by_supplier.values()), _ZERO)
    all_qty_received = sum((Decimal(str(v[2])) for v in lines_by_supplier.values()), _ZERO)
    lead_sum = sum((v[0] for v in lead_by_supplier.values()), Decimal(0))
    lead_count = sum(v[1] for v in lead_by_supplier.values())
    avg_lead = (
        str((lead_sum / Decimal(lead_count)).quantize(Decimal("0.1"))) if lead_count else None
    )

    return {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "summary": {
            "supplier_count": len(po_rows),
            "po_count": sum(int(r[2]) for r in po_rows),
            "total_ordered_value": str(_q2(total_ordered)),
            "total_received_value": str(_q2(total_received)),
            "fill_rate_percent": _pct(all_qty_received, all_qty_ordered),
            "full_supply_rate_percent": _pct(Decimal(full_total), Decimal(delivered_total)),
            "avg_lead_time_days": avg_lead,
            "received_po_count": delivered_total,
        },
        "suppliers": suppliers,
    }


async def supplier_performance_csv(
    session: AsyncSession,
    *,
    branch_id: uuid.UUID,
    date_from: dt.date,
    date_to: dt.date,
) -> str:
    """The supplier performance table as CSV (one row per supplier)."""
    report = await supplier_performance_report(
        session, branch_id=branch_id, date_from=date_from, date_to=date_to
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "supplier",
            "po_count",
            "ordered_value",
            "received_value",
            "fill_rate_percent",
            "full_supply_rate_percent",
            "avg_lead_time_days",
        ]
    )
    suppliers = cast("list[dict[str, object]]", report["suppliers"])
    for row in suppliers:
        writer.writerow(
            [
                # Supplier names are free text created by narrower roles than the
                # reports.export audience — neutralize CSV formula injection.
                csv_safe(cast("str", row["name"])),
                row["po_count"],
                row["ordered_value"],
                row["received_value"],
                row["fill_rate_percent"] or "",
                row["full_supply_rate_percent"] or "",
                row["avg_lead_time_days"] or "",
            ]
        )
    return "\ufeff" + buf.getvalue()


# ============================== P3-M5 — customers ==============================
#
# RFM (simplified, MVP): the three components are reported raw —
#   R (recency)   = days since the customer's LAST completed purchase in range,
#   F (frequency) = completed invoice count in range,
#   M (monetary)  = net spend = Σ invoices.subtotal − Σ returns.subtotal
#                   (credit notes carry the original invoice's customer_id, so
#                   refunds net against the right customer).
# No arbitrary scoring buckets — the dashboard sorts by M and shows R/F/M.
# Per-customer purchase-history drill-down already exists (customer_service.
# customer_history on the /customers screen) and is deliberately NOT re-derived.


async def customer_analytics_report(
    session: AsyncSession,
    *,
    branch_id: uuid.UUID,
    date_from: dt.date,
    date_to: dt.date,
    top_limit: int = 10,
) -> dict[str, object]:
    """A branch's customer purchasing analytics over an inclusive local-day range.

    Walk-in sales (invoice.customer_id IS NULL) have no customer to attribute
    them to and are excluded. Loyalty balances are the customers table's
    current derived balance (the ledger is the truth — a point-in-time read,
    NOT range-scoped, matching how the /customers screen displays it).
    """
    _validate(date_from, date_to, "day")
    p: dict[str, object] = {
        "b": branch_id,
        "f": date_from,
        "t_excl": date_to + dt.timedelta(days=1),
    }

    # 1) Sales per customer (completed invoices only — same convention as the
    #    sales/P&L reports). last_purchase/recency are computed IN SQL (the
    #    ::date cast uses the session TimeZone — the same local-midnight
    #    convention as date_trunc above), never mixed with Python's clock.
    sold_rows = (await session.execute(text("""
                SELECT i.customer_id,
                       COUNT(DISTINCT i.id)   AS invoice_count,
                       COALESCE(SUM(i.subtotal), 0) AS gross_spend,
                       (MAX(i.created_at))::date AS last_purchase,
                       CURRENT_DATE - (MAX(i.created_at))::date AS recency_days
                FROM invoices i
                WHERE i.branch_id = :b AND NOT i.is_deleted AND i.status = 'completed'
                  AND i.customer_id IS NOT NULL
                  AND i.created_at >= :f AND i.created_at < :t_excl
                GROUP BY i.customer_id
                """).bindparams(**p))).all()

    # 2) Refunds per customer (credit notes carry the invoice's customer_id).
    refund_rows = (await session.execute(text("""
                SELECT r.customer_id,
                       COUNT(*)                     AS refund_count,
                       COALESCE(SUM(r.subtotal), 0) AS refunded
                FROM returns r
                WHERE r.branch_id = :b AND NOT r.is_deleted AND r.customer_id IS NOT NULL
                  AND r.created_at >= :f AND r.created_at < :t_excl
                GROUP BY r.customer_id
                """).bindparams(**p))).all()
    refunds_by_customer = {str(r[0]): (int(r[1]), Decimal(str(r[2]))) for r in refund_rows}
    sold_by_customer = {
        str(r[0]): (int(r[1]), Decimal(str(r[2])), cast("dt.date", r[3]), int(r[4]))
        for r in sold_rows
    }

    # 3) Customer identity + current loyalty balance.
    customer_ids = set(sold_by_customer) | set(refunds_by_customer)
    customers: dict[str, tuple[object, object, object]] = {}
    if customer_ids:
        id_rows = (await session.execute(text("""
                    SELECT id, name, phone, loyalty_points FROM customers
                    WHERE id = ANY(:ids)
                    """).bindparams(ids=list(customer_ids)))).all()
        customers = {str(r[0]): (r[1], r[2], r[3]) for r in id_rows}

    rows: list[dict[str, object]] = []
    for key in customer_ids:
        identity = customers.get(key, (None, None, 0))
        invoice_count, gross, last_purchase, recency = sold_by_customer.get(
            key, (0, _ZERO, None, None)
        )
        refund_count, refunded = refunds_by_customer.get(key, (0, _ZERO))
        net = gross - refunded
        rows.append(
            {
                "customer_id": key,
                "name": identity[0],
                "phone": identity[1],
                "invoice_count": invoice_count,
                "refund_count": refund_count,
                "gross_spend": str(_q2(gross)),
                "refunds_total": str(_q2(refunded)),
                "net_spend": str(_q2(net)),
                "last_purchase": None if last_purchase is None else last_purchase.isoformat(),
                "recency_days": recency,
                "loyalty_points": cast("int", identity[2]),
            }
        )
    rows.sort(
        key=lambda row: (Decimal(str(row["net_spend"])), str(row["last_purchase"] or "")),
        reverse=True,
    )
    if top_limit > 0:
        rows = rows[:top_limit]

    gross_total = sum((v[1] for v in sold_by_customer.values()), _ZERO)
    refunds_total = sum((v[1] for v in refunds_by_customer.values()), _ZERO)
    net_total = gross_total - refunds_total
    customer_count = len(customer_ids)
    avg_spend = str(_q2(net_total / Decimal(customer_count))) if customer_count else None

    return {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "summary": {
            "customer_count": customer_count,
            "invoice_count": sum(int(r[1]) for r in sold_rows),
            "refund_count": sum(v[0] for v in refunds_by_customer.values()),
            "gross_spend": str(_q2(gross_total)),
            "refunds_total": str(_q2(refunds_total)),
            "net_spend": str(_q2(net_total)),
            "avg_spend_per_customer": avg_spend,
        },
        "customers": rows,
    }


async def customer_analytics_csv(
    session: AsyncSession,
    *,
    branch_id: uuid.UUID,
    date_from: dt.date,
    date_to: dt.date,
) -> str:
    """The customer analytics rows as CSV (one row per customer, top by spend).

    top_limit=0 means UNLIMITED here (the same convention
    supplier_performance_report documents for its CSV path) — the router's
    Query(ge=1) keeps API callers from ever sending 0, so this is a service/CSV
    concern only.
    """
    report = await customer_analytics_report(
        session,
        branch_id=branch_id,
        date_from=date_from,
        date_to=date_to,
        top_limit=0,
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "customer",
            "phone",
            "invoice_count",
            "net_spend",
            "last_purchase",
            "recency_days",
            "loyalty_points",
        ]
    )
    rows = cast("list[dict[str, object]]", report["customers"])
    for row in rows:
        writer.writerow(
            [
                # Free text (any cashier can create customers; a manager exports)
                # and phones legitimately start with '+' (international format) —
                # both must be neutralized against CSV formula interpretation.
                csv_safe(cast("str", row["name"])),
                csv_safe(cast("str | None", row["phone"])),
                row["invoice_count"],
                row["net_spend"],
                row["last_purchase"],
                row["recency_days"],
                row["loyalty_points"],
            ]
        )
    return "\ufeff" + buf.getvalue()
