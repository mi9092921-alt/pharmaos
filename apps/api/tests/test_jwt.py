"""JWT RS256 sign/verify, expiry semantics, token_version claim."""

import uuid

import pytest

from pharmaos_api.errors import ApiError
from pharmaos_api.security.jwt import create_token, decode_token


def test_roundtrip_access_token() -> None:
    uid = uuid.uuid4()
    token = create_token(user_id=uid, token_version=3, token_type="access", role_code="super_admin")
    payload = decode_token(token, expected_type="access")
    assert payload["sub"] == str(uid)
    assert payload["token_version"] == 3
    assert payload["role"] == "super_admin"
    # 15-minute lifetime (CLAUDE.md)
    assert payload["exp"] - payload["iat"] == 15 * 60


def test_refresh_lifetime_is_seven_days() -> None:
    token = create_token(
        user_id=uuid.uuid4(), token_version=0, token_type="refresh", role_code=None
    )
    payload = decode_token(token, expected_type="refresh")
    assert payload["exp"] - payload["iat"] == 7 * 24 * 3600


def test_type_confusion_rejected() -> None:
    token = create_token(
        user_id=uuid.uuid4(), token_version=0, token_type="refresh", role_code=None
    )
    with pytest.raises(ApiError):
        decode_token(token, expected_type="access")


def test_tampered_token_rejected() -> None:
    token = create_token(user_id=uuid.uuid4(), token_version=0, token_type="access", role_code=None)
    with pytest.raises(ApiError):
        decode_token(token[:-4] + "AAAA", expected_type="access")
