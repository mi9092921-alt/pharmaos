"""Smart alerts (P3-M6, ratified D4/D6/D7).

GET  /alerts            — branch list, status filter (alerts.view)
GET  /alerts/summary    — live counts by severity (dashboard banner, alerts.view)
POST /alerts/evaluate   — idempotent rule evaluation (alerts.manage + CSRF) —
                          the same engine runs at boot and via the CLI
                          `alerts-evaluate` (ratified D6, expiry-sweep pattern)
POST /alerts/{id}/acknowledge — active -> acknowledged (alerts.manage + CSRF)
POST /alerts/{id}/resolve     — -> resolved          (alerts.manage + CSRF)

Acknowledging/resolving is deliberately NOT audited (ratified D7 — the closed
audit log gains no alert actions). Evaluation is idempotent and never blocks or
breaks a primary flow.
"""

import uuid

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.db import get_session
from pharmaos_api.deps import get_current_user, require_permission
from pharmaos_api.errors import success_envelope
from pharmaos_api.models import User
from pharmaos_api.security.csrf import enforce_csrf
from pharmaos_api.services import alerts_service as svc

router = APIRouter(prefix="/api/v1", tags=["alerts"])

_alerts_view = Depends(require_permission("alerts.view"))
_alerts_manage = Depends(require_permission("alerts.manage"))

_STATUS_PATTERN = "^(active|acknowledged|resolved|all)$"


@router.get("/alerts")
async def list_alerts(
    branch_id: uuid.UUID = Query(),
    status: str = Query(default="active", pattern=_STATUS_PATTERN),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    _: None = _alerts_view,
) -> dict[str, object]:
    data = await svc.list_alerts(
        session, branch_id=branch_id, status=status, skip=skip, limit=limit
    )
    return success_envelope(data)


@router.get("/alerts/summary")
async def alert_summary(
    branch_id: uuid.UUID = Query(),
    session: AsyncSession = Depends(get_session),
    _: None = _alerts_view,
) -> dict[str, object]:
    data = await svc.alert_summary(session, branch_id=branch_id)
    return success_envelope(data)


@router.post("/alerts/evaluate")
async def evaluate_alerts(
    request: Request,
    branch_id: uuid.UUID | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    _manage: None = _alerts_manage,
) -> dict[str, object]:
    """Run ALERT_RULES evaluation idempotently — one branch or all (D6)."""
    enforce_csrf(request)
    if branch_id is None:
        return success_envelope(await svc.evaluate_all(session))
    return success_envelope(await svc.evaluate_branch(session, branch_id))


@router.post("/alerts/{alert_id}/acknowledge")
async def acknowledge_alert(
    alert_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
    _: None = _alerts_manage,
) -> dict[str, object]:
    enforce_csrf(request)
    data = await svc.acknowledge(session, alert_id=alert_id, actor_id=user.id)
    return success_envelope(data)


@router.post("/alerts/{alert_id}/resolve")
async def resolve_alert(
    alert_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    _: None = _alerts_manage,
) -> dict[str, object]:
    enforce_csrf(request)
    data = await svc.resolve(session, alert_id=alert_id)
    return success_envelope(data)
