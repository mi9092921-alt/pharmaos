"""Owner-only HTTP controls for the manual backup/restore UI."""

import uuid

import httpx
import pytest

from pharmaos_api import middleware
from pharmaos_api.routers import backup as backup_router
from pharmaos_api.security.passwords import hash_password


async def _login(client: httpx.AsyncClient, user: dict[str, str]) -> str:
    response = await client.post(
        "/api/v1/auth/login",
        json={"username": user["username"], "password": user["password"]},
    )
    assert response.status_code == 200
    return response.json()["data"]["csrf_token"]


async def _viewer(db_session: object) -> dict[str, str]:
    from sqlalchemy import select

    from pharmaos_api.models import Role, User

    session = db_session
    role = (await session.execute(select(Role).where(Role.code == "viewer"))).scalar_one()
    user = User(
        username=f"viewer_backup_{uuid.uuid4().hex[:10]}",
        full_name="Viewer",
        password_hash=hash_password("View3r@pass!"),
        role_id=role.id,
    )
    session.add(user)
    await session.commit()
    return {"username": user.username, "password": "View3r@pass!"}


async def test_create_requires_csrf_and_uses_owner_permission(
    client: httpx.AsyncClient,
    seeded_user: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    csrf = await _login(client, seeded_user)
    result = {
        "file_name": "pharmaos_test.pharmaos-backup",
        "size_bytes": 123,
        "sha256": "a" * 64,
        "secondary_file_name": None,
        "cloud_uploaded": False,
        "pruned_count": 0,
    }
    monkeypatch.setattr(backup_router, "_run_manual_backup", lambda: result)

    missing_csrf = await client.post("/api/v1/system/backup/create")
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["error"]["code"] == "E-AUTH-004"

    response = await client.post(
        "/api/v1/system/backup/create",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    assert response.json()["data"] == result


async def test_create_denies_non_owner(
    client: httpx.AsyncClient,
    db_session: object,
) -> None:
    viewer = await _viewer(db_session)
    csrf = await _login(client, viewer)
    response = await client.post(
        "/api/v1/system/backup/create",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "E-AUTH-002"


async def test_restore_requires_owner_reauthentication(
    client: httpx.AsyncClient,
    seeded_user: dict[str, str],
) -> None:
    csrf = await _login(client, seeded_user)
    response = await client.post(
        "/api/v1/system/backup/restore",
        headers={"X-CSRF-Token": csrf},
        data={"password": "wrong", "confirmation": "RESTORE_BACKUP"},
        files={"file": ("selected.pharmaos-backup", b"not-a-backup", "application/octet-stream")},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "E-BKP-004"


async def test_restore_validates_confirmation_before_touching_backup(
    client: httpx.AsyncClient,
    seeded_user: dict[str, str],
) -> None:
    csrf = await _login(client, seeded_user)
    response = await client.post(
        "/api/v1/system/backup/restore",
        headers={"X-CSRF-Token": csrf},
        data={"password": seeded_user["password"], "confirmation": "wrong"},
        files={"file": ("selected.pharmaos-backup", b"not-a-backup", "application/octet-stream")},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "E-BKP-003"


async def test_restore_success_clears_session_and_records_upload_metadata(
    client: httpx.AsyncClient,
    seeded_user: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    csrf = await _login(client, seeded_user)
    monkeypatch.setattr(
        backup_router,
        "_run_restore",
        lambda _path: {"verification": {"users": 1}, "migrations": {"applied": []}},
    )

    response = await client.post(
        "/api/v1/system/backup/restore",
        headers={"X-CSRF-Token": csrf},
        data={"password": seeded_user["password"], "confirmation": "RESTORE_BACKUP"},
        files={
            "file": ("selected.pharmaos-backup", b"fake-encrypted-data", "application/octet-stream")
        },
    )
    assert response.status_code == 200
    assert response.json()["data"]["file_name"] == "selected.pharmaos-backup"
    assert 'pharmaos_access=""' in response.headers.get("set-cookie", "")


async def test_maintenance_gate_rejects_new_requests(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(middleware, "is_active", lambda: True)
    blocked = await client.get("/api/v1/auth/me")
    assert blocked.status_code == 503
    assert blocked.json()["error"]["code"] == "E-BKP-005"

    health = await client.get("/api/v1/health")
    assert health.status_code == 200
