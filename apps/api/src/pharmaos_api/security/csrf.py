"""CSRF protection — double-submit cookie pattern (CLAUDE.md: httpOnly cookies + CSRF tokens).

The session cookies are httpOnly; a separate NON-httpOnly CSRF cookie is issued
at login. Mutating requests must echo it in the X-CSRF-Token header; the values
must match (an attacker's cross-site request cannot read the cookie).
"""

import secrets

from fastapi import Request

from pharmaos_api.config import get_settings
from pharmaos_api.errors import ApiError, ErrorCode

CSRF_HEADER = "X-CSRF-Token"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def enforce_csrf(request: Request) -> None:
    """Dependency for cookie-authenticated mutating endpoints."""
    if request.method in _SAFE_METHODS:
        return
    cookie = request.cookies.get(get_settings().csrf_cookie_name)
    header = request.headers.get(CSRF_HEADER)
    if not cookie or not header or not secrets.compare_digest(cookie, header):
        raise ApiError(ErrorCode.CSRF_FAILED, 403)
