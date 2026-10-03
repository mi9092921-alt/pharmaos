"""Analytics reports (Phase 3).

P3-M1 — sales reports (daily / monthly / annual) + CSV export.
P3-M2 — inventory reports: stock level (+ CSV), valuation, movement analysis
         (by type + fast/slow movers). Gated by `reports.inventory` (super_admin,
         branch_manager, pharmacist per CLAUDE.md — a wider tier than
         `reports.sales`, since pharmacists review stock decisions day to day).
P3-M3 — expiry & waste analytics: near-expiry buckets (reused verbatim from
         expiry_alerts) + waste value swept in a date range + a forward
         weekly trend + expired/locked capital (reused from
         batch_status_report). Same reports.inventory gate as M2 — this is
         the same inventory-analytics audience, not a new permission domain.
P3-M4 — profit & loss: net revenue (invoices.subtotal net of VAT, minus
         credit notes), COGS from the batch's own purchase_price via
         invoice_items.batch_id (decision D3), gross margin per
         item/category/period, and operating net after expenses. Gated by
         `reports.financial` (super_admin, branch_manager) — margins are
         commercially sensitive pricing data, the same tier as reports.sales.
P3-M5 — supplier performance (PO activity/value, approval→receipt lead time,
         fill & full-supply rates) gated by `reports.financial` — purchasing
         values are money data. Customer analytics (top spenders, simplified
         RFM, loyalty balances) gated by `reports.sales` — it derives entirely
         from the same invoices the sales reports already cover.

All routes are READ-ONLY (GET), so there is no CSRF or audit surface here.
Aggregation is entirely server-side SQL (decision D2), in `reporting_service`
(sales + P&L + M5) and `inventory_service` (inventory — reuses the Phase-1/2
read models per the P3-M2/M3 plan rather than re-deriving them).
"""

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.db import get_session
from pharmaos_api.deps import require_permission
from pharmaos_api.errors import success_envelope
from pharmaos_api.services import inventory_service as inventory_svc
from pharmaos_api.services import reporting_service as svc

router = APIRouter(prefix="/api/v1", tags=["reports"])

_reports_sales = Depends(require_permission("reports.sales"))
_reports_inventory = Depends(require_permission("reports.inventory"))
_reports_financial = Depends(require_permission("reports.financial"))
_reports_export = Depends(require_permission("reports.export"))

_GRANULARITY_PATTERN = "^(day|month|year)$"


@router.get("/reports/sales")
async def sales_report(
    branch_id: uuid.UUID = Query(),
    date_from: dt.date = Query(),
    date_to: dt.date = Query(),
    granularity: str = Query(default="day", pattern=_GRANULARITY_PATTERN),
    top_limit: int = Query(default=10, ge=1, le=50),
    session: AsyncSession = Depends(get_session),
    _: None = _reports_sales,
) -> dict[str, object]:
    data = await svc.sales_report(
        session,
        branch_id=branch_id,
        date_from=date_from,
        date_to=date_to,
        granularity=granularity,
        top_limit=top_limit,
    )
    return success_envelope(data)


@router.get("/reports/sales/export")
async def sales_report_export(
    branch_id: uuid.UUID = Query(),
    date_from: dt.date = Query(),
    date_to: dt.date = Query(),
    granularity: str = Query(default="day", pattern=_GRANULARITY_PATTERN),
    session: AsyncSession = Depends(get_session),
    _: None = _reports_export,
) -> Response:
    csv_text = await svc.sales_report_csv(
        session,
        branch_id=branch_id,
        date_from=date_from,
        date_to=date_to,
        granularity=granularity,
    )
    filename = f"sales_{date_from.isoformat()}_{date_to.isoformat()}_{granularity}.csv"
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/reports/profit-loss")
async def profit_loss_report(
    branch_id: uuid.UUID = Query(),
    date_from: dt.date = Query(),
    date_to: dt.date = Query(),
    granularity: str = Query(default="day", pattern=_GRANULARITY_PATTERN),
    top_limit: int = Query(default=10, ge=1, le=50),
    session: AsyncSession = Depends(get_session),
    _: None = _reports_financial,
) -> dict[str, object]:
    data = await svc.profit_loss_report(
        session,
        branch_id=branch_id,
        date_from=date_from,
        date_to=date_to,
        granularity=granularity,
        top_limit=top_limit,
    )
    return success_envelope(data)


