"""The immutable runtime license state + the atomic swap (P4 §4).

The gate middleware (M2) reads this via a module-attribute lookup —
`runtime.get_state()` — on EVERY request, so tests can monkeypatch the provider
function (P4 §4: never `from … import` the function or capture it at middleware
construction). The state is memory-only and carries NO key material, ever.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from pharmaos_api.licensing.payload import LicensePayloadV1

GRACE_DAYS = 30
WARNING_DAYS = 14

# Frozen status vocabulary (P4 §3) — mirrored by the license_state CHECK.
STATUS_UNLICENSED = "unlicensed"
STATUS_ACTIVE = "active"
STATUS_GRACE = "grace"
STATUS_READ_ONLY = "read_only"
STATUS_CLOCK_ERROR = "clock_error"
STATUS_KEY_LOST = "key_lost"
STATUS_ERROR = "error"
STATUS_TAMPER = "tamper"

# Every status past active/grace blocks mutations; the distinction is the
# message + the recovery path shown to the user.
BLOCKING_STATUSES = frozenset(
    {
        STATUS_UNLICENSED,
        STATUS_READ_ONLY,
        STATUS_CLOCK_ERROR,
        STATUS_KEY_LOST,
        STATUS_ERROR,
        STATUS_TAMPER,
    }
)


@dataclass(frozen=True, slots=True)
class LicenseRuntimeState:
    """Immutable per-boot/activation snapshot — memory-only reads for the gate."""

    status: str
    hwid: str | None = None
    license_id: str | None = None
    customer_name: str | None = None
    valid_until: datetime | None = None
    grace_until: datetime | None = None
    days_left: int | None = None
    needs_activation: bool = False
    warning: bool = False  # active but inside the 14-day renewal window
    tamper_flag: bool = False
    high_water_utc: datetime | None = None


_module_state: LicenseRuntimeState | None = None


def get_state() -> LicenseRuntimeState | None:
    """Module-attribute lookup point for the gate (P4 §4). None ⇒ the gate
    DENIES (fail-closed) — the state simply has not been computed yet."""
    return _module_state


def set_state(state: LicenseRuntimeState | None) -> None:
    """Atomic swap — a single module-global reference reassignment."""
    global _module_state
    _module_state = state


def build_runtime_state(
    *,
    status: str,
    payload: LicensePayloadV1 | None,
    now_utc: datetime,
    high_water_utc: datetime | None,
    hwid: str | None = None,
    tamper_flag: bool = False,
) -> LicenseRuntimeState:
    effective_now = _effective_now(now_utc, high_water_utc)
    valid_until = payload.valid_until if payload is not None else None
    grace_until = valid_until + timedelta(days=GRACE_DAYS) if valid_until is not None else None
    days_left = (
        (valid_until - effective_now).days
        if valid_until is not None and payload is not None
        else None
    )
    warning = bool(
        payload is not None
        and valid_until is not None
        and effective_now >= valid_until - timedelta(days=WARNING_DAYS)
    )
    return LicenseRuntimeState(
        status=status,
        hwid=hwid or (payload.hwid if payload is not None else None),
        license_id=payload.license_id if payload is not None else None,
        customer_name=payload.customer if payload is not None else None,
        valid_until=valid_until,
        grace_until=grace_until,
        days_left=days_left,
        needs_activation=payload is None or status == STATUS_UNLICENSED,
        warning=warning,
        tamper_flag=tamper_flag,
        high_water_utc=high_water_utc,
    )


def effective_now(now_utc: datetime, high_water_utc: datetime | None) -> datetime:
    """The gate's expiry clock (P4 §4): the high-water mark never lets a rolled
    back clock un-expire a license between periodic reconciliations."""
    return _effective_now(now_utc, high_water_utc)


def _effective_now(now_utc: datetime, high_water_utc: datetime | None) -> datetime:
    now = now_utc.astimezone(UTC)
    if high_water_utc is None:
        return now
    return max(now, high_water_utc.astimezone(UTC))


def public_view(state: LicenseRuntimeState) -> dict[str, Any]:
    """GET /license/status body (M2) — the frozen nullability contract (P4 §6):
    unlicensed → valid_until/days_left null, needs_activation true; no
    signature/customer/license_id ever leaves the authenticated surface."""
    return {
        "status": state.status,
        "hwid": state.hwid,
        "valid_until": (
            None
            if state.valid_until is None
            else state.valid_until.astimezone(UTC).isoformat().replace("+00:00", "Z")
        ),
        "days_left": state.days_left,
        "needs_activation": state.needs_activation,
    }
