"""FastAPI dependencies: current user + permission enforcement.

CLAUDE.md mandates a permission check on EVERY endpoint:
    _: None = Depends(require_permission("inventory.view"))
Backend checks are authoritative — frontend guards are UX only.
"""

import uuid
from collections.abc import Awaitable, Callable

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.config import get_settings
from pharmaos_api.db import get_session
from pharmaos_api.errors import ApiError, ErrorCode
from pharmaos_api.models import Permission, RolePermission, User
from pharmaos_api.security.jwt import decode_token
from pharmaos_api.services.auth_service import get_active_user_by_id


def _extract_access_token(request: Request) -> str:
    """Access token from the httpOnly cookie (browser/Electron) or Bearer header (services)."""
    token = request.cookies.get(get_settings().access_cookie_name)
    if token:
        return token
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth.removeprefix("Bearer ")
    raise ApiError(ErrorCode.UNAUTHORIZED, 401)


async def get_current_user(request: Request, session: AsyncSession = Depends(get_session)) -> User:
    payload = decode_token(_extract_access_token(request), expected_type="access")
    user = await get_active_user_by_id(session, uuid.UUID(payload["sub"]))
    if user is None or payload.get("token_version") != user.token_version:
        raise ApiError(ErrorCode.UNAUTHORIZED, 401)
    return user


async def user_has_permission(session: AsyncSession, user: User, permission_code: str) -> bool:
    """Programmatic twin of require_permission — for CONDITIONAL behavior the
    guard pattern can't express (e.g. the shortage list redacts customer PII
    for roles below inventory.edit). Same DB source of truth: the role's
    grants seeded from packages/shared/permissions.ts."""
    if user.role_id is None:
        return False
    stmt = (
        select(RolePermission.id)
        .join(Permission, Permission.id == RolePermission.permission_id)
        .where(
            RolePermission.role_id == user.role_id,
            RolePermission.is_deleted.is_(False),
            Permission.code == permission_code,
            Permission.is_deleted.is_(False),
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


def require_permission(permission_code: str) -> Callable[..., Awaitable[None]]:
    """Endpoint guard: the current user's role must hold the permission (from the DB,
    which is seeded from packages/shared/permissions.ts — code is the source)."""

    async def _check(
        current_user: User = Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> None:
        if not await user_has_permission(session, current_user, permission_code):
            raise ApiError(ErrorCode.PERMISSION_DENIED, 403)

    return _check


def require_any_permission(*permission_codes: str) -> Callable[..., Awaitable[None]]:
    """Endpoint guard for ALTERNATIVE permissions (any one suffices) — e.g. the
    shortage create accepts inventory.edit (staff) OR sales.create (a cashier
    recording a customer request from the POS)."""

    async def _check(
        current_user: User = Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> None:
        if current_user.role_id is None:
            raise ApiError(ErrorCode.PERMISSION_DENIED, 403)
        stmt = (
            select(RolePermission.id)
            .join(Permission, Permission.id == RolePermission.permission_id)
            .where(
                RolePermission.role_id == current_user.role_id,
                RolePermission.is_deleted.is_(False),
                Permission.code.in_(permission_codes),
                Permission.is_deleted.is_(False),
            )
            .limit(1)
        )
        if (await session.execute(stmt)).scalar_one_or_none() is None:
            raise ApiError(ErrorCode.PERMISSION_DENIED, 403)

    return _check