@router.get("/reports/profit-loss/export")
async def profit_loss_report_export(
    branch_id: uuid.UUID = Query(),
    date_from: dt.date = Query(),
    date_to: dt.date = Query(),
    granularity: str = Query(default="day", pattern=_GRANULARITY_PATTERN),
    session: AsyncSession = Depends(get_session),
    _: None = _reports_export,
) -> Response:
    csv_text = await svc.profit_loss_csv(
        session,
        branch_id=branch_id,
        date_from=date_from,
        date_to=date_to,
        granularity=granularity,
    )
    filename = f"profit_loss_{date_from.isoformat()}_{date_to.isoformat()}_{granularity}.csv"
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/reports/suppliers/performance")
async def supplier_performance_report(
    branch_id: uuid.UUID = Query(),
    date_from: dt.date = Query(),
    date_to: dt.date = Query(),
    limit: int = Query(default=50, ge=1, le=inventory_svc.MAX_PAGE_SIZE),
    session: AsyncSession = Depends(get_session),
    _: None = _reports_financial,
) -> dict[str, object]:
    data = await svc.supplier_performance_report(
        session, branch_id=branch_id, date_from=date_from, date_to=date_to, limit=limit
    )
    return success_envelope(data)


@router.get("/reports/suppliers/performance/export")
async def supplier_performance_report_export(
    branch_id: uuid.UUID = Query(),
    date_from: dt.date = Query(),
    date_to: dt.date = Query(),
    session: AsyncSession = Depends(get_session),
    _: None = _reports_export,
) -> Response:
    csv_text = await svc.supplier_performance_csv(
        session, branch_id=branch_id, date_from=date_from, date_to=date_to
    )
    filename = f"supplier_performance_{date_from.isoformat()}_{date_to.isoformat()}.csv"
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/reports/customers/analytics")
async def customer_analytics_report(
    branch_id: uuid.UUID = Query(),
    date_from: dt.date = Query(),
    date_to: dt.date = Query(),
    top_limit: int = Query(default=10, ge=1, le=50),
    session: AsyncSession = Depends(get_session),
    _: None = _reports_sales,
) -> dict[str, object]:
    data = await svc.customer_analytics_report(
        session,
        branch_id=branch_id,
        date_from=date_from,
        date_to=date_to,
        top_limit=top_limit,
    )
    return success_envelope(data)


@router.get("/reports/customers/analytics/export")
async def customer_analytics_report_export(
    branch_id: uuid.UUID = Query(),
    date_from: dt.date = Query(),
    date_to: dt.date = Query(),
    session: AsyncSession = Depends(get_session),
    _: None = _reports_export,
) -> Response:
    csv_text = await svc.customer_analytics_csv(
        session, branch_id=branch_id, date_from=date_from, date_to=date_to
    )
    filename = f"customer_analytics_{date_from.isoformat()}_{date_to.isoformat()}.csv"
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/reports/inventory/stock-level")
async def stock_level_report(
    branch_id: uuid.UUID = Query(),
    low_stock_only: bool = Query(default=False),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=inventory_svc.MAX_PAGE_SIZE),
    session: AsyncSession = Depends(get_session),
    _: None = _reports_inventory,
) -> dict[str, object]:
    data = await inventory_svc.stock_level_report(
        session, branch_id=branch_id, low_stock_only=low_stock_only, skip=skip, limit=limit
    )
    return success_envelope(data)


@router.get("/reports/inventory/stock-level/export")
async def stock_level_report_export(
    branch_id: uuid.UUID = Query(),
    low_stock_only: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
    _: None = _reports_export,
) -> Response:
    csv_text = await inventory_svc.stock_level_report_csv(
        session, branch_id=branch_id, low_stock_only=low_stock_only
    )
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="stock_level.csv"'},
    )


@router.get("/reports/inventory/valuation")
async def inventory_valuation_report(
    branch_id: uuid.UUID = Query(),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=inventory_svc.MAX_PAGE_SIZE),
    session: AsyncSession = Depends(get_session),
    _: None = _reports_inventory,
) -> dict[str, object]:
    data = await inventory_svc.inventory_valuation_report(
        session, branch_id=branch_id, skip=skip, limit=limit
    )
    return success_envelope(data)


@router.get("/reports/inventory/movement")
async def inventory_movement_report(
    branch_id: uuid.UUID = Query(),
    date_from: dt.date = Query(),
    date_to: dt.date = Query(),
    mover_limit: int = Query(default=10, ge=1, le=50),
    session: AsyncSession = Depends(get_session),
    _: None = _reports_inventory,
) -> dict[str, object]:
    data = await inventory_svc.movement_report(
        session,
        branch_id=branch_id,
        date_from=date_from,
        date_to=date_to,
        mover_limit=mover_limit,
    )
    return success_envelope(data)


@router.get("/reports/inventory/expiry-waste")
async def inventory_expiry_waste_report(
    branch_id: uuid.UUID = Query(),
    date_from: dt.date = Query(),
    date_to: dt.date = Query(),
    session: AsyncSession = Depends(get_session),
    _: None = _reports_inventory,
) -> dict[str, object]:
    data = await inventory_svc.expiry_waste_report(
        session, branch_id=branch_id, date_from=date_from, date_to=date_to
    )
    return success_envelope(data)
