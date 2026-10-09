"""P4-M2 License Gate Middleware & License Endpoints Tests.

Tests the ASGI license gate middleware, allowlists, status schema,
tamper lockdown, and activation endpoint limit & rate-limiting.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from pharmaos_api.errors import ErrorCode
from pharmaos_api.licensing import runtime
from pharmaos_api.licensing.runtime import LicenseRuntimeState
from pharmaos_api.main import create_app


@pytest.fixture
async def gate_client() -> httpx.AsyncClient:
    app = create_app(license_scheduler=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


# ---------------------------------------------------------------------------
# A) Patchability Guard & Fail-closed
# ---------------------------------------------------------------------------


async def test_gate_fail_closed_when_state_is_none(
    gate_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "get_state", lambda: None)
    res = await gate_client.get("/api/v1/users")
    assert res.status_code == 503
    data = res.json()
    assert data["success"] is False
    assert data["error"]["code"] == ErrorCode.LICENSE_STATE_ERROR


async def test_gate_patchability_guard(
    gate_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. Flip to unlicensed -> /api/v1/users is blocked
    monkeypatch.setattr(
        runtime,
        "get_state",
        lambda: LicenseRuntimeState(status=runtime.STATUS_UNLICENSED),
    )
    res = await gate_client.get("/api/v1/users")
    assert res.status_code == 403
    assert res.json()["error"]["code"] == ErrorCode.LICENSE_REQUIRED

    # 2. Flip to active -> gate passes through (downstream gives 401 unauthenticated, not 403)
    monkeypatch.setattr(
        runtime,
        "get_state",
        lambda: LicenseRuntimeState(
            status=runtime.STATUS_ACTIVE,
            hwid="PHAR-TEST-0000-0000-0000",
        ),
    )
    res = await gate_client.get("/api/v1/users")
    assert res.status_code == 401
    assert res.json()["error"]["code"] == ErrorCode.UNAUTHORIZED


# ---------------------------------------------------------------------------
# B) Unlicensed Allowlist
# ---------------------------------------------------------------------------


async def test_unlicensed_allows_health(
    gate_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runtime,
        "get_state",
        lambda: LicenseRuntimeState(status=runtime.STATUS_UNLICENSED),
    )
    res = await gate_client.get("/api/v1/health")
    assert res.status_code == 200
    assert res.json()["success"] is True


async def test_unlicensed_allows_license_status(
    gate_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runtime,
        "get_state",
        lambda: LicenseRuntimeState(
            status=runtime.STATUS_UNLICENSED,
            hwid="PHAR-TEST-1234-5678-9012",
            needs_activation=True,
        ),
    )
    res = await gate_client.get("/api/v1/license/status")
    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True
    assert body["data"]["status"] == "unlicensed"
    assert body["data"]["hwid"] == "PHAR-TEST-1234-5678-9012"
    assert body["data"]["valid_until"] is None
    assert body["data"]["days_left"] is None
    assert body["data"]["needs_activation"] is True


async def test_unlicensed_blocks_protected_routes(
    gate_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runtime,
        "get_state",
        lambda: LicenseRuntimeState(status=runtime.STATUS_UNLICENSED),
    )
    # Auth endpoints
    r1 = await gate_client.get("/api/v1/auth/me")
    assert r1.status_code == 403
    assert r1.json()["error"]["code"] == ErrorCode.LICENSE_REQUIRED

    r2 = await gate_client.post("/api/v1/auth/login", json={"username": "a", "password": "b"})
    assert r2.status_code == 403
    assert r2.json()["error"]["code"] == ErrorCode.LICENSE_REQUIRED

    # Business endpoints
    r3 = await gate_client.get("/api/v1/catalog/categories")
    assert r3.status_code == 403
    assert r3.json()["error"]["code"] == ErrorCode.LICENSE_REQUIRED


# ---------------------------------------------------------------------------
# C) Tamper / Key Lost / Error / Clock Error Lockdown
# ---------------------------------------------------------------------------


async def test_locked_status_tamper(
    gate_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runtime,
        "get_state",
        lambda: LicenseRuntimeState(status=runtime.STATUS_TAMPER),
    )
    res = await gate_client.get("/api/v1/users")
    assert res.status_code == 423
    assert res.json()["error"]["code"] == ErrorCode.LICENSE_TAMPER_DETECTED


async def test_locked_status_key_lost(
    gate_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runtime,
        "get_state",
        lambda: LicenseRuntimeState(status=runtime.STATUS_KEY_LOST),
    )
    res = await gate_client.get("/api/v1/users")
    assert res.status_code == 423
    assert res.json()["error"]["code"] == ErrorCode.LICENSE_KEY_LOST


async def test_locked_status_clock_error(
    gate_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runtime,
        "get_state",
        lambda: LicenseRuntimeState(status=runtime.STATUS_CLOCK_ERROR),
    )
    res = await gate_client.get("/api/v1/users")
    assert res.status_code == 423
    assert res.json()["error"]["code"] == ErrorCode.LICENSE_TAMPER_DETECTED


async def test_locked_status_error(
    gate_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runtime,
        "get_state",
        lambda: LicenseRuntimeState(status=runtime.STATUS_ERROR),
    )
    res = await gate_client.get("/api/v1/users")
    assert res.status_code == 503
    assert res.json()["error"]["code"] == ErrorCode.LICENSE_STATE_ERROR


# ---------------------------------------------------------------------------
# D) Read-Only Mode (Mutations Blocked, Reads & Reactivation Allowed)
# ---------------------------------------------------------------------------


async def test_read_only_blocks_mutations_and_allows_reads(
    gate_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runtime,
        "get_state",
        lambda: LicenseRuntimeState(status=runtime.STATUS_READ_ONLY),
    )
    # Reads allowed through gate (falls through to router auth -> 401)
    res_get = await gate_client.get("/api/v1/medications")
    assert res_get.status_code in (200, 401)

    # Health check is always 200
    res_health = await gate_client.get("/api/v1/health")
    assert res_health.status_code == 200

    # Mutations blocked with 403 E-LIC-002
    res_post = await gate_client.post("/api/v1/pos/invoices", json={})
    assert res_post.status_code == 403
    assert res_post.json()["error"]["code"] == ErrorCode.LICENSE_READ_ONLY

    res_put = await gate_client.put("/api/v1/catalog/products/1", json={})
    assert res_put.status_code == 403
    assert res_put.json()["error"]["code"] == ErrorCode.LICENSE_READ_ONLY

    res_del = await gate_client.delete("/api/v1/catalog/products/1")
    assert res_del.status_code == 403
    assert res_del.json()["error"]["code"] == ErrorCode.LICENSE_READ_ONLY


async def test_read_only_permits_activate(
    gate_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runtime,
        "get_state",
        lambda: LicenseRuntimeState(status=runtime.STATUS_READ_ONLY),
    )
    # POST /api/v1/license/activate is not blocked by read_only gate (reaches activate handler)
    res = await gate_client.post(
        "/api/v1/license/activate",
        content=b"invalid-container-data",
        headers={"Content-Type": "application/octet-stream"},
    )
    # Reached the activate endpoint, so error is invalid signature (400), not read_only (403)
    assert res.status_code == 400
    assert res.json()["error"]["code"] == ErrorCode.LICENSE_INVALID_SIGNATURE


# ---------------------------------------------------------------------------
# E) Grace Period
# ---------------------------------------------------------------------------


async def test_grace_period_allows_operations(
    gate_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(UTC)
    monkeypatch.setattr(
        runtime,
        "get_state",
        lambda: LicenseRuntimeState(
            status=runtime.STATUS_GRACE,
            valid_until=now - timedelta(days=2),
            grace_until=now + timedelta(days=28),
        ),
    )
    # Should pass gate and reach auth handler
    res = await gate_client.get("/api/v1/users")
    assert res.status_code == 401
    assert res.json()["error"]["code"] == ErrorCode.UNAUTHORIZED


# ---------------------------------------------------------------------------
# F) 64 KiB Payload Limit on /activate
# ---------------------------------------------------------------------------


async def test_activate_64kib_limit(gate_client: httpx.AsyncClient) -> None:
    # 65 KiB payload
    large_payload = b"X" * (65 * 1024)
    res = await gate_client.post(
        "/api/v1/license/activate",
        content=large_payload,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert res.status_code == 413
    assert res.json()["error"]["code"] == ErrorCode.VALIDATION_FAILED


# ---------------------------------------------------------------------------
# G) OPTIONS & HEAD Always Allowed
# ---------------------------------------------------------------------------


async def test_options_and_head_always_allowed(
    gate_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Even in tamper or unlicensed state
    for st in (runtime.STATUS_TAMPER, runtime.STATUS_UNLICENSED):
        monkeypatch.setattr(
            runtime,
            "get_state",
            lambda s=st: LicenseRuntimeState(status=s),
        )
        res_opts = await gate_client.options("/api/v1/users")
        assert res_opts.status_code != 423
        assert res_opts.status_code != 403

        res_head = await gate_client.head("/api/v1/health")
        assert res_head.status_code == 200


# ---------------------------------------------------------------------------
# H) License Status Schema Nullability Contract
# ---------------------------------------------------------------------------


async def test_license_status_schema(
    gate_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. Unlicensed: valid_until and days_left are null
    monkeypatch.setattr(
        runtime,
        "get_state",
        lambda: LicenseRuntimeState(
            status=runtime.STATUS_UNLICENSED,
            hwid="PHAR-TEST-1234",
            needs_activation=True,
        ),
    )
    r1 = await gate_client.get("/api/v1/license/status")
    d1 = r1.json()["data"]
    assert d1["status"] == "unlicensed"
    assert d1["hwid"] == "PHAR-TEST-1234"
    assert d1["valid_until"] is None
    assert d1["days_left"] is None
    assert d1["needs_activation"] is True

    # 2. Active: valid_until and days_left are populated
    until = datetime(2027, 10, 1, 0, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(
        runtime,
        "get_state",
        lambda: LicenseRuntimeState(
            status=runtime.STATUS_ACTIVE,
            hwid="PHAR-TEST-1234",
            valid_until=until,
            days_left=356,
            needs_activation=False,
        ),
    )
    r2 = await gate_client.get("/api/v1/license/status")
    d2 = r2.json()["data"]
    assert d2["status"] == "active"
    assert d2["hwid"] == "PHAR-TEST-1234"
    assert d2["valid_until"] == "2027-10-01T00:00:00Z"
    assert d2["days_left"] == 356
    assert d2["needs_activation"] is False
