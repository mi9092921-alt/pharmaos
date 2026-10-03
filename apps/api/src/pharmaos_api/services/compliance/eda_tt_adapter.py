"""EDA Track & Trace PORT/ADAPTER (P2-M11).

Same compliance gate as ETA: the queue/build/report logic is complete and
tested against a LOCAL SIMULATOR. Real EDA reporting needs facility
registration + the approved reporting channel (portal/app/provider — decree
475/2025), which is not provisioned here, so:

  - The DEFAULT adapter is the local simulator: it "reports" an event and
    returns a synthetic report reference.
  - The real HTTP adapter is a stub that raises until the channel is wired.
  - get_adapter() returns the simulator unless a real endpoint is configured.

⚠️ No real EDA acceptance is claimed without a real channel (pending).
"""

import uuid
from dataclasses import dataclass

from pharmaos_api.config import get_settings


@dataclass(frozen=True)
class TtReportResult:
    report_id: str
    reported: bool


class EdaTtAdapterError(Exception):
    """Raised when an event cannot be reported (worker marks it failed)."""


class LocalSimulatorEdaTtAdapter:
    """Offline stand-in for the EDA track & trace reporting channel."""

    is_simulator = True

    def report(self, *, payload: dict[str, object]) -> TtReportResult:
        # A real adapter would POST the event to the national system here.
        seed = f"tt:{payload.get('event_type', '')}:{payload.get('serial_number', '')}"
        report_id = str(uuid.uuid5(uuid.NAMESPACE_URL, seed))
        return TtReportResult(report_id=report_id, reported=True)


class HttpEdaTtAdapter:
    """Real EDA adapter — not wired until the reporting channel exists (pending)."""

    is_simulator = False

    def report(self, *, payload: dict[str, object]) -> TtReportResult:  # pragma: no cover
        raise EdaTtAdapterError("EDA track & trace channel is not configured — pending.")


def adapter_is_simulated() -> bool:
    """True unless a real EDA endpoint is configured (it is not, here)."""
    settings = get_settings()
    return not bool(getattr(settings, "eda_tt_api_base", None))


def get_adapter() -> LocalSimulatorEdaTtAdapter | HttpEdaTtAdapter:
    return LocalSimulatorEdaTtAdapter() if adapter_is_simulated() else HttpEdaTtAdapter()
