"""Clock-event schema + the chain hash (P4 §1 — LOCK-1, the literal formula).

    message     = b"pharmaos.clock.chain.v1\\n" || u64_be(seq) || prev_hash_bytes
                  || canonical_event_bytes
    entry_hash  = HMAC-SHA256(LICENSE_CLOCK_HMAC_KEY, message)   → 64 lowercase hex

prev_hash_bytes is the RAW 32-byte digest of the preceding row (b"" for the
genesis row, whose prev_hash column is NULL). seq is packed as fixed-width
8-byte big-endian so the message length never depends on its digit count.
The committed test vectors build the expected hash with an independent inline
hmac construction — never by calling this module.
"""

import hashlib
import hmac
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from pharmaos_api.licensing.canonical import canonical_json_bytes, iso_z

CHAIN_DOMAIN = b"pharmaos.clock.chain.v1\n"
EVENT_TYPES = ("boot_seen", "rollback_detected", "source_regression", "activation", "state_changed")
ORIGINS = ("boot", "activation", "periodic", "reconciliation")


class LicenseClockEventV1(BaseModel):
    """Strict closed event schema (P4 §1). seq / prev_hash / entry_hash are
    chain metadata and are deliberately OUTSIDE the event — they never enter
    the HMAC through this model (prev_hash enters the message separately, as
    raw bytes)."""

    model_config = ConfigDict(extra="forbid")

    event_type: Literal[
        "boot_seen", "rollback_detected", "source_regression", "activation", "state_changed"
    ]
    observed_at: datetime
    high_water_utc: datetime
    origin: Literal["boot", "activation", "periodic", "reconciliation"]
    ref: str = Field(default="", max_length=64)
    anomaly_count: int = Field(default=0, ge=0)

    @field_validator("observed_at", "high_water_utc")
    @classmethod
    def _must_be_utc_instant(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware UTC instants")
        return value.astimezone(UTC)

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "observed_at": iso_z(self.observed_at),
            "high_water_utc": iso_z(self.high_water_utc),
            "origin": self.origin,
            "ref": self.ref,
            "anomaly_count": self.anomaly_count,
        }


def chain_message_bytes(seq: int, prev_hash_bytes: bytes, event: LicenseClockEventV1) -> bytes:
    """The exact HMAC input (LOCK-1), exposed for the committed test vectors —
    tests assemble the expected digest with an independent hmac construction
    over these bytes instead of trusting the production hashing function."""
    return (
        CHAIN_DOMAIN
        + seq.to_bytes(8, "big", signed=False)
        + prev_hash_bytes
        + canonical_json_bytes(event.to_canonical_dict())
    )


def chain_entry_hash(
    clock_key: bytes, seq: int, prev_hash_bytes: bytes, event: LicenseClockEventV1
) -> str:
    return hmac.new(
        clock_key, chain_message_bytes(seq, prev_hash_bytes, event), hashlib.sha256
    ).hexdigest()
