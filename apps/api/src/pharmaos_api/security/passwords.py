"""Password hashing & policy (CLAUDE.md security standards).

- Hash: argon2id (preferred algorithm per spec).
- Policy: >= 8 chars, uppercase, number, special character.
"""

import re

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from pharmaos_api.config import get_settings

_hasher = PasswordHasher()  # argon2id defaults (argon2-cffi)

_SPECIAL_RE = re.compile(r"[^A-Za-z0-9]")


def validate_password_policy(password: str) -> list[str]:
    """Return the list of violated policy rules (empty = compliant).

    Rule identifiers are stable machine codes — the UI translates them.
    """
    s = get_settings()
    violations: list[str] = []
    if len(password) < s.min_password_length:
        violations.append("password.too_short")
    if s.require_uppercase and not any(c.isupper() for c in password):
        violations.append("password.missing_uppercase")
    if s.require_number and not any(c.isdigit() for c in password):
        violations.append("password.missing_number")
    if s.require_special and not _SPECIAL_RE.search(password):
        violations.append("password.missing_special")
    return violations


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, candidate: str) -> bool:
    try:
        return _hasher.verify(password_hash, candidate)
    except VerifyMismatchError:
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)
