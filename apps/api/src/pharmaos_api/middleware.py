"""Security headers + login rate limiting (CLAUDE.md security standards).

Rate limit: login 5/minute per client IP (in-memory sliding window — the local
pharmacy device serves a single POS; the cloud deployment fronts this with its
own gateway limits).
"""

import contextlib
import json
import time
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from pharmaos_api.config import get_settings
from pharmaos_api.errors import ApiError, ErrorCode, error_envelope
from pharmaos_api.licensing import runtime

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'self'",
}

_MAX_LICENSE_BYTES = 64 * 1024  # 64 KiB (P4 §6)


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
    """Sliding-window limiter for sensitive endpoints (login, license activation)."""

    def __init__(self, app: ASGIApp, window_seconds: int = 60) -> None:
        super().__init__(app)
        self._window = window_seconds
        self._hits: dict[str, deque[float]] = {}

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.url.path.rstrip("/")
        if request.method == "POST" and path in (
            "/api/v1/auth/login",
            "/api/v1/license/activate",
        ):
            limit = get_settings().login_rate_limit_per_minute
            client_ip = request.client.host if request.client else "unknown"
            bucket_key = f"{client_ip}:{path}"
            now = time.monotonic()
            bucket = self._hits.setdefault(bucket_key, deque())
            while bucket and now - bucket[0] > self._window:
                bucket.popleft()
            if len(bucket) >= limit:
                msg = (
                    "Too many login attempts."
                    if path == "/api/v1/auth/login"
                    else "Too many license activation attempts."
                )
                return JSONResponse(
                    status_code=429,
                    content=error_envelope(ErrorCode.RATE_LIMITED, msg),
                )
            bucket.append(now)
        return await call_next(request)


class LicenseGateMiddleware:
    """Pure ASGI gate enforcing device license state (P4 §4 + §6).

    Lookup rule: accesses `runtime.get_state()` fresh on every request via
    module-attribute (never captures reference at init).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET").upper()
        raw_path = scope.get("path", "")
        path = raw_path.rstrip("/") if raw_path != "/" else raw_path

        # 1. Always allowed methods
        if method in ("OPTIONS", "HEAD"):
            await self.app(scope, receive, send)
            return

        # 2. Always allowed health check
        if path == "/api/v1/health":
            await self.app(scope, receive, send)
            return

        # 3. Read runtime state (fail-closed if None)
        state = runtime.get_state()
        if state is None:
            await self._send_json(
                send,
                503,
                error_envelope(
                    ErrorCode.LICENSE_STATE_ERROR,
                    "License state is temporarily unavailable — retrying.",
                ),
            )
            return

        status = state.status

        # 4. Locked states (423/503)
        if status == runtime.STATUS_TAMPER:
            await self._send_json(
                send,
                423,
                error_envelope(
                    ErrorCode.LICENSE_TAMPER_DETECTED,
                    "License integrity check failed — contact support.",
                ),
            )
            return

        if status == runtime.STATUS_KEY_LOST:
            await self._send_json(
                send,
                423,
                error_envelope(
                    ErrorCode.LICENSE_KEY_LOST,
                    "License key is missing from this device — restore it or contact support.",
                ),
            )
            return

        if status == runtime.STATUS_CLOCK_ERROR:
            await self._send_json(
                send,
                423,
                error_envelope(
                    ErrorCode.LICENSE_TAMPER_DETECTED,
                    "System clock error detected — check system date/time.",
                ),
            )
            return

        if status == runtime.STATUS_ERROR:
            await self._send_json(
                send,
                503,
                error_envelope(
                    ErrorCode.LICENSE_STATE_ERROR,
                    "License state is temporarily unavailable — retrying.",
                ),
            )
            return

        # 5. Unlicensed state
        if status == runtime.STATUS_UNLICENSED:
            if (path == "/api/v1/license/status" and method == "GET") or (
                path == "/api/v1/license/activate" and method == "POST"
            ):
                pass
            else:
                await self._send_json(
                    send,
                    403,
                    error_envelope(
                        ErrorCode.LICENSE_REQUIRED,
                        "License activation required.",
                    ),
                )
                return

        # 6. Read-only state (expired past grace)
        elif status == runtime.STATUS_READ_ONLY:
            if method in ("POST", "PUT", "PATCH", "DELETE") and not (
                path == "/api/v1/license/activate" and method == "POST"
            ):
                await self._send_json(
                    send,
                    403,
                    error_envelope(
                        ErrorCode.LICENSE_READ_ONLY,
                        "License expired — the system is in read-only mode.",
                    ),
                )
                return

        # 7. Active / Grace (or dynamically expired between periodic runs)
        elif status in (runtime.STATUS_ACTIVE, runtime.STATUS_GRACE):
            if state.valid_until is not None:
                now_utc = datetime.now(UTC)
                eff_now = runtime.effective_now(now_utc, state.high_water_utc)
                if (
                    state.grace_until is not None
                    and eff_now >= state.grace_until
                    and method in ("POST", "PUT", "PATCH", "DELETE")
                    and not (path == "/api/v1/license/activate" and method == "POST")
                ):
                    await self._send_json(
                        send,
                        403,
                        error_envelope(
                            ErrorCode.LICENSE_READ_ONLY,
                            "License expired — the system is in read-only mode.",
                        ),
                    )
                    return
        else:
            # Any unhandled blocking state fails closed
            await self._send_json(
                send,
                403,
                error_envelope(
                    ErrorCode.LICENSE_REQUIRED,
                    "License activation required.",
                ),
            )
            return

        # 8. Enforce 64 KiB payload limit on license activate
        if path == "/api/v1/license/activate" and method == "POST":
            content_length: int | None = None
            for name, val in scope.get("headers", []):
                if name.lower() == b"content-length":
                    with contextlib.suppress(ValueError):
                        content_length = int(val.decode("latin1"))
                    break

            if content_length is not None and content_length > _MAX_LICENSE_BYTES:
                await self._send_json(
                    send,
                    413,
                    error_envelope(
                        ErrorCode.VALIDATION_FAILED,
                        "License file exceeds maximum size (64 KiB).",
                    ),
                )
                return

            total_bytes = 0

            async def limited_receive() -> Message:
                nonlocal total_bytes
                msg = await receive()
                if msg["type"] == "http.request":
                    body_chunk = msg.get("body", b"")
                    total_bytes += len(body_chunk)
                    if total_bytes > _MAX_LICENSE_BYTES:
                        raise ApiError(
                            ErrorCode.VALIDATION_FAILED,
                            413,
                            "License file exceeds maximum size (64 KiB).",
                        )
                return msg

            try:
                await self.app(scope, limited_receive, send)
            except ApiError as exc:
                await self._send_json(
                    send,
                    exc.http_status,
                    error_envelope(exc.code, exc.message, exc.details),
                )
            return

        await self.app(scope, receive, send)

    async def _send_json(self, send: Send, status_code: int, data: dict[str, Any]) -> None:
        body = json.dumps(data).encode("utf-8")
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
        ]
        await send(
            {
                "type": "http.response.start",
                "status": status_code,
                "headers": headers,
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": body,
            }
        )
