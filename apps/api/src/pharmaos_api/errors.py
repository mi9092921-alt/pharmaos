"""Error-code registry (mirror of packages/shared/errors.ts) + unified API errors.

CLAUDE.md rules:
- The API returns a STABLE error code; the UI translates it per user language.
- No hardcoded Arabic strings in the API layer (bilingual system).
- `message` is a fallback in the request language; `details` is debugging-only
  and must never carry sensitive data or stack traces.
"""

from typing import Any


class ErrorCode:
    """Append-only registry — codes are never modified (CLAUDE.md)."""

    STOCK_INSUFFICIENT = "E-STK-001"
    BATCH_EXPIRED = "E-STK-002"
    VALIDATION_FAILED = "E-VAL-001"
    UNAUTHORIZED = "E-AUTH-001"
    PERMISSION_DENIED = "E-AUTH-002"
    ACCOUNT_LOCKED = "E-AUTH-003"
    CSRF_FAILED = "E-AUTH-004"
    RATE_LIMITED = "E-AUTH-005"
    USERNAME_TAKEN = "E-USR-001"
    BARCODE_TAKEN = "E-CAT-001"
    ERECEIPT_REJECTED = "E-ETA-001"
    TT_REPORT_FAILED = "E-TT-001"
    PACK_SERIAL_DUPLICATE = "E-TT-002"
    PACK_SERIAL_MISMATCH = "E-TT-003"
    SYNC_CONFLICT = "E-SYN-001"
    PRINTER_NOT_CONFIGURED = "E-PRN-001"
    PRINTER_UNREACHABLE = "E-PRN-002"
    PAPER_NOT_THERMAL = "E-PRN-003"
    SESSION_ALREADY_OPEN = "E-CSH-001"
    SESSION_NOT_OPEN = "E-CSH-002"
    PRESCRIPTION_REQUIRED = "E-RX-001"
    PRESCRIPTION_EXCEEDED = "E-RX-002"
    PRESCRIPTION_INVALID = "E-RX-003"
    NOT_FOUND = "E-GEN-001"
    # P4-M1 (licensing) — statuses/HTTP mapping live in the gate (M2) and
    # docs/phase4-execution-plan-licensing.md §7. Same commit closes the E-SYS-001
    # precedent (used as a literal in main.py, defined in TS only until now).
    UNEXPECTED = "E-SYS-001"
    LICENSE_REQUIRED = "E-LIC-001"
    LICENSE_READ_ONLY = "E-LIC-002"
    LICENSE_INVALID_SIGNATURE = "E-LIC-003"
    LICENSE_DEVICE_MISMATCH = "E-LIC-004"
    LICENSE_EXPIRED = "E-LIC-005"
    LICENSE_TAMPER_DETECTED = "E-LIC-006"
    LICENSE_STATE_ERROR = "E-LIC-007"
    LICENSE_KEY_LOST = "E-LIC-008"


# Fallback messages in English (the neutral request-language fallback; the
# frontend translates codes via i18n — ar is the default locale there).
_FALLBACK_MESSAGES: dict[str, str] = {
    ErrorCode.STOCK_INSUFFICIENT: "Insufficient stock.",
    ErrorCode.BATCH_EXPIRED: "Batch is expired or quarantined.",
    ErrorCode.VALIDATION_FAILED: "Validation failed.",
    ErrorCode.UNAUTHORIZED: "Authentication required.",
    ErrorCode.PERMISSION_DENIED: "Permission denied.",
    ErrorCode.ACCOUNT_LOCKED: "Account temporarily locked after repeated failed attempts.",
    ErrorCode.CSRF_FAILED: "CSRF verification failed.",
    ErrorCode.RATE_LIMITED: "Too many requests. Try again later.",
    ErrorCode.USERNAME_TAKEN: "Username is already taken.",
    ErrorCode.BARCODE_TAKEN: "Barcode is already registered.",
    ErrorCode.ERECEIPT_REJECTED: "E-receipt was rejected.",
    ErrorCode.TT_REPORT_FAILED: "Track & trace report failed.",
    ErrorCode.PACK_SERIAL_DUPLICATE: "Duplicate pack serial (GTIN + serial).",
    ErrorCode.PACK_SERIAL_MISMATCH: "Scanned serial is not from a batch dispensed in this sale.",
    ErrorCode.SYNC_CONFLICT: "Synchronization conflict.",
    ErrorCode.PRINTER_NOT_CONFIGURED: "No receipt printer is configured on this device.",
    ErrorCode.PRINTER_UNREACHABLE: "Could not reach the receipt printer.",
    ErrorCode.PAPER_NOT_THERMAL: "Branch paper size is not 80mm thermal.",
    ErrorCode.SESSION_ALREADY_OPEN: "A cash session is already open for this cashier.",
    ErrorCode.SESSION_NOT_OPEN: "The cash session is not open.",
    ErrorCode.PRESCRIPTION_REQUIRED: "This medication requires a linked prescription.",
    ErrorCode.PRESCRIPTION_EXCEEDED: "Quantity exceeds what remains on the prescription.",
    ErrorCode.PRESCRIPTION_INVALID: "Prescription item does not match this medication.",
    ErrorCode.NOT_FOUND: "The requested resource was not found.",
    ErrorCode.UNEXPECTED: "Unexpected error.",
    ErrorCode.LICENSE_REQUIRED: "License activation required.",
    ErrorCode.LICENSE_READ_ONLY: "License expired — the system is in read-only mode.",
    ErrorCode.LICENSE_INVALID_SIGNATURE: "The license file is invalid.",
    ErrorCode.LICENSE_DEVICE_MISMATCH: "This license is bound to a different device.",
    ErrorCode.LICENSE_EXPIRED: "This license has expired.",
    ErrorCode.LICENSE_TAMPER_DETECTED: "License integrity check failed — contact support.",
    ErrorCode.LICENSE_STATE_ERROR: "License state is temporarily unavailable — retrying.",
    ErrorCode.LICENSE_KEY_LOST: (
        "License key is missing from this device — restore it or contact support."
    ),
}


class ApiError(Exception):
    """Raised by services/routers; converted to the unified envelope by main.py."""

    def __init__(
        self,
        code: str,
        http_status: int,
        message: str | None = None,
        details: Any = None,
    ) -> None:
        self.code = code
        self.http_status = http_status
        self.message = message or _FALLBACK_MESSAGES.get(code, "Unexpected error.")
        self.details = details
        super().__init__(self.message)


def error_envelope(code: str, message: str, details: Any = None) -> dict[str, Any]:
    """Unified ApiResponse error shape (CLAUDE.md)."""
    body: dict[str, Any] = {"success": False, "error": {"code": code, "message": message}}
    if details is not None:
        body["error"]["details"] = details
    return body


def success_envelope(data: Any, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Unified ApiResponse success shape (CLAUDE.md)."""
    body: dict[str, Any] = {"success": True, "data": data}
    if meta is not None:
        body["meta"] = meta
    return body
