"""Fail-closed gate for compliance submissions (installer decisions 8/6).

The local ETA/EDA simulators return accepted/reported=True — legitimate in
dev/test, FORBIDDEN in production: a scheduled compliance-drain must never
let simulated acceptances stand in for real ETA e-receipts or EDA
track-and-trace reports (drug tracing has a legal go-live deadline).

Production + simulator ⇒ raise; the caller leaves queued rows untouched
(pending), so nothing is ever silently "compliant" that isn't.
"""

from pharmaos_api.config import get_settings


class ComplianceFailClosedError(RuntimeError):
    """Production refuses to process compliance rows through a simulator."""


def ensure_allowed(*, simulator: bool) -> None:
    """Raise when a simulator would be used to mark rows in production."""
    if simulator and get_settings().is_production:
        raise ComplianceFailClosedError(
            "production refuses the local compliance simulator (fail-closed) — "
            "configure real ETA/EDA credentials; queued rows are left untouched"
        )
