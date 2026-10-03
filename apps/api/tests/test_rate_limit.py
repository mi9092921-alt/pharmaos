"""Login rate limit (CLAUDE.md: login 5/minute) — transport-level protection,
independent of the per-account lockout policy."""

import httpx
import pytest

from pharmaos_api.config import get_settings


async def test_sixth_rapid_login_is_rate_limited(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "login_rate_limit_per_minute", 5)

    for _ in range(5):
        r = await client.post(
            "/api/v1/auth/login", json={"username": "rl_user", "password": "Whatever1!"}
        )
        assert r.status_code == 401  # unknown user — but the attempt is counted

    r = await client.post(
        "/api/v1/auth/login", json={"username": "rl_user", "password": "Whatever1!"}
    )
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "E-AUTH-005"
