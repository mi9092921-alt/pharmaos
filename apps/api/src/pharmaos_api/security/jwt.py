"""JWT RS256 (CLAUDE.md mandatory settings).

- Asymmetric RS256: the cloud / other services can VERIFY tokens without
  owning the signing key.
- Access token: 15 minutes. Refresh token: 7 days.
- token_version claim: bumping users.token_version invalidates outstanding
  tokens (session invalidation on password change / targeted logout).
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt as pyjwt

from pharmaos_api.config import get_settings
from pharmaos_api.errors import ApiError, ErrorCode
from pharmaos_api.security import keystore

TokenType = Literal["access", "refresh"]

_ISSUER = "pharmaos"


def _keys() -> tuple[str, str]:
    return keystore.ensure_jwt_keypair()


def create_token(
    *,
    user_id: uuid.UUID,
    token_version: int,
    token_type: TokenType,
    role_code: str | None,
) -> str:
    s = get_settings()
    now = datetime.now(UTC)
    if token_type == "access":  # noqa: S105 — token TYPE discriminator, not a credential
        expires = now + timedelta(minutes=s.access_token_expire_minutes)
    else:
        expires = now + timedelta(hours=s.refresh_token_expire_hours)
    private_pem, _ = _keys()
    payload: dict[str, Any] = {
        "iss": _ISSUER,
        "sub": str(user_id),
        "type": token_type,
        "token_version": token_version,
        "role": role_code,
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
        "jti": str(uuid.uuid4()),
    }
    return pyjwt.encode(payload, private_pem, algorithm=s.jwt_algorithm)


def decode_token(token: str, *, expected_type: TokenType) -> dict[str, Any]:
    s = get_settings()
    _, public_pem = _keys()
    try:
        payload: dict[str, Any] = pyjwt.decode(
            token, public_pem, algorithms=[s.jwt_algorithm], issuer=_ISSUER
        )
    except pyjwt.ExpiredSignatureError:
        raise ApiError(ErrorCode.UNAUTHORIZED, 401) from None
    except pyjwt.InvalidTokenError:
        raise ApiError(ErrorCode.UNAUTHORIZED, 401) from None
    if payload.get("type") != expected_type:
        raise ApiError(ErrorCode.UNAUTHORIZED, 401)
    return payload
