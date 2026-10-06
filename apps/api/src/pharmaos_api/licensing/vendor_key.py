"""Vendor license-signing public key — baked at build time (P4 §2).

The owner generates the Ed25519 keypair ONCE with `license-cli init` and
commits the public PEM here. Until it is set, every verification path fails
closed: no license can parse, the device stays unlicensed. That is a documented
pre-launch state — NOT a runtime fallback; shipping without the baked key is a
packaging-gate failure (P5-M5), and nothing can be tricked into accepting a
license while the key is unset (there is no key to verify against).
"""

import logging

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from pharmaos_api.errors import ErrorCode
from pharmaos_api.licensing.errors import LicensingError

logger = logging.getLogger(__name__)

VENDOR_LICENSE_PUBLIC_KEY_PEM: str | None = None


def vendor_public_key() -> Ed25519PublicKey:
    """The baked vendor key — fail-closed when the owner has not baked one yet."""
    if VENDOR_LICENSE_PUBLIC_KEY_PEM is None:
        raise LicensingError(ErrorCode.LICENSE_STATE_ERROR, "vendor_key_not_baked")
    key = serialization.load_pem_public_key(VENDOR_LICENSE_PUBLIC_KEY_PEM.encode("ascii"))
    if not isinstance(key, Ed25519PublicKey):
        raise LicensingError(ErrorCode.LICENSE_STATE_ERROR, "vendor_key_not_ed25519")
    return key
