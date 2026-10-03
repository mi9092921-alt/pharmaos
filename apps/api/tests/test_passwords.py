"""Password policy + argon2id hashing (CLAUDE.md security standards)."""

from pharmaos_api.security.passwords import (
    hash_password,
    validate_password_policy,
    verify_password,
)


def test_policy_rejects_each_missing_rule() -> None:
    assert "password.too_short" in validate_password_policy("Ab1!")
    assert "password.missing_uppercase" in validate_password_policy("abcdef1!")
    assert "password.missing_number" in validate_password_policy("Abcdefg!")
    assert "password.missing_special" in validate_password_policy("Abcdefg1")


def test_policy_accepts_compliant_password() -> None:
    assert validate_password_policy("Sup3r@dmin!") == []


def test_hash_is_argon2id_and_verifies() -> None:
    h = hash_password("Sup3r@dmin!")
    assert h.startswith("$argon2id$")
    assert verify_password(h, "Sup3r@dmin!") is True
    assert verify_password(h, "wrong-password") is False
