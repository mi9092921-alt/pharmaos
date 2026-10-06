"""Shared licensing-test helpers: a TEST-ONLY vendor keypair (never the
production issuer key — P4 §2 CI rule) and a license-file factory."""

from datetime import UTC, datetime, timedelta

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pharmaos_api.licensing.payload import LicensePayloadV1, sign_payload

TEST_KID = "testkid01"
TEST_HWID = "PHAR-AAAA-BBBB-CCCC-DDDD"


def make_vendor_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def make_license(
    private_key: Ed25519PrivateKey,
    *,
    license_id: str,
    hwid: str = TEST_HWID,
    issued_at: datetime | None = None,
    valid_until: datetime | None = None,
    customer: str = "صيدلية الاختبار",
    kind: str = "subscription",
    features: list[str] | None = None,
    kid: str = TEST_KID,
) -> bytes:
    issued = issued_at or datetime.now(UTC)
    until = valid_until or (issued + timedelta(days=365))
    payload = LicensePayloadV1(
        schema_version=1,
        license_id=license_id,
        customer=customer,
        hwid=hwid,
        issued_at=issued,
        valid_until=until,
        kind=kind,  # type: ignore[arg-type]
        features=features or [],
    )
    return sign_payload(payload, private_key=private_key, kid=kid)
