"""Application factory.

- Unified ApiResponse envelope for success and errors (CLAUDE.md).
- No stack traces or sensitive data ever reach the client (forbidden action #6).
- The local API binds to 127.0.0.1 only (see run()).
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from pharmaos_api.errors import ApiError, ErrorCode, error_envelope, success_envelope
from pharmaos_api.middleware import (
    LicenseGateMiddleware,
    LoginRateLimitMiddleware,
    SecurityHeadersMiddleware,
)
from pharmaos_api.routers import (
    alerts,
    auth,
    cashier,
    catalog,
    compliance,
    config,
    customers,
    finance,
    inventory,
    notifications,
    pos,
    prescriptions,
    purchases,
    reports,
    returns,
    users,
)
from pharmaos_api.routers import (
    license as license_router,
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


async def _boot_alert_evaluation() -> None:
    """Evaluate ALERT_RULES once at startup (P3-M6, ratified D6 — boot / CLI /
    on-demand, no scheduler). Runs AFTER inventory maintenance so a drift alert
    means the healer itself could not restore the invariant. Best-effort: an
    alerts failure must never stop the API (plan convention #8)."""
    from pharmaos_api.config import get_settings

    if get_settings().pharmaos_env == "test":
        return  # tests manage their own data and call the service directly
    try:
        from pharmaos_api.db import get_session_factory
        from pharmaos_api.services import alerts_service

        async with get_session_factory()() as session:
            summary = await alerts_service.evaluate_all(session)
        results = cast("list[dict[str, object]]", summary["results"])
        created = sum(cast("int", r["created"]) for r in results)
        logger.info(
            "alerts evaluated at boot: %s branch(es), %s created",
            summary["branches"],
            created,
        )
    except Exception:  # boot maintenance is best-effort — never stop the API
        logger.exception("alert evaluation at boot skipped (non-fatal)")


async def _boot_email_drain() -> None:
    """Drain the queued email channel through the configured provider once at
    startup (P3-M7, ratified D5/D6 — after alert evaluation so rows it just
    queued are the first candidates). The NoopEmailProvider default keeps
    every row pending; a failure must never stop the API (plan convention #8)."""
    from pharmaos_api.config import get_settings

    if get_settings().pharmaos_env == "test":
        return  # tests call the drain directly
    try:
        from pharmaos_api.db import get_session_factory
        from pharmaos_api.services import notification_service

        async with get_session_factory()() as session:
            out = await notification_service.dispatch_pending_email(session)
        if out["attempted"] or out["skipped"]:
            logger.info("email drain at boot: %s", out)
    except Exception:  # boot maintenance is best-effort — never stop the API
        logger.exception("email drain at boot skipped (non-fatal)")


async def _run_boot_migrations() -> None:
    """Run database migrations in-process at boot (decision 7).

    Ensures no separate CLI process is needed.
    """
    from pharmaos_api.config import get_settings

    if get_settings().pharmaos_env == "test":
        return
    try:
        from pharmaos_api.migrations_runner import (
            _load_migrations,
            _load_seeds,
            default_migrations_dir,
            default_seeds_dir,
            run_migrations_async,
        )

        s = get_settings()
        report = await run_migrations_async(
            s.resolved_database_url,
            migrations=_load_migrations(default_migrations_dir()),
            seeds=_load_seeds(default_seeds_dir()),
        )
        applied = report.get("applied")
        if applied:
            logger.info("boot migrations applied: %s", applied)
    except Exception:
        logger.exception("boot migrations failed (non-fatal)")


async def _run_background_boot_maintenance() -> None:
    """Non-blocking background boot maintenance.

    Performs inventory healing, alert evaluation, and email drain.
    """
    await _boot_inventory_maintenance()
    await _boot_alert_evaluation()
    await _boot_email_drain()


async def _boot_license_evaluation() -> None:
    """Boot: evaluate license state (fail-closed) (P4 §4)."""
    from pharmaos_api.db import get_session_factory
    from pharmaos_api.licensing import runtime
    from pharmaos_api.licensing.external_stores import default_providers
    from pharmaos_api.licensing.hwid import compute_hwid
    from pharmaos_api.licensing.state import evaluate_license
    from pharmaos_api.licensing.vendor_key import vendor_public_key

    try:
        public_key = vendor_public_key()
    except Exception:
        # vendor_key missing or broken -> error state (not tamper)
        runtime.set_state(runtime.LicenseRuntimeState(status=runtime.STATUS_ERROR))
        return

    try:
        await evaluate_license(
            get_session_factory(),
            public_key=public_key,
            external_providers=default_providers(),
            hwid=compute_hwid(),
        )
    except Exception:
        logger.exception("boot license evaluation failed")
        runtime.set_state(runtime.LicenseRuntimeState(status=runtime.STATUS_ERROR))


async def _license_periodic_task() -> None:
    """Periodically re-evaluate license state every 15 minutes (P4 §4).

    Clean cancellation on shutdown; cycle failure sets STATUS_ERROR (never silent pass).
    """
    interval_seconds = 900  # 15 minutes
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await _boot_license_evaluation()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("license periodic check failed")
            from pharmaos_api.licensing import runtime

            current = runtime.get_state()
            if current is not None and current.status not in (
                runtime.STATUS_TAMPER,
                runtime.STATUS_KEY_LOST,
            ):
                runtime.set_state(runtime.LicenseRuntimeState(status=runtime.STATUS_ERROR))


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    await _run_boot_migrations()

    # P4-M2: evaluate license at boot (fail-closed)
    await _boot_license_evaluation()

    task = asyncio.create_task(_run_background_boot_maintenance())

    # P4-M2: periodic license evaluation task
    scheduler_task: asyncio.Task[None] | None = None
    if getattr(_app.state, "license_scheduler", True):
        scheduler_task = asyncio.create_task(_license_periodic_task())

    try:
        yield
    finally:
        task.cancel()
        if scheduler_task is not None:
            scheduler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await scheduler_task


def create_app(license_scheduler: bool = True) -> FastAPI:
    app = FastAPI(
        title="PharmaOS API",
        version="1.1.0",
        docs_url=None,
        redoc_url=None,
        # No OpenAPI schema on the device either (P3-M8 OWASP gate: the local
        # API surface should be exactly the endpoints the app calls — nothing
        # self-describing for anything else reading 127.0.0.1).
        openapi_url=None,
        lifespan=_lifespan,
    )
    app.state.license_scheduler = license_scheduler

    # Middleware LIFO order (outermost executed first):
    # SecurityHeaders (1st) -> LicenseGate (2nd) -> LoginRateLimit (3rd) -> app
    app.add_middleware(LoginRateLimitMiddleware)
    app.add_middleware(LicenseGateMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)

    app.include_router(license_router.router)
    app.include_router(auth.router)
    app.include_router(pos.router)
    app.include_router(users.router)
    app.include_router(config.router)
    app.include_router(catalog.router)
    app.include_router(inventory.router)
    app.include_router(alerts.router)
    app.include_router(notifications.router)
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

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        _request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        # Routing-level rejections (404 unknown path, 405 wrong method) join the
        # unified envelope too — the client's t(`errors.${code}`) path never
        # meets a bare {"detail": ...} body (P3-M8, E-GEN-001). Starlette's
        # computed headers (e.g. 405's RFC-9110 Allow) are forwarded untouched.
        if exc.status_code == 404:
            message = "The requested resource was not found."
        else:
            message = f"Request rejected with status {exc.status_code}."
        return JSONResponse(
            status_code=exc.status_code,
            content=error_envelope(ErrorCode.NOT_FOUND, message),
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(_request: Request, exc: Exception) -> JSONResponse:
        # Real error goes to the log ONLY (forbidden action #6: no stack traces to clients).
        logger.exception("Unhandled error", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content=error_envelope("E-SYS-001", "Unexpected error."),
        )

    @app.api_route("/api/v1/health", methods=["GET", "HEAD"])
    async def health() -> dict[str, object]:
        return success_envelope({"status": "ok"})

    @app.get("/api/v1/system/setup-status")
    async def system_setup_status() -> dict[str, object]:
        from sqlalchemy import func, select

        from pharmaos_api.db import get_session_factory
        from pharmaos_api.models import Branch, User
        from pharmaos_api.services import installation_state

        async with get_session_factory()() as session:
            users = (await session.execute(select(func.count()).select_from(User))).scalar_one()
            branches = (
                await session.execute(select(func.count()).select_from(Branch))
            ).scalar_one()
            state = await installation_state.get_all(session)
        return success_envelope(
            {
                "users": users,
                "branches": branches,
                "setup_complete": state.get("setup_complete") == "1",
                "last_completed_step": state.get("last_completed_step", "none"),
            }
        )

    @app.post("/api/v1/system/setup-complete")
    async def system_setup_complete() -> dict[str, object]:
        from pharmaos_api.db import get_session_factory
        from pharmaos_api.services import installation_state

        async with get_session_factory()() as session:
            await installation_state.set_values(
                session,
                {"setup_complete": "1", "last_completed_step": "complete"},
            )
        return success_envelope({"status": "complete"})

    return app


app = create_app()


def run() -> None:
    """Entry point — binds to 127.0.0.1 only (CLAUDE.md local security rule)."""
    import uvicorn

    from pharmaos_api.config import get_settings

    s = get_settings()
    uvicorn.run("pharmaos_api.main:app", host=s.api_host, port=s.api_port)
