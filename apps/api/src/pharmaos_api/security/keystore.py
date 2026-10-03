"""OS-keystore-backed secret storage (CLAUDE.md key-protection policy).

Production devices: secrets live in the operating-system store —
Windows DPAPI / macOS Keychain — accessed from Python via `keyring`
(the approved alternative to Electron safeStorage in CLAUDE.md).
.env carries only non-secret settings and key REFERENCES.

First run: keys are generated and stored in the secure store automatically.
An emergency copy of the keys is included in the ENCRYPTED backup (M9) —
without it, restore would be impossible (CLAUDE.md).

Non-production fallback: when no OS keyring backend exists (dev containers,
CI runners), secrets fall back to a 0600 file under ./.pharmaos-devkeys.
This fallback REFUSES to run in production (the spec forbids plaintext keys
on production devices).
"""

import logging
import os
from pathlib import Path

import keyring
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from keyring.errors import KeyringError

from pharmaos_api.config import get_settings

logger = logging.getLogger(__name__)

SERVICE_NAME = "pharmaos"
JWT_PRIVATE_KEY_NAME = "JWT_PRIVATE_KEY"
JWT_PUBLIC_KEY_NAME = "JWT_PUBLIC_KEY"
ENCRYPTION_KEY_NAME = "ENCRYPTION_KEY"
BACKUP_KEY_NAME = "BACKUP_ENCRYPTION_KEY"

_DEV_STORE_DIR = Path(".pharmaos-devkeys")


class KeystoreUnavailableError(RuntimeError):
    """No secure keystore available in production."""


def _dev_store_path(name: str) -> Path:
    return _DEV_STORE_DIR / name


def _dev_get(name: str) -> str | None:
    path = _dev_store_path(name)
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return None


def _dev_set(name: str, value: str) -> None:
    _DEV_STORE_DIR.mkdir(mode=0o700, exist_ok=True)
    path = _dev_store_path(name)
    path.write_text(value, encoding="utf-8")
    os.chmod(path, 0o600)


def get_secret(name: str) -> str | None:
    """Read a secret from the OS keystore, falling back to the dev store."""
    try:
        value = keyring.get_password(SERVICE_NAME, name)
        if value is not None:
            return value
    except KeyringError:
        logger.warning("OS keyring unavailable while reading %s", name)
    if get_settings().is_production:
        return None
    return _dev_get(name)


def set_secret(name: str, value: str) -> None:
    """Write a secret to the OS keystore (dev-store fallback outside production)."""
    try:
        keyring.set_password(SERVICE_NAME, name, value)
        return
    except KeyringError:
        if get_settings().is_production:
            raise KeystoreUnavailableError(
                "No OS keystore available — refusing to store secrets in plaintext "
                "on a production device."
            ) from None
        logger.warning("OS keyring unavailable — using 0600 dev-store for %s", name)
        _dev_set(name, value)


def ensure_jwt_keypair() -> tuple[str, str]:
    """Return (private_pem, public_pem), generating & storing them on first run."""
    private_pem = get_secret(JWT_PRIVATE_KEY_NAME)
    public_pem = get_secret(JWT_PUBLIC_KEY_NAME)
    if private_pem and public_pem:
        return private_pem, public_pem

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("utf-8")
    public_pem = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )
    set_secret(JWT_PRIVATE_KEY_NAME, private_pem)
    set_secret(JWT_PUBLIC_KEY_NAME, public_pem)
    logger.info("Generated new RS256 JWT keypair and stored it in the keystore.")
    return private_pem, public_pem


def ensure_encryption_key() -> bytes:
    """Return the 32-byte AES-256 field-encryption key, generating on first run."""
    stored = get_secret(ENCRYPTION_KEY_NAME)
    if stored:
        return bytes.fromhex(stored)
    key = os.urandom(32)
    set_secret(ENCRYPTION_KEY_NAME, key.hex())
    logger.info("Generated new AES-256 field-encryption key and stored it in the keystore.")
    return key


def ensure_backup_key() -> bytes:
    """Return the INDEPENDENT 32-byte backup-encryption key (CLAUDE.md:
    backups are always encrypted with a key separate from the field key)."""
    stored = get_secret(BACKUP_KEY_NAME)
    if stored:
        return bytes.fromhex(stored)
    key = os.urandom(32)
    set_secret(BACKUP_KEY_NAME, key.hex())
    logger.info("Generated new AES-256 backup-encryption key and stored it in the keystore.")
    return key
