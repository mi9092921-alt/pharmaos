"""User service — the service-layer boundary for the encrypted phone field.

CLAUDE.md: encryption happens here, before write; decryption only on
authorized reads.
"""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.models import User
from pharmaos_api.security.crypto import DecryptionError, decrypt_field, encrypt_field

logger = logging.getLogger(__name__)

PHONE_CONTEXT = "users.phone"


def encrypt_phone(phone: str | None) -> bytes | None:
    """Encrypt a phone value for storage (None clears it)."""
    return encrypt_field(phone, context=PHONE_CONTEXT) if phone is not None else None


async def set_user_phone(session: AsyncSession, user: User, phone: str | None) -> None:
    """Store the user's phone encrypted (or clear it) and commit."""
    user.phone_encrypted = encrypt_phone(phone)
    await session.commit()


def get_user_phone(user: User) -> str | None:
    """Decrypt the user's phone for an AUTHORIZED read path (raises on failure)."""
    if user.phone_encrypted is None:
        return None
    return decrypt_field(bytes(user.phone_encrypted), context=PHONE_CONTEXT)


def safe_get_user_phone(user: User) -> str | None:
    """Bulk-read variant: never raises. Returns None if the value cannot be
    decrypted (e.g. a key rotation left an old row unreadable) so one bad row
    does not break a whole list response. The failure is logged for follow-up."""
    try:
        return get_user_phone(user)
    except DecryptionError:
        logger.warning("Could not decrypt phone for user %s (key mismatch?)", user.id)
        return None
