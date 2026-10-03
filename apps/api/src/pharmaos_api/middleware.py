"""Security headers + login rate limiting (CLAUDE.md security standards).

Rate limit: login 5/minute per client IP (in-memory sliding window — the local
pharmacy device serves a single POS; the cloud deployment fronts this with its
own gateway limits).
"""

import time
from collections import deque
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from pharmaos_api.config import get_settings
from pharmaos_api.errors import ErrorCode, error_envelope

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'self'",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        for header, value in _SECURITY_HEADERS.items():
            response.headers[header] = value
        # HSTS is cloud-only (local device runs on localhost HTTP by design).
        if get_settings().cookie_secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response


class LoginRateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window limiter for the login endpoint."""

    def __init__(self, app: ASGIApp, window_seconds: int = 60) -> None:
        super().__init__(app)
        self._window = window_seconds
        self._hits: dict[str, deque[float]] = {}

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path == "/api/v1/auth/login" and request.method == "POST":
            limit = get_settings().login_rate_limit_per_minute
            client_ip = request.client.host if request.client else "unknown"
            now = time.monotonic()
            bucket = self._hits.setdefault(client_ip, deque())
            while bucket and now - bucket[0] > self._window:
                bucket.popleft()
            if len(bucket) >= limit:
                return JSONResponse(
                    status_code=429,
                    content=error_envelope(ErrorCode.RATE_LIMITED, "Too many login attempts."),
                )
            bucket.append(now)
        return await call_next(request)
