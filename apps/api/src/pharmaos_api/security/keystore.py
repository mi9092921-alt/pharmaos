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

import contextlib
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
CLOCK_HMAC_KEY_NAME = "LICENSE_CLOCK_HMAC_KEY"
# Device database password (installer decision 1): lives ONLY here — never in
# .env. Generated once by the first-run wizard, bundled (encrypted) inside
# every backup's keys.json so cross-device recovery is self-contained.
DB_PASSWORD_NAME = "DB_PASSWORD"  # noqa: S105  (a keystore entry NAME, not a password)
# Marker distinguishing an OWNER-IMPORTED recovery key from one auto-generated
# by a local backup: restore refuses to guess between them (decision 10).
BACKUP_KEY_IMPORTED_FLAG = "BACKUP_KEY_IMPORTED"

_DEV_STORE_DIR = Path(".pharmaos-devkeys")


def _configure_keyring_persist() -> None:
    """keyring's Windows backend defaults to CRED_PERSIST_ENTERPRISE, whose
    credential blob is capped at 256 bytes - SMALLER THAN A PEM KEY, so the
    JWT keypair write dies with CredWrite 1783 (found by the M2 smoke gate).
    A single-device app has no roaming use for ENTERPRISE: switch to
    LOCAL_MACHINE (blob limit 5*512 = 2560 bytes). No-op off Windows."""
    if os.name != "nt":
        return
    try:
        from win32ctypes.pywin32 import win32cred  # type: ignore[import-untyped]

        backend = keyring.get_keyring()
        backend._persist = win32cred.CRED_PERSIST_LOCAL_MACHINE  # type: ignore[attr-defined]
    except Exception:  # non-Windows keystore / fail backend - nothing to tune
        logger.debug("keyring persist override skipped", exc_info=True)


_configure_keyring_persist()


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
        value = _keyring_read(name)
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
        _keyring_write(name, value)
        return
    except KeyringError:
        pass
    except Exception as exc:  # win32ctypes raises raw pywintypes.error, not KeyringError
        if get_settings().is_production:
            raise KeystoreUnavailableError(f"keystore write failed: {exc}") from exc
        logger.warning("OS keyring write failed (%s) - using 0600 dev-store for %s", exc, name)
        _dev_set(name, value)
        return
    if get_settings().is_production:
        raise KeystoreUnavailableError(
            "No OS keystore available - refusing to store secrets in plaintext "
            "on a production device."
        ) from None
    logger.warning("OS keyring unavailable - using 0600 dev-store for %s", name)
    _dev_set(name, value)


# Windows Credential Manager rejects CredWrite blobs above a practical limit
# we measured at 1280 bytes (found by the M2 smoke gate: the JWT private-key
# PEM is ~1679 chars). Larger secrets are split across sibling credentials
# ("NAME.parts" holds the count; "NAME.part0..N-1" the chunks) transparently.
_KEYRING_CHUNK_SIZE = 1000


def _keyring_read(name: str) -> str | None:
    parts = keyring.get_password(SERVICE_NAME, f"{name}.parts")
    if parts is None:
        return keyring.get_password(SERVICE_NAME, name)
    count = int(parts)
    chunks = []
    for i in range(count):
        chunk = keyring.get_password(SERVICE_NAME, f"{name}.part{i}")
        if chunk is None:
            raise KeystoreUnavailableError(
                f"keystore credential {name} is incomplete (part {i} missing)"
            )
        chunks.append(chunk)
    return "".join(chunks)


def _keyring_write(name: str, value: str) -> None:
    chunks = [value[i : i + _KEYRING_CHUNK_SIZE] for i in range(0, len(value), _KEYRING_CHUNK_SIZE)]
    if len(chunks) > 1:
        keyring.set_password(SERVICE_NAME, f"{name}.parts", str(len(chunks)))
        for i, chunk in enumerate(chunks):
            keyring.set_password(SERVICE_NAME, f"{name}.part{i}", chunk)
        # Rewrite from small to large: drop the single-credential copy.
        with contextlib.suppress(KeyringError):
            keyring.delete_password(SERVICE_NAME, name)
    else:
        # Single-credential layout: clear any previous chunk marker.
        with contextlib.suppress(KeyringError):
            keyring.delete_password(SERVICE_NAME, f"{name}.parts")
        keyring.set_password(SERVICE_NAME, name, value)


def _keyring_clear(name: str) -> None:
    # Chunked layout keeps at most a handful of parts for our secrets
    # (largest is the ~1.7KB PEM = 2 chunks); sweep a generous fixed range.
    targets = (name, f"{name}.parts") + tuple(f"{name}.part{i}" for i in range(8))
    for target in targets:
        with contextlib.suppress(KeyringError):
            keyring.delete_password(SERVICE_NAME, target)


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
    backups are always encrypted with a key separate from the field key).

    Generating on first backup is legitimate; RESTORE paths must never call
    this — they use get_backup_key() so a missing key fails loudly instead of
    silently creating one that can never decrypt the backup (decision 10)."""
    stored = get_secret(BACKUP_KEY_NAME)
    if stored:
        return bytes.fromhex(stored)
    key = os.urandom(32)
    set_secret(BACKUP_KEY_NAME, key.hex())
    logger.info("Generated new AES-256 backup-encryption key and stored it in the keystore.")
    return key


def get_backup_key() -> bytes | None:
    """Non-generating read of the backup-encryption key (restore paths)."""
    stored = get_secret(BACKUP_KEY_NAME)
    return bytes.fromhex(stored) if stored else None


def mark_backup_key_imported() -> None:
    """Record that the current backup key came from the owner's offline copy."""
    set_secret(BACKUP_KEY_IMPORTED_FLAG, "1")


def backup_key_imported() -> bool:
    return get_secret(BACKUP_KEY_IMPORTED_FLAG) == "1"


def get_db_password() -> str | None:
    """The device database password — absent until the first-run wizard
    provisions it. The runtime builds DATABASE_URL from it (decision 1)."""
    return get_secret(DB_PASSWORD_NAME)


def set_db_password(password: str) -> None:
    set_secret(DB_PASSWORD_NAME, password)


def delete_secret(name: str) -> None:
    """Remove a secret (restore rollback: undo an import after a failed swap)."""
    try:
        _keyring_clear(name)
        return
    except KeyringError:
        pass
    if not get_settings().is_production:
        path = _dev_store_path(name)
        if path.is_file():
            path.unlink()


def get_clock_hmac_key() -> bytes | None:
    """Return the 32-byte license clock-HMAC key, or None when absent.

    The P4 §3 virgin check lives in pharmaos_api.licensing (it must inspect the
    chain/external stores before deciding): missing key + existing state is
    `key_lost` (E-LIC-008) and must NEVER regenerate here."""
    stored = get_secret(CLOCK_HMAC_KEY_NAME)
    if stored is None:
        return None
    return bytes.fromhex(stored)


def set_clock_hmac_key(key: bytes) -> None:
    """Store a 32-byte license clock-HMAC key (first-run generation, backup
    `import-keys`, or owner-initiated key rotation — P4 §3)."""
    set_secret(CLOCK_HMAC_KEY_NAME, key.hex())
