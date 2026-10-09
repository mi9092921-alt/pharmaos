"""License endpoints: status / activate (P4 §6).

These endpoints are in the unauthenticated, unlicensed allowlist so a freshly
installed device can inspect its state and complete initial activation.
"""

from typing import Any

from fastapi import APIRouter, Request

from pharmaos_api.db import get_session_factory
from pharmaos_api.errors import ApiError, ErrorCode, success_envelope
from pharmaos_api.licensing import runtime
from pharmaos_api.licensing.activation import activate_license
from pharmaos_api.licensing.errors import LICENSE_HTTP_STATUS, LicensingError
from pharmaos_api.licensing.external_stores import default_providers
from pharmaos_api.licensing.hwid import compute_hwid
from pharmaos_api.licensing.vendor_key import vendor_accepted_kids, vendor_public_key

router = APIRouter(prefix="/api/v1/license", tags=["license"])


def _lic_error_to_api(exc: LicensingError) -> ApiError:
    status_code = LICENSE_HTTP_STATUS.get(exc.code, 400)
    details = {"reason": exc.reason} if exc.reason is not None else None
    msg = str(exc)
    message = None if msg.startswith("Licensing error:") else msg
    return ApiError(exc.code, status_code, message=message, details=details)


@router.get("/status")
async def license_status() -> dict[str, Any]:
    """Inspect current device license status (P4 §6)."""
    state = runtime.get_state()
    if state is None:
        raise ApiError(ErrorCode.LICENSE_STATE_ERROR, 503)
    return success_envelope(runtime.public_view(state))


@router.post("/activate")
async def license_activate(request: Request) -> dict[str, Any]:
    """Activate or update license using a raw .license container (P4 §6)."""
    body = await request.body()
    if not body:
        raise ApiError(ErrorCode.LICENSE_INVALID_SIGNATURE, 400)

    try:
        public_key = vendor_public_key()
    except Exception as exc:
        raise ApiError(ErrorCode.LICENSE_STATE_ERROR, 503) from exc

    try:
        result = await activate_license(
            get_session_factory(),
            license_file=body,
            public_key=public_key,
            accepted_kids=vendor_accepted_kids(),
            external_providers=default_providers(),
            hwid=compute_hwid(),
        )
    except LicensingError as exc:
        raise _lic_error_to_api(exc) from exc

    return success_envelope(runtime.public_view(result.runtime))
