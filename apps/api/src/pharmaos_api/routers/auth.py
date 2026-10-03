"""Auth endpoints: login / refresh / logout / me.

Session model (CLAUDE.md): httpOnly cookies + CSRF token. Tokens are also
returned in the body for the Electron main process (localhost HTTP).
"""

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.config import Settings, get_settings
from pharmaos_api.db import get_session
from pharmaos_api.deps import get_current_user
from pharmaos_api.errors import ApiError, ErrorCode, success_envelope
from pharmaos_api.models import User
from pharmaos_api.security.csrf import enforce_csrf, generate_csrf_token
from pharmaos_api.services import auth_service

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1, max_length=128)


def _set_session_cookies(response: Response, s: Settings, access: str, refresh: str) -> str:
    csrf = generate_csrf_token()
    response.set_cookie(
        s.access_cookie_name,
        access,
        max_age=s.access_token_expire_minutes * 60,
        httponly=True,
        samesite="strict",
        secure=s.cookie_secure,
        path="/",
    )
    response.set_cookie(
        s.refresh_cookie_name,
        refresh,
        max_age=s.refresh_token_expire_hours * 3600,
        httponly=True,
        samesite="strict",
        secure=s.cookie_secure,
        path="/",
    )
    # CSRF cookie is intentionally NOT httpOnly (double-submit pattern).
    response.set_cookie(
        s.csrf_cookie_name,
        csrf,
        max_age=s.refresh_token_expire_hours * 3600,
        httponly=False,
        samesite="strict",
        secure=s.cookie_secure,
        path="/",
    )
    return csrf


def _clear_session_cookies(response: Response, s: Settings) -> None:
    for name in (s.access_cookie_name, s.refresh_cookie_name, s.csrf_cookie_name):
        response.delete_cookie(name, path="/")


def _user_payload(user: User) -> dict[str, object]:
    return {
        "id": str(user.id),
        "username": user.username,
        "full_name": user.full_name,
        "role": user.role.code if user.role is not None else None,
    }


@router.post("/login")
async def login(
    body: LoginRequest,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    user = await auth_service.authenticate(session, body.username, body.password)
    access, refresh = auth_service.issue_token_pair(user)
    csrf = _set_session_cookies(response, get_settings(), access, refresh)
    return success_envelope(
        {
            "user": _user_payload(user),
            "access_token": access,
            "refresh_token": refresh,
            "csrf_token": csrf,
        }
    )


@router.post("/refresh")
async def refresh(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    s = get_settings()
    token = request.cookies.get(s.refresh_cookie_name)
    if not token:
        raise ApiError(ErrorCode.UNAUTHORIZED, 401)
    user, access, new_refresh = await auth_service.refresh_tokens(session, token)
    csrf = _set_session_cookies(response, s, access, new_refresh)
    return success_envelope(
        {
            "user": _user_payload(user),
            "access_token": access,
            "refresh_token": new_refresh,
            "csrf_token": csrf,
        }
    )


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    enforce_csrf(request)
    # Bump token_version: every outstanding token for this user becomes invalid.
    await auth_service.invalidate_sessions(session, current_user)
    _clear_session_cookies(response, get_settings())
    return success_envelope({"logged_out": True})


@router.get("/me")
async def me(current_user: User = Depends(get_current_user)) -> dict[str, object]:
    return success_envelope({"user": _user_payload(current_user)})
