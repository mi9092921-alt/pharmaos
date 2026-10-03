"""Field-level encryption — AES-256-GCM (CLAUDE.md encrypted-field classification).

Rules (CLAUDE.md):
- Encryption happens in the SERVICE layer before write; decryption only on
  authorized reads.
- Encrypted fields (current phase): users.phone. Phase 2 adds
  customers.national_id / customers.insurance_number / prescriptions.notes
  and compliance credentials.
- Searchable/indexable business data (names, prices, quantities, invoice
  numbers) is intentionally NOT encrypted.

Wire format: nonce (12 bytes) || AES-GCM ciphertext+tag.
The field name is bound as AAD ("context") so a ciphertext copied into a
different column fails authentication.
"""

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from pharmaos_api.security import keystore

_NONCE_SIZE = 12


class DecryptionError(Exception):
    """Ciphertext failed authentication (wrong key, tampering, or wrong context)."""


def _aesgcm() -> AESGCM:
    return AESGCM(keystore.ensure_encryption_key())


def encrypt_field(plaintext: str, *, context: str) -> bytes:
    """Encrypt a field value. `context` is the fully-qualified column name."""
    nonce = os.urandom(_NONCE_SIZE)
    ciphertext = _aesgcm().encrypt(nonce, plaintext.encode("utf-8"), context.encode("utf-8"))
    return nonce + ciphertext


def decrypt_field(payload: bytes, *, context: str) -> str:
    """Decrypt a field value previously produced by encrypt_field."""
    if len(payload) <= _NONCE_SIZE:
        raise DecryptionError("payload too short")
    nonce, ciphertext = payload[:_NONCE_SIZE], payload[_NONCE_SIZE:]
    try:
        plaintext = _aesgcm().decrypt(nonce, ciphertext, context.encode("utf-8"))
    except InvalidTag as exc:
        raise DecryptionError("authentication failed") from exc
    return plaintext.decode("utf-8")
