"""ETA e-receipt PORT/ADAPTER (P2-M10).

The compliance gate (Phase-2 plan): the queue/build/sign/submit logic is fully
implemented and tested against a LOCAL SIMULATOR. A real ETA submission needs
credentials that are not provisioned here (digital taxpayer profile, per-POS
client_id/secret, and an X.509 seal certificate), so:

  - The DEFAULT adapter is the local simulator: it "signs" with a deterministic
    stand-in seal (a base64 SHA-256 of the canonical payload — NOT a real X.509
    signature) and "accepts" the submission, returning a synthetic UUID + QR.
  - The real HTTP adapter is a stub that raises until credentials are wired.
  - get_adapter() returns the simulator unless real credentials are configured.

⚠️ No real ETA acceptance is ever claimed without real credentials. A row
accepted here is SIMULATOR-accepted; production readiness is "pending
credentials" (docs/pilot-checklist + the compliance acceptance criterion).
"""

import base64
import hashlib
import json
import uuid
from dataclasses import dataclass

from pharmaos_api.config import get_settings


@dataclass(frozen=True)
class EtaSubmissionResult:
    eta_uuid: str
    qr_data: str
    accepted: bool


class EtaAdapterError(Exception):
    """Raised when a submission cannot be built/signed/sent (worker marks failed)."""


def _canonical(payload: dict[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class LocalSimulatorEtaAdapter:
    """Offline stand-in for the ETA e-receipt API used in dev/test."""

    is_simulator = True

    def sign(self, payload: dict[str, object]) -> str:
        # Deterministic stand-in for the X.509 seal (NOT a real signature).
        digest = hashlib.sha256(_canonical(payload).encode("utf-8")).digest()
        return "SIM-SEAL:" + base64.b64encode(digest).decode("ascii")

    def submit(self, *, signed_payload: str, payload: dict[str, object]) -> EtaSubmissionResult:
        # A real adapter would OAuth2 + POST /receiptsubmissions here. The
        # simulator derives a stable UUID from the invoice so re-submits are
        # idempotent-looking, and builds the verification QR the receipt prints.
        eta_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, "eta:" + str(payload.get("invoice_id", ""))))
        qr_data = f"https://eta.gov.eg/receipts/{eta_uuid}"
        return EtaSubmissionResult(eta_uuid=eta_uuid, qr_data=qr_data, accepted=True)


class HttpEtaAdapter:
    """Real ETA adapter — not wired until credentials exist (pending)."""

    is_simulator = False

    def sign(
        self, payload: dict[str, object]
    ) -> str:  # pragma: no cover - not reachable without creds
        raise EtaAdapterError("ETA credentials (X.509 seal) are not configured — pending.")

    def submit(
        self, *, signed_payload: str, payload: dict[str, object]
    ) -> EtaSubmissionResult:  # pragma: no cover
        raise EtaAdapterError("ETA credentials (client_id/secret) are not configured — pending.")


def adapter_is_simulated() -> bool:
    """True unless real ETA credentials are configured (they are not, here)."""
    settings = get_settings()
    return not bool(getattr(settings, "eta_client_id", None))


def get_adapter() -> LocalSimulatorEtaAdapter | HttpEtaAdapter:
    return LocalSimulatorEtaAdapter() if adapter_is_simulated() else HttpEtaAdapter()
