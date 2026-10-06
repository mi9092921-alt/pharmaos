"""Payload schema — the closed-contract twin of
pharmaos_api.licensing.payload.LicensePayloadV1 (P4 §2)."""

import re
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pharmaos_license_cli.canonical import iso_z

LICENSE_ID_PATTERN = r"^LIC-\d{4}-\d{4,6}$"
HWID_PATTERN = r"^PHAR-[A-Z0-9]{4}(-[A-Z0-9]{4}){3}$"
KNOWN_FEATURES = frozenset({"pos", "inventory", "reports"})
CONTAINER_FORMAT = "pharmaos-license-v1"
LICENSE_ID_RE = re.compile(r"^LIC-(\d{4})-(\d{4,6})$")

PRESETS: dict[str, int] = {
    "trial": 0,  # days — handled separately (14 days, not months)
    "monthly": 1,
    "annual": 12,
    "emergency": 0,  # days — 7 days
}


class LicensePayloadV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    license_id: str = Field(pattern=LICENSE_ID_PATTERN)
    customer: str = Field(min_length=1, max_length=200)
    hwid: str = Field(pattern=HWID_PATTERN)
    issued_at: datetime
    valid_until: datetime
    kind: Literal["trial", "subscription", "emergency"]
    features: list[str] = Field(default_factory=list)

    @field_validator("issued_at", "valid_until")
    @classmethod
    def _must_be_utc_instant(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware UTC instants")
        return value.astimezone(UTC)

    @field_validator("features")
    @classmethod
    def _closed_feature_set(cls, value: list[str]) -> list[str]:
        unknown = sorted(feature for feature in value if feature not in KNOWN_FEATURES)
        if unknown:
            raise ValueError(f"unknown feature identifiers: {unknown}")
        return value

    @model_validator(mode="after")
    def _valid_until_after_issued(self) -> "LicensePayloadV1":
        if self.valid_until <= self.issued_at:
            raise ValueError("valid_until must be after issued_at")
        return self

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "license_id": self.license_id,
            "customer": self.customer,
            "hwid": self.hwid,
            "issued_at": iso_z(self.issued_at),
            "valid_until": iso_z(self.valid_until),
            "kind": self.kind,
            "features": list(self.features),
        }


def license_id_str(year: int, number: int) -> str:
    return f"LIC-{year:04d}-{number:06d}"


def license_id_parts(license_id: str) -> tuple[int, int]:
    match = LICENSE_ID_RE.match(license_id)
    if match is None:
        raise ValueError(f"malformed license_id: {license_id!r}")
    return int(match.group(1)), int(match.group(2))


def next_license_id(issued_at: datetime, ledger_numbers: list[int]) -> str:
    """Sequential per-ledger numbering: the largest issued number + 1."""
    number = max(ledger_numbers, default=0) + 1
    return license_id_str(issued_at.astimezone(UTC).year, number)


def now_utc() -> datetime:
    return datetime.now(UTC)
