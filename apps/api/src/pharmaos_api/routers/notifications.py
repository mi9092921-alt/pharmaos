"""Notifications (P3-M7) — the delivery surface.

GET  /notifications                — the viewer's list (notifications.view)
GET  /notifications/unread-count   — the bell counter      (notifications.view)
POST /notifications/{id}/read      — mark one VISIBLE row read (view + CSRF)
POST /notifications/read-all       — mark all VISIBLE rows read (view + CSRF)

Rows are created by the alerts engine (created-from-alert) and via
notification_service.notify — no user-facing creation endpoint in Phase 3.
Marking read is self-service on VISIBLE rows only (own or broadcast); the
broader notifications.manage tier exists for M8+ admin surfaces and future
fan-out management, and is deliberately not required for reading your own.
"""

import uuid

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.db import get_session
from pharmaos_api.deps import get_current_user, require_permission
from pharmaos_api.errors import success_envelope
from pharmaos_api.models import User
from pharmaos_api.security.csrf import enforce_csrf
from pharmaos_api.services import notification_service as svc

router = APIRouter(prefix="/api/v1", tags=["notifications"])

_notifications_view = Depends(require_permission("notifications.view"))


@router.get("/notifications")
async def list_notifications(
    branch_id: uuid.UUID = Query(),
    unread_only: bool = Query(default=False),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
    _: None = _notifications_view,
) -> dict[str, object]:
    data = await svc.list_notifications(
        session,
        branch_id=branch_id,
        user_id=user.id,
        unread_only=unread_only,
        skip=skip,
        limit=limit,
    )
    return success_envelope(data)


@router.get("/notifications/unread-count")
async def unread_count(
    branch_id: uuid.UUID = Query(),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
    _: None = _notifications_view,
) -> dict[str, object]:
    data = await svc.unread_count(session, branch_id=branch_id, user_id=user.id)
    return success_envelope(data)


@router.post("/notifications/{notification_id}/read")
async def mark_read(
    notification_id: uuid.UUID,
    request: Request,
    branch_id: uuid.UUID = Query(),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
    _: None = _notifications_view,
) -> dict[str, object]:
    enforce_csrf(request)
    data = await svc.mark_read(
        session, notification_id=notification_id, branch_id=branch_id, user_id=user.id
    )
    return success_envelope(data)


@router.post("/notifications/read-all")
async def mark_all_read(
    request: Request,
    branch_id: uuid.UUID = Query(),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
    _: None = _notifications_view,
) -> dict[str, object]:
    enforce_csrf(request)
    data = await svc.mark_all_read(session, branch_id=branch_id, user_id=user.id)
    return success_envelope(data)
