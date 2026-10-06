"""License file container + payload schema (P4 §2 — closed contracts).

Container: {"format","kid","payload","signature"} — strict top-level keys,
standard Base64 (with padding) of the 64-byte Ed25519 signature; the signature
covers ONLY the canonical payload bytes (the container itself is never
canonicalized, so transport formatting and key order are free).

Verification is fail-closed: any structural deviation raises E-LIC-003 with a
distinct details.reason (bad_signature | duplicate_keys | unknown_fields |
schema_mismatch; the future-dated check lives in activation and adds
issued_at_in_future).
"""

import re
from base64 import b64decode, b64encode
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from pharmaos_api.errors import ErrorCode
from pharmaos_api.licensing.canonical import (
    DuplicateKeyError,
    canonical_json_bytes,
    iso_z,
    loads_strict,
)
from pharmaos_api.licensing.errors import LicensingError

CONTAINER_FORMAT = "pharmaos-license-v1"
MAX_LICENSE_FILE_BYTES = 64 * 1024
CONTAINER_KEYS = frozenset({"format", "kid", "payload", "signature"})
LICENSE_ID_PATTERN = r"^LIC-\d{4}-\d{4,6}$"
HWID_PATTERN = r"^PHAR-[A-Z0-9]{4}(-[A-Z0-9]{4}){3}$"
# Closed feature registry (P4 §2): identifiers are STORED but feature gating is
# NOT built in v1 — no code path may branch on these.
KNOWN_FEATURES = frozenset({"pos", "inventory", "reports"})

_E_INVALID = ErrorCode.LICENSE_INVALID_SIGNATURE
_LICENSE_ID_RE = re.compile(r"^LIC-(\d{4})-(\d{4,6})$")


class LicensePayloadV1(BaseModel):
    """Strict payload schema (v1). Extra fields are rejected — the signed
    surface is exactly this closed set."""

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

    def to_canonical_dict(self) -> dict[str, Any]:
        """The exact object whose canonical JSON bytes are signed/verified."""
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

    def activation_rank(self) -> tuple[datetime, int, int]:
        """Ordering key for the new-vs-old comparison (P4 §3): numeric
        (issued_at, year, number) — never a string compare, so LIC-2026-99999
        outranks LIC-2026-100000 only when its number really is larger."""
        return (self.issued_at, *license_id_parts(self.license_id))


def license_id_parts(license_id: str) -> tuple[int, int]:
    """(year, number) of `LIC-YYYY-NNNN[NN]` — numeric ordering keys."""
    match = _LICENSE_ID_RE.match(license_id)
    if match is None:  # unreachable: the field pattern already enforces this
        raise ValueError(f"malformed license_id: {license_id!r}")
    return int(match.group(1)), int(match.group(2))


def license_id_str(year: int, number: int) -> str:
    """Canonical rendering used in store records/ledger (6-digit number —
    within the 4–6 digit pattern)."""
    return f"LIC-{year:04d}-{number:06d}"


@dataclass(frozen=True, slots=True)
class LicenseContainer:
    payload: LicensePayloadV1
    kid: str
    signature: bytes
    payload_canonical_bytes: bytes


def parse_license_file(
    raw: bytes, *, public_key: Ed25519PublicKey, accepted_kids: frozenset[str]
) -> LicenseContainer:
    """Strict container verification (P4 §2). Fail-closed: every deviation is
    E-LIC-003 with a distinct reason."""
    if len(raw) > MAX_LICENSE_FILE_BYTES:
        raise LicensingError(_E_INVALID, "file_too_large")
    try:
        document = loads_strict(raw)
    except DuplicateKeyError as exc:
        raise LicensingError(_E_INVALID, "duplicate_keys") from exc
    except ValueError as exc:
        raise LicensingError(_E_INVALID, "malformed_json") from exc
    if not isinstance(document, dict):
        raise LicensingError(_E_INVALID, "schema_mismatch")

    keys = set(document)
    if keys - CONTAINER_KEYS:
        raise LicensingError(_E_INVALID, "unknown_fields")
    if CONTAINER_KEYS - keys:
        raise LicensingError(_E_INVALID, "schema_mismatch")
    if document["format"] != CONTAINER_FORMAT:
        raise LicensingError(_E_INVALID, "schema_mismatch")

    kid = document["kid"]
    if not isinstance(kid, str) or not kid.isascii() or not (1 <= len(kid) <= 16):
        raise LicensingError(_E_INVALID, "schema_mismatch")
    if kid not in accepted_kids:
        raise LicensingError(_E_INVALID, "schema_mismatch")

    signature_b64 = document["signature"]
    if not isinstance(signature_b64, str):
        raise LicensingError(_E_INVALID, "bad_signature")
    try:
        signature = b64decode(signature_b64, validate=True)
    except Exception as exc:  # binascii.Error subclasses ValueError; kept broad-deliberate
        raise LicensingError(_E_INVALID, "bad_signature") from exc
    if len(signature) != 64:
        raise LicensingError(_E_INVALID, "bad_signature")

    payload_document = document["payload"]
    if not isinstance(payload_document, dict):
        raise LicensingError(_E_INVALID, "schema_mismatch")
    try:
        payload = LicensePayloadV1.model_validate(payload_document)
    except ValidationError as exc:
        raise LicensingError(_E_INVALID, _payload_reason(exc)) from exc
    payload_canonical = canonical_json_bytes(payload.to_canonical_dict())
    try:
        public_key.verify(signature, payload_canonical)
    except InvalidSignature as exc:
        raise LicensingError(_E_INVALID, "bad_signature") from exc
    return LicenseContainer(
        payload=payload, kid=kid, signature=signature, payload_canonical_bytes=payload_canonical
    )


def _payload_reason(exc: ValidationError) -> str:
    """Map pydantic validation failures onto the frozen reasons list."""
    if any(error["type"] == "extra_forbidden" for error in exc.errors()):
        return "unknown_fields"
    return "schema_mismatch"


def verify_stored_payload(
    payload_document: dict[str, Any], signature_b64: str, *, public_key: Ed25519PublicKey
) -> LicensePayloadV1:
    """Re-verify a license previously persisted in license_state (P4 §1 — the
    boot check re-runs signature verification; DB-side edits are detectable
    because the signature covers the exact canonical bytes)."""
    try:
        payload = LicensePayloadV1.model_validate(payload_document)
    except ValidationError as exc:
        raise LicensingError(_E_INVALID, _payload_reason(exc)) from exc
    try:
        signature = b64decode(signature_b64, validate=True)
    except Exception as exc:
        raise LicensingError(_E_INVALID, "bad_signature") from exc
    if len(signature) != 64:
        raise LicensingError(_E_INVALID, "bad_signature")
    try:
        public_key.verify(signature, canonical_json_bytes(payload.to_canonical_dict()))
    except InvalidSignature as exc:
        raise LicensingError(_E_INVALID, "bad_signature") from exc
    return payload


def sign_payload(payload: LicensePayloadV1, *, private_key: Any, kid: str) -> bytes:
    """Build the signed container file (owner/CLI/test-side helper — shares the
    exact canonical bytes with the verifier). Returns the file bytes."""
    if not isinstance(private_key, Ed25519PrivateKey):
        raise TypeError("private_key must be an Ed25519PrivateKey")
    canonical = canonical_json_bytes(payload.to_canonical_dict())
    signature = private_key.sign(canonical)
    document = {
        "format": CONTAINER_FORMAT,
        "kid": kid,
        "payload": payload.to_canonical_dict(),
        "signature": b64encode(signature).decode("ascii"),
    }
    return canonical_json_bytes(document)
