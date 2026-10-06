"""Typed licensing errors + the frozen E-LIC → HTTP status map (P4 §7).

The client branches on `error.code` (+ `details.reason`), NEVER on the HTTP
status alone (E-LIC-001/002 share 403 with E-AUTH-002 but are not permission
failures). Routers/gate (M2) convert these via LICENSE_HTTP_STATUS.
"""

from pharmaos_api.errors import ErrorCode

LICENSE_HTTP_STATUS: dict[str, int] = {
    ErrorCode.LICENSE_REQUIRED: 403,
    ErrorCode.LICENSE_READ_ONLY: 403,
    ErrorCode.LICENSE_INVALID_SIGNATURE: 400,
    ErrorCode.LICENSE_DEVICE_MISMATCH: 409,
    ErrorCode.LICENSE_EXPIRED: 409,
    ErrorCode.LICENSE_TAMPER_DETECTED: 423,
    ErrorCode.LICENSE_STATE_ERROR: 503,
    ErrorCode.LICENSE_KEY_LOST: 423,
}


class LicensingError(Exception):
    """Raised by the licensing core; converted to the unified envelope by the
    gate/router. `reason` feeds error.details.reason (P4 §7 — e.g.
    `issued_at_in_future` must not read as a corrupt file to the client)."""

    def __init__(self, code: str, reason: str | None = None, message: str | None = None) -> None:
        self.code = code
        self.reason = reason
        super().__init__(message or f"Licensing error: {code}")
