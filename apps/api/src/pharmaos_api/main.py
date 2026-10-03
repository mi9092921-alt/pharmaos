"""Application factory.

- Unified ApiResponse envelope for success and errors (CLAUDE.md).
- No stack traces or sensitive data ever reach the client (forbidden action #6).
- The local API binds to 127.0.0.1 only (see run()).
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from pharmaos_api.errors import ApiError, ErrorCode, error_envelope, success_envelope
from pharmaos_api.middleware import LoginRateLimitMiddleware, SecurityHeadersMiddleware
from pharmaos_api.routers import (
    auth,
    cashier,
    catalog,
    compliance,
    config,
    customers,
    finance,
    inventory,
    pos,
    prescriptions,
    purchases,
    reports,
    returns,
    users,
)

logger = logging.getLogger(__name__)


async def _boot_inventory_maintenance() -> None:
    """Verify the derived inventory cache at boot and self-heal any drift.

    CLAUDE.md invariant: cached_quantity == SUM(active batches) — rebuilt
    periodically and AT BOOT. This runs once on startup so a device coming back
    online (e.g. after the M12 PostgreSQL-restart scenario) always serves a
    correct cache. It must never block or crash startup.
    """
    from pharmaos_api.config import get_settings

    if get_settings().pharmaos_env == "test":
        return  # tests manage their own data; no boot maintenance
    try:
        from pharmaos_api.db import get_session_factory
        from pharmaos_api.services import inventory_service

        async with get_session_factory()() as session:
            summary = await inventory_service.boot_check_and_heal(session)
        healed = {bid: s for bid, s in summary.items() if s.get("healed")}
        if healed:
            logger.warning("inventory cache drift healed at boot: %s", healed)
        else:
            logger.info("inventory cache verified at boot (%d branch(es))", len(summary))
    except Exception:  # boot maintenance is best-effort — never stop the API
        logger.exception("inventory boot maintenance skipped (non-fatal)")


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    await _boot_inventory_maintenance()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="PharmaOS API",
        version="1.1.0",
        docs_url=None,
        redoc_url=None,
        lifespan=_lifespan,
    )

    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(LoginRateLimitMiddleware)

    app.include_router(auth.router)
    app.include_router(pos.router)
    app.include_router(users.router)
    app.include_router(config.router)
    app.include_router(catalog.router)
    app.include_router(inventory.router)
    app.include_router(cashier.router)
    app.include_router(purchases.router)
    app.include_router(customers.router)
    app.include_router(returns.router)
    app.include_router(prescriptions.router)
    app.include_router(finance.router)
    app.include_router(compliance.router)
    app.include_router(reports.router)

    @app.exception_handler(ApiError)
    async def api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content=error_envelope(exc.code, exc.message, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Field locations only — never echo submitted values back.
        details = [
            {"loc": [str(part) for part in err.get("loc", [])], "type": err.get("type", "")}
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=error_envelope(ErrorCode.VALIDATION_FAILED, "Validation failed.", details),
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(_request: Request, exc: Exception) -> JSONResponse:
        # Real error goes to the log ONLY (forbidden action #6: no stack traces to clients).
        logger.exception("Unhandled error", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content=error_envelope("E-SYS-001", "Unexpected error."),
        )

    @app.get("/api/v1/health")
    async def health() -> dict[str, object]:
        return success_envelope({"status": "ok"})

    return app


app = create_app()


def run() -> None:
    """Entry point — binds to 127.0.0.1 only (CLAUDE.md local security rule)."""
    import uvicorn

    from pharmaos_api.config import get_settings

    s = get_settings()
    uvicorn.run("pharmaos_api.main:app", host=s.api_host, port=s.api_port)
