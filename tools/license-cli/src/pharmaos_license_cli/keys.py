"""PLKEY1 — the passphrase-encrypted issuer key file (P4 §2, frozen binary
format; no algorithm alternatives):

    magic b"PLKEY1" (6) | version u8 = 1 | kid_len u8 | kid (ASCII, ≤16) |
    salt 16B | nonce 12B | AES-256-GCM(ciphertext 32B seed + 16B tag)

    derived_key = scrypt(passphrase, salt, N=32768, r=8, p=1, dkLen=32)

The file lives OUTSIDE this repository (default ~/.pharmaos-vendor/) — it never
enters git, a customer device, or a device backup. Losing it without the
exported copy means no more license issuance (the keystore has no recovery).
"""

import getpass
import os
import secrets
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"PLKEY1"
VERSION = 1
SCRYPT_N = 32768
SCRYPT_R = 8
SCRYPT_P = 1
SALT_LEN = 16
NONCE_LEN = 12
SEED_LEN = 32
TAG_LEN = 16
KID_MAX = 16

DEFAULT_DIR = Path.home() / ".pharmaos-vendor"
DEFAULT_KEY_FILE = DEFAULT_DIR / "license-signing.key"
DEFAULT_LEDGER_FILE = DEFAULT_DIR / "ledger.jsonl"


class KeyFileError(RuntimeError):
    """Malformed key file / wrong passphrase."""


def _derive_key(passphrase: bytes, salt: bytes) -> bytes:
    kdf = Scrypt(salt=salt, length=32, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    return kdf.derive(passphrase)


def save_key_file(
    path: Path, *, kid: str, private_seed: bytes, passphrase: bytes
) -> None:
    if not (1 <= len(kid) <= KID_MAX) or not kid.isascii():
        raise KeyFileError("kid must be 1-16 ASCII characters")
    if len(private_seed) != SEED_LEN:
        raise KeyFileError("private seed must be 32 bytes")
    salt = os.urandom(SALT_LEN)
    nonce = os.urandom(NONCE_LEN)
    derived = _derive_key(passphrase, salt)
    ciphertext = AESGCM(derived).encrypt(nonce, private_seed, None)
    blob = (
        MAGIC
        + bytes([VERSION])
        + bytes([len(kid)])
        + kid.encode("ascii")
        + salt
        + nonce
        + ciphertext
    )
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.write_bytes(blob)
    os.chmod(path, 0o600)


def load_key_file(path: Path, *, passphrase: bytes) -> tuple[str, bytes]:
    """Returns (kid, private_seed). Raises KeyFileError on anything off."""
    blob = path.read_bytes()
    if len(blob) < len(MAGIC) + 3 or blob[: len(MAGIC)] != MAGIC:
        raise KeyFileError("not a PLKEY1 key file")
    offset = len(MAGIC)
    version = blob[offset]
    offset += 1
    if version != VERSION:
        raise KeyFileError(f"unsupported PLKEY1 version: {version}")
    kid_len = blob[offset]
    offset += 1
    kid = blob[offset : offset + kid_len]
    offset += kid_len
    if not (1 <= kid_len <= KID_MAX) or not kid.isascii():
        raise KeyFileError("malformed kid")
    salt = blob[offset : offset + SALT_LEN]
    offset += SALT_LEN
    nonce = blob[offset : offset + NONCE_LEN]
    offset += NONCE_LEN
    ciphertext = blob[offset:]
    if len(ciphertext) != SEED_LEN + TAG_LEN:
        raise KeyFileError("malformed ciphertext")
    derived = _derive_key(passphrase, salt)
    try:
        seed = AESGCM(derived).decrypt(nonce, ciphertext, None)
    except Exception as exc:
        raise KeyFileError("wrong passphrase or corrupted key file") from exc
    return kid.decode("ascii"), seed


def prompt_passphrase(confirm: bool) -> bytes:
    # Automation path matches the repo convention (PHARMAOS_ADMIN_PASSWORD +
    # getpass fallback in pharmaos_api.cli): an env var may carry the
    # passphrase for scripted use; interactive runs prompt on the TTY.
    from_env = os.environ.get("PHARMAOS_LICENSE_PASSPHRASE")
    if from_env:
        if len(from_env) < 8:
            raise KeyFileError("passphrase must be at least 8 characters")
        return from_env.encode("utf-8")
    first = getpass.getpass("Issuer key passphrase: ")
    if len(first) < 8:
        raise KeyFileError("passphrase must be at least 8 characters")
    if confirm and getpass.getpass("Confirm passphrase: ") != first:
        raise KeyFileError("passphrases do not match")
    return first.encode("utf-8")


def generate_kid() -> str:
    return secrets.token_hex(4)  # 8 hex chars — the baked-container key id
