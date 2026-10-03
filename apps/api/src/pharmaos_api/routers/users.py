"""User & role management endpoints (P1-M3).

Every endpoint requires the `settings.users` permission (super_admin per the
CLAUDE.md matrix). Mutations enforce CSRF. Responses use the unified envelope
and never leak the password hash or the raw encrypted phone.
"""

import uuid

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.db import get_session
from pharmaos_api.deps import get_current_user, require_permission
from pharmaos_api.errors import success_envelope
from pharmaos_api.models import User
from pharmaos_api.security.csrf import enforce_csrf
from pharmaos_api.services import users_admin_service as svc
from pharmaos_api.services.user_service import safe_get_user_phone

router = APIRouter(prefix="/api/v1/users", tags=["users"])

# Every route in this router is super_admin-only (settings.users).
_guard = Depends(require_permission("settings.users"))

_USERNAME = Field(min_length=3, max_length=50, pattern=r"^[A-Za-z0-9._-]+$")
_PASSWORD = Field(min_length=8, max_length=128)
_FULL_NAME = Field(min_length=1, max_length=255)


class CreateUserIn(BaseModel):
    username: str = _USERNAME
    full_name: str = _FULL_NAME
    password: str = _PASSWORD
    role_code: str = Field(min_length=1, max_length=50)
    phone: str | None = Field(default=None, max_length=32)


class UpdateProfileIn(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=255)
    phone: str | None = Field(default=None, max_length=32)
    set_phone: bool = False


class ChangeRoleIn(BaseModel):
    role_code: str = Field(min_length=1, max_length=50)


class SetActiveIn(BaseModel):
    active: bool


class ResetPasswordIn(BaseModel):
    new_password: str = _PASSWORD


def _serialize(user: User) -> dict[str, object]:
    """Safe public representation — decrypted phone for an authorized (super_admin) read."""
    return {
        "id": str(user.id),
        "username": user.username,
        "full_name": user.full_name,
        "role": user.role.code if user.role is not None else None,
        "phone": safe_get_user_phone(user),
        "is_active": user.is_active,
        "created_at": user.created_at.isoformat(),
    }


@router.get("")
async def list_users(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=svc.MAX_PAGE_SIZE),
    session: AsyncSession = Depends(get_session),
    _: None = _guard,
) -> dict[str, object]:
    users, total = await svc.list_users(session, skip=skip, limit=limit)
    return success_envelope(
        [_serialize(u) for u in users],
        meta={"page": skip // limit + 1, "total": total, "per_page": limit},
    )


@router.post("")
async def create_user(
    body: CreateUserIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _guard,
) -> dict[str, object]:
    enforce_csrf(request)
    user = await svc.create_user(
        session,
        actor=actor,
        username=body.username,
        full_name=body.full_name,
        password=body.password,
        role_code=body.role_code,
        phone=body.phone,
    )
    return success_envelope(_serialize(user))


@router.get("/{user_id}")
async def get_user(
    user_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: None = _guard,
) -> dict[str, object]:
    user = await svc.get_user(session, user_id)
    return success_envelope(_serialize(user))


@router.patch("/{user_id}")
async def update_profile(
    user_id: uuid.UUID,
    body: UpdateProfileIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _guard,
) -> dict[str, object]:
    enforce_csrf(request)
    user = await svc.get_user(session, user_id)
    user = await svc.update_user_profile(
        session,
        actor=actor,
        user=user,
        full_name=body.full_name,
        phone=body.phone,
        set_phone=body.set_phone,
    )
    return success_envelope(_serialize(user))


@router.post("/{user_id}/role")
async def change_role(
    user_id: uuid.UUID,
    body: ChangeRoleIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _guard,
) -> dict[str, object]:
    enforce_csrf(request)
    user = await svc.get_user(session, user_id)
    user = await svc.change_user_role(session, actor=actor, user=user, role_code=body.role_code)
    return success_envelope(_serialize(user))


@router.post("/{user_id}/active")
async def set_active(
    user_id: uuid.UUID,
    body: SetActiveIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _guard,
) -> dict[str, object]:
    enforce_csrf(request)
    user = await svc.get_user(session, user_id)
    user = await svc.set_user_active(session, actor=actor, user=user, active=body.active)
    return success_envelope(_serialize(user))


@router.post("/{user_id}/reset-password")
async def reset_password(
    user_id: uuid.UUID,
    body: ResetPasswordIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(get_current_user),
    _: None = _guard,
) -> dict[str, object]:
    enforce_csrf(request)
    user = await svc.get_user(session, user_id)
    await svc.reset_user_password(session, actor=actor, user=user, new_password=body.new_password)
    return success_envelope({"reset": True})
