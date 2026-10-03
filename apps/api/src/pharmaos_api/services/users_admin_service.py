"""User & role management (P1-M3).

Guarded by the `settings.users` permission (super_admin per CLAUDE.md). All
mutations audit where the AUDITED_OPERATIONS registry defines an event
(user.created / user.role_changed / user.deactivated) and invalidate the
target's sessions where security requires it:

- password reset  -> token_version bump (CLAUDE.md: session invalidation on
  password change)
- deactivation    -> token_version bump (log the user out; get_current_user
  also rejects inactive users immediately)
- role change     -> token_version bump so the JWT role claim is re-issued
  (backend authorization already reads the live DB role, but this keeps the
  session consistent and forces a clean re-auth on privilege change)

Self-lockout guards: an actor cannot deactivate or change the role of their own
account (prevents the last super_admin from locking themselves out). This is
defensive correctness, not a product feature.
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.audit import AuditAction
from pharmaos_api.errors import ApiError, ErrorCode
from pharmaos_api.models import Role, User
from pharmaos_api.security.passwords import hash_password, validate_password_policy
from pharmaos_api.services import audit_service
from pharmaos_api.services.user_service import encrypt_phone

MAX_PAGE_SIZE = 100


async def _role_by_code(session: AsyncSession, role_code: str) -> Role:
    role = (
        await session.execute(
            select(Role).where(Role.code == role_code, Role.is_deleted.is_(False))
        )
    ).scalar_one_or_none()
    if role is None:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Unknown role.")
    return role


async def list_users(
    session: AsyncSession, *, skip: int = 0, limit: int = 50
) -> tuple[list[User], int]:
    """Paginated (<=100) list of non-deleted users + total count."""
    capped = min(max(limit, 1), MAX_PAGE_SIZE)
    total = (
        await session.execute(select(func.count(User.id)).where(User.is_deleted.is_(False)))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(User)
                .where(User.is_deleted.is_(False))
                .order_by(User.created_at)
                .offset(max(skip, 0))
                .limit(capped)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


async def get_user(session: AsyncSession, user_id: uuid.UUID) -> User:
    user = (
        await session.execute(select(User).where(User.id == user_id, User.is_deleted.is_(False)))
    ).scalar_one_or_none()
    if user is None:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 404, message="User not found.")
    return user


async def create_user(
    session: AsyncSession,
    *,
    actor: User,
    username: str,
    full_name: str,
    password: str,
    role_code: str,
    phone: str | None = None,
) -> User:
    violations = validate_password_policy(password)
    if violations:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED, 422, message="Password policy.", details=violations
        )

    role = await _role_by_code(session, role_code)

    existing = (
        await session.execute(select(User.id).where(User.username == username))
    ).scalar_one_or_none()
    if existing is not None:
        raise ApiError(ErrorCode.USERNAME_TAKEN, 409)

    user = User(
        username=username,
        full_name=full_name,
        password_hash=hash_password(password),
        role_id=role.id,
        phone_encrypted=encrypt_phone(phone),
        created_by=actor.id,
        updated_by=actor.id,
    )
    session.add(user)
    await session.flush()

    await audit_service.record(
        session,
        AuditAction.USER_CREATED,
        actor=actor,
        entity_type="user",
        entity_id=user.id,
        metadata={"username": username, "role": role_code},
    )
    await session.commit()
    await session.refresh(user)
    return user


async def update_user_profile(
    session: AsyncSession,
    *,
    actor: User,
    user: User,
    full_name: str | None = None,
    phone: str | None = None,
    set_phone: bool = False,
) -> User:
    """Update non-sensitive profile fields. `set_phone=True` applies `phone`
    (including clearing to None); otherwise phone is left untouched."""
    if full_name is not None:
        user.full_name = full_name
    if set_phone:
        user.phone_encrypted = encrypt_phone(phone)
    user.updated_by = actor.id
    await session.commit()
    await session.refresh(user)
    return user


async def change_user_role(
    session: AsyncSession, *, actor: User, user: User, role_code: str
) -> User:
    if user.id == actor.id:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="You cannot change your own role.")
    role = await _role_by_code(session, role_code)
    if user.role_id == role.id:
        return user  # no-op, no audit churn

    user.role_id = role.id
    user.updated_by = actor.id
    user.token_version = user.token_version + 1  # re-issue JWT role claim
    await audit_service.record(
        session,
        AuditAction.USER_ROLE_CHANGED,
        actor=actor,
        entity_type="user",
        entity_id=user.id,
        metadata={"role": role_code},
    )
    await session.commit()
    await session.refresh(user)
    return user


async def set_user_active(session: AsyncSession, *, actor: User, user: User, active: bool) -> User:
    if not active and user.id == actor.id:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED, 422, message="You cannot deactivate your own account."
        )
    if user.is_active == active:
        return user  # no-op

    user.is_active = active
    user.updated_by = actor.id
    if not active:
        # Log the user out everywhere; user.deactivated is an audited operation.
        user.token_version = user.token_version + 1
        await audit_service.record(
            session,
            AuditAction.USER_DEACTIVATED,
            actor=actor,
            entity_type="user",
            entity_id=user.id,
            metadata={"username": user.username},
        )
    await session.commit()
    await session.refresh(user)
    return user


async def reset_user_password(
    session: AsyncSession, *, actor: User, user: User, new_password: str
) -> None:
    violations = validate_password_policy(new_password)
    if violations:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED, 422, message="Password policy.", details=violations
        )
    user.password_hash = hash_password(new_password)
    user.updated_by = actor.id
    user.token_version = user.token_version + 1  # invalidate sessions (CLAUDE.md)
    await session.commit()
