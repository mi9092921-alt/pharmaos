"""AES-256-GCM field encryption: round-trip, tamper detection, context binding,
key-from-keystore, and the users.phone service-layer path against the DB."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.security import keystore
from pharmaos_api.security.crypto import DecryptionError, decrypt_field, encrypt_field


def test_roundtrip_arabic_text() -> None:
    payload = encrypt_field("٠١٢٣٤٥٦٧٨٩ رقم تجريبي", context="users.phone")
    assert decrypt_field(payload, context="users.phone") == "٠١٢٣٤٥٦٧٨٩ رقم تجريبي"


def test_ciphertext_differs_per_call_but_decrypts() -> None:
    a = encrypt_field("0100000000", context="users.phone")
    b = encrypt_field("0100000000", context="users.phone")
    assert a != b  # random nonce per encryption
    assert decrypt_field(a, context="users.phone") == decrypt_field(b, context="users.phone")


def test_tampered_ciphertext_rejected() -> None:
    payload = bytearray(encrypt_field("0100000000", context="users.phone"))
    payload[-1] ^= 0x01
    with pytest.raises(DecryptionError):
        decrypt_field(bytes(payload), context="users.phone")


def test_context_binding_rejects_column_swap() -> None:
    payload = encrypt_field("29001010100000", context="customers.national_id")
    with pytest.raises(DecryptionError):
        decrypt_field(payload, context="users.phone")


def test_key_is_persisted_in_keystore() -> None:
    k1 = keystore.ensure_encryption_key()
    k2 = keystore.ensure_encryption_key()
    assert k1 == k2 and len(k1) == 32  # AES-256


async def test_user_phone_encrypted_at_rest(db_session: AsyncSession, seeded_user: dict) -> None:
    from pharmaos_api.models import User
    from pharmaos_api.services.user_service import get_user_phone, set_user_phone

    user = (
        await db_session.execute(select(User).where(User.username == seeded_user["username"]))
    ).scalar_one()

    await set_user_phone(db_session, user, "01001234567")
    await db_session.refresh(user)

    raw = bytes(user.phone_encrypted or b"")
    assert b"01001234567" not in raw  # never plaintext at rest
    assert get_user_phone(user) == "01001234567"

    await set_user_phone(db_session, user, None)
    await db_session.refresh(user)
    assert user.phone_encrypted is None and get_user_phone(user) is None
