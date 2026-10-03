"""Analytics reports (Phase 3).

P3-M1 — sales reports (daily / monthly / annual) + CSV export.

All routes are READ-ONLY (GET), so there is no CSRF or audit surface here. The
sales report is gated by `reports.sales`; the CSV download additionally requires
`reports.export`. Aggregation is entirely server-side SQL (decision D2) in
`reporting_service`.
"""

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.db import get_session
from pharmaos_api.deps import require_permission
from pharmaos_api.errors import success_envelope
from pharmaos_api.services import reporting_service as svc

router = APIRouter(prefix="/api/v1", tags=["reports"])

_reports_sales = Depends(require_permission("reports.sales"))
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
