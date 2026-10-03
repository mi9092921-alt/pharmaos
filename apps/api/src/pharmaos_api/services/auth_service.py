"""Authentication service: login (with lockout), token refresh, session invalidation.

Lockout policy (CLAUDE.md): MAX_LOGIN_ATTEMPTS=5, then the account is locked
for 15 minutes. Counters reset on successful login.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.config import get_settings
from pharmaos_api.errors import ApiError, ErrorCode
from pharmaos_api.models import User
from pharmaos_api.security.jwt import create_token, decode_token
from pharmaos_api.security.passwords import verify_password


async def _get_active_user_by_username(session: AsyncSession, username: str) -> User | None:
    stmt = select(User).where(
        User.username == username,
        User.is_deleted.is_(False),
        User.is_active.is_(True),
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_active_user_by_id(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    stmt = select(User).where(
        User.id == user_id,
        User.is_deleted.is_(False),
        User.is_active.is_(True),
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def authenticate(session: AsyncSession, username: str, password: str) -> User:
    """Verify credentials, enforcing the lockout policy. Raises ApiError on failure."""
    s = get_settings()
    now = datetime.now(UTC)
    user = await _get_active_user_by_username(session, username)

    if user is None:
        # Same error as a wrong password — never reveal whether the account exists.
        raise ApiError(ErrorCode.UNAUTHORIZED, 401)

    if user.locked_until is not None and user.locked_until > now:
        raise ApiError(ErrorCode.ACCOUNT_LOCKED, 423)

    if not verify_password(user.password_hash, password):
        user.failed_login_attempts = user.failed_login_attempts + 1
        if user.failed_login_attempts >= s.max_login_attempts:
            user.locked_until = now + timedelta(minutes=s.lockout_minutes)
            user.failed_login_attempts = 0
            await session.commit()
            raise ApiError(ErrorCode.ACCOUNT_LOCKED, 423)
        await session.commit()
        raise ApiError(ErrorCode.UNAUTHORIZED, 401)

    # Success — reset counters.
    user.failed_login_attempts = 0
    user.locked_until = None
    await session.commit()
    return user


def issue_token_pair(user: User) -> tuple[str, str]:
    role_code = user.role.code if user.role is not None else None
    access = create_token(
        user_id=user.id,
        token_version=user.token_version,
        token_type="access",  # noqa: S106 — token TYPE discriminator, not a credential
        role_code=role_code,
    )
    refresh = create_token(
        user_id=user.id,
        token_version=user.token_version,
        token_type="refresh",  # noqa: S106 — token TYPE discriminator, not a credential
        role_code=role_code,
    )
    return access, refresh


async def refresh_tokens(session: AsyncSession, refresh_token: str) -> tuple[User, str, str]:
    """Validate a refresh token (incl. token_version) and rotate the pair."""
    payload = decode_token(refresh_token, expected_type="refresh")
    user = await get_active_user_by_id(session, uuid.UUID(payload["sub"]))
    if user is None or payload.get("token_version") != user.token_version:
        raise ApiError(ErrorCode.UNAUTHORIZED, 401)
    access, refresh = issue_token_pair(user)
    return user, access, refresh


async def invalidate_sessions(session: AsyncSession, user: User) -> None:
    """Bump token_version — all outstanding tokens become invalid.

    Used on password change (CLAUDE.md: session invalidation) and explicit
    'log out everywhere'.
    """
    user.token_version = user.token_version + 1
    await session.commit()
