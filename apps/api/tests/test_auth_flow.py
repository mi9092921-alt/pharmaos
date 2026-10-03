"""End-to-end auth flow against a real PostgreSQL 17 with migrations applied:
login -> me -> refresh -> logout (token_version bump) -> lockout policy."""

import httpx

# pytest-asyncio runs these async tests automatically (asyncio_mode = "auto").


async def test_login_me_refresh_logout(client: httpx.AsyncClient, seeded_user: dict) -> None:
    # login sets httpOnly session cookies + csrf cookie
    r = await client.post(
        "/api/v1/auth/login",
        json={"username": seeded_user["username"], "password": seeded_user["password"]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["data"]["user"]["role"] == "super_admin"
    assert body["data"]["csrf_token"]
    assert "pharmaos_access" in r.cookies and "pharmaos_refresh" in r.cookies

    # me (cookie auth)
    r = await client.get("/api/v1/auth/me")
    assert r.status_code == 200
    assert r.json()["data"]["user"]["username"] == seeded_user["username"]

    # refresh rotates the pair
    r = await client.post("/api/v1/auth/refresh")
    assert r.status_code == 200
    new_csrf = r.json()["data"]["csrf_token"]

    # logout without CSRF header -> rejected (double-submit check)
    r = await client.post("/api/v1/auth/logout")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "E-AUTH-004"

    # logout with CSRF header -> ok; bumps token_version
    r = await client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": new_csrf})
    assert r.status_code == 200

    # old access token is now invalid (token_version bumped)
    r = await client.get("/api/v1/auth/me")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "E-AUTH-001"


async def test_wrong_password_then_lockout(client: httpx.AsyncClient, seeded_user: dict) -> None:
    # 4 wrong attempts -> still unauthorized (not locked yet)
    for _ in range(4):
        r = await client.post(
            "/api/v1/auth/login",
            json={"username": seeded_user["username"], "password": "Wrong1!aa"},
        )
        assert r.status_code == 401

    # 5th wrong attempt -> account locked for 15 minutes (E-AUTH-003, HTTP 423)
    r = await client.post(
        "/api/v1/auth/login",
        json={"username": seeded_user["username"], "password": "Wrong1!aa"},
    )
    assert r.status_code == 423
    assert r.json()["error"]["code"] == "E-AUTH-003"

    # even the CORRECT password is rejected while locked
    r = await client.post(
        "/api/v1/auth/login",
        json={"username": seeded_user["username"], "password": seeded_user["password"]},
    )
    assert r.status_code == 423


async def test_unknown_user_indistinguishable(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/api/v1/auth/login", json={"username": "no_such_user", "password": "Whatever1!"}
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "E-AUTH-001"
