"""Shelf locations M2: PATCH /inventory/settings contract + receive set-if-NULL.

Covers the ratified v4/v5 decisions against a real PostgreSQL 17:
  - absent key = no change; explicit null/"" = clear; raw input normalized
    FIRST (a-3 -> A3) then validated (E-VAL-001, never a silent drop);
  - every shelf change audited with old/new in the same transaction;
  - no quantity or stock_movement side effects (drift stays clean);
  - inventory.edit gate + CSRF on the mutation;
  - receive writes the slot only when it is NULL, never overwrites/clears;
  - service normalizer matches packages/shared/src/shelf_location_vectors.json.
"""

import datetime as dt
import json
import os
import uuid

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.errors import ApiError
from pharmaos_api.models import Branch, Medication, Role, User
from pharmaos_api.security.passwords import hash_password
from pharmaos_api.services.inventory_service import normalize_shelf_location

FUTURE = (dt.date.today() + dt.timedelta(days=365)).isoformat()
SETTINGS = "/api/v1/inventory/settings"


async def _seed_user(db_session: AsyncSession, role_code: str) -> str:
    role = (await db_session.execute(select(Role).where(Role.code == role_code))).scalar_one()
    username = f"{role_code}_{uuid.uuid4().hex[:8]}"
    db_session.add(
        User(
            username=username,
            full_name=f"م {role_code}",
            password_hash=hash_password("T3st@user!"),
            role_id=role.id,
        )
    )
    await db_session.commit()
    return username


async def _login(client: httpx.AsyncClient, username: str) -> str:
    r = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": "T3st@user!"}
    )
    assert r.status_code == 200, r.text
    return r.json()["data"]["csrf_token"]


@pytest.fixture
async def admin(client: httpx.AsyncClient, db_session: AsyncSession):
    csrf = await _login(client, await _seed_user(db_session, "super_admin"))
    return client, csrf


@pytest.fixture
async def branch(db_session: AsyncSession) -> Branch:
    b = Branch(name=f"فرع {uuid.uuid4().hex[:6]}", country_code="EG", currency_code="EGP")
    db_session.add(b)
    await db_session.commit()
    return b


async def _make_med(db_session: AsyncSession) -> str:
    med = Medication(trade_name=f"ShelfM2 {uuid.uuid4().hex[:6]}", trade_name_ar="دواء رف")
    db_session.add(med)
    await db_session.commit()
    return str(med.id)


async def _receive(client, csrf, *, branch_id, medication_id, qty="100", **extra):
    body = {
        "branch_id": branch_id,
        "medication_id": medication_id,
        "batch_number": f"B-{uuid.uuid4().hex[:6]}",
        "expiry_date": FUTURE,
        "quantity": qty,
        "purchase_price": "2.50",
        **extra,
    }
    return await client.post("/api/v1/inventory/receive", headers={"X-CSRF-Token": csrf}, json=body)


async def _patch(client, csrf, *, branch_id, medication_id, payload):
    return await client.patch(
        SETTINGS,
        headers={"X-CSRF-Token": csrf},
        json={"branch_id": branch_id, "medication_id": medication_id, **payload},
    )


async def _shelf(client, branch_id, med_id):
    inv = await client.get("/api/v1/inventory", params={"branch_id": branch_id})
    assert inv.status_code == 200, inv.text
    row = next(x for x in inv.json()["data"] if x["medication_id"] == med_id)
    return row["shelf_location"]


async def _movements(db_session: AsyncSession, branch_id) -> int:
    return (
        await db_session.execute(
            text("SELECT COUNT(*) FROM stock_movements WHERE branch_id = :b").bindparams(
                b=branch_id
            )
        )
    ).scalar_one()


# ------------------------------ PATCH contract ------------------------------


async def test_patch_absent_key_keeps_value(admin, branch, db_session: AsyncSession):
    client, csrf = admin
    med_id = await _make_med(db_session)
    bid = str(branch.id)
    r = await _patch(
        client, csrf, branch_id=bid, medication_id=med_id, payload={"shelf_location": "B7"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["shelf_location"] == "B7"

    # reorder-only payload carries NO shelf key -> slot must survive
    r2 = await _patch(
        client, csrf, branch_id=bid, medication_id=med_id, payload={"reorder_point": "10"}
    )
    assert r2.status_code == 200, r2.text
    assert await _shelf(client, bid, med_id) == "B7"


async def test_patch_clear_variants(admin, branch, db_session: AsyncSession):
    client, csrf = admin
    bid = str(branch.id)
    for payload in ({"shelf_location": None}, {"shelf_location": ""}):
        med_id = await _make_med(db_session)
        ok = await _patch(
            client, csrf, branch_id=bid, medication_id=med_id, payload={"shelf_location": "B7"}
        )
        assert ok.status_code == 200, ok.text
        clr = await _patch(client, csrf, branch_id=bid, medication_id=med_id, payload=payload)
        assert clr.status_code == 200, clr.text
        assert clr.json()["data"]["shelf_location"] is None
        assert await _shelf(client, bid, med_id) is None


async def test_patch_normalizes_before_validating(admin, branch, db_session: AsyncSession):
    client, csrf = admin
    med_id = await _make_med(db_session)
    bid = str(branch.id)
    r = await _patch(
        client, csrf, branch_id=bid, medication_id=med_id, payload={"shelf_location": "a-3"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["shelf_location"] == "A3"


async def test_patch_rejects_invalid_unchanged(admin, branch, db_session: AsyncSession):
    client, csrf = admin
    med_id = await _make_med(db_session)
    bid = str(branch.id)
    for bad in ("A0", "A01", "أ1", "AB12", "A100"):
        r = await _patch(
            client, csrf, branch_id=bid, medication_id=med_id, payload={"shelf_location": bad}
        )
        assert r.status_code == 422, (bad, r.text)
        assert r.json()["error"]["code"] == "E-VAL-001"
    # rejected writes must not even create a cache row
    inv = await client.get("/api/v1/inventory", params={"branch_id": bid})
    assert inv.status_code == 200
    assert all(x["medication_id"] != med_id for x in inv.json()["data"])


async def test_patch_audit_carries_old_and_new(admin, branch, db_session: AsyncSession):
    client, csrf = admin
    med_id = await _make_med(db_session)
    bid = str(branch.id)
    assert (
        await _patch(
            client, csrf, branch_id=bid, medication_id=med_id, payload={"shelf_location": "B7"}
        )
    ).status_code == 200
    mv = await _patch(
        client, csrf, branch_id=bid, medication_id=med_id, payload={"shelf_location": "B8"}
    )
    assert mv.status_code == 200, mv.text
    rows = (
        await db_session.execute(
            text(
                "SELECT metadata FROM audit_logs WHERE action = 'settings.changed' "
                "AND entity_id = :m AND metadata->>'scope' = 'stock_settings' "
                "ORDER BY created_at DESC LIMIT 1"
            ).bindparams(m=uuid.UUID(med_id))
        )
    ).all()
    assert rows, "expected an audit row for the shelf move"
    meta = rows[0][0]
    assert meta["shelf_location_old"] == "B7"
    assert meta["shelf_location"] == "B8"


async def test_patch_has_no_quantity_side_effects(admin, branch, db_session: AsyncSession):
    client, csrf = admin
    med_id = await _make_med(db_session)
    bid = str(branch.id)
    await _receive(client, csrf, branch_id=bid, medication_id=med_id, qty="50")
    before = await _movements(db_session, branch.id)
    r = await _patch(
        client, csrf, branch_id=bid, medication_id=med_id, payload={"shelf_location": "C2"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["cached_quantity"] in ("50.000", "50")
    assert await _movements(db_session, branch.id) == before
    drift = await client.get("/api/v1/inventory/drift", params={"branch_id": bid})
    assert drift.json()["data"]["ok"] is True


async def test_patch_permission_and_csrf(client, branch, db_session: AsyncSession):
    admin_name = await _seed_user(db_session, "super_admin")
    admin_csrf = await _login(client, admin_name)
    med_id = await _make_med(db_session)
    body = {"branch_id": str(branch.id), "medication_id": med_id, "shelf_location": "B7"}

    # cashier holds inventory.view but NOT inventory.edit
    cashier_csrf = await _login(client, await _seed_user(db_session, "cashier"))
    denied = await client.patch(SETTINGS, headers={"X-CSRF-Token": cashier_csrf}, json=body)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "E-AUTH-002"

    # CSRF gate fires for an authorized caller without the header
    # (re-login so the session cookie carries inventory.edit).
    admin_csrf = await _login(client, admin_name)
    no_csrf = await client.patch(SETTINGS, json=body)
    assert no_csrf.status_code == 403
    assert no_csrf.json()["error"]["code"] == "E-AUTH-004"

    ok = await client.patch(SETTINGS, headers={"X-CSRF-Token": admin_csrf}, json=body)
    assert ok.status_code == 200, ok.text


# ------------------------------ receive set-if-NULL ------------------------------


async def test_receive_sets_shelf_when_null(admin, branch, db_session: AsyncSession):
    client, csrf = admin
    med_id = await _make_med(db_session)
    bid = str(branch.id)
    r = await _receive(
        client, csrf, branch_id=bid, medication_id=med_id, qty="20", shelf_location="c2"
    )
    assert r.status_code == 200, r.text
    assert await _shelf(client, bid, med_id) == "C2"


async def test_receive_never_overwrites_or_clears(admin, branch, db_session: AsyncSession):
    client, csrf = admin
    med_id = await _make_med(db_session)
    bid = str(branch.id)
    assert (
        await _patch(
            client, csrf, branch_id=bid, medication_id=med_id, payload={"shelf_location": "B7"}
        )
    ).status_code == 200
    # second receipt with a different slot must NOT move it ...
    r = await _receive(
        client, csrf, branch_id=bid, medication_id=med_id, qty="5", shelf_location="C2"
    )
    assert r.status_code == 200, r.text
    assert await _shelf(client, bid, med_id) == "B7"
    # ... and a blank/absent slot must NOT clear it either
    r2 = await _receive(
        client, csrf, branch_id=bid, medication_id=med_id, qty="5", shelf_location=""
    )
    assert r2.status_code == 200, r2.text
    assert await _shelf(client, bid, med_id) == "B7"


async def test_receive_rejects_invalid_shelf(admin, branch, db_session: AsyncSession):
    client, csrf = admin
    med_id = await _make_med(db_session)
    r = await _receive(
        client, csrf, branch_id=str(branch.id), medication_id=med_id, shelf_location="A0"
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "E-VAL-001"


# ------------------------------ vectors parity ------------------------------


def test_normalizer_matches_shared_vectors():
    here = os.path.dirname(os.path.abspath(__file__))
    raw = os.path.join(
        here, "..", "..", "..", "packages", "shared", "src", "shelf_location_vectors.json"
    )
    with open(raw, encoding="utf-8") as fh:
        spec = json.load(fh)
    assert spec["spec"]["final_pattern"].startswith("^[A-Z][1-9][0-9]?$")
    for v in spec["vectors"]:
        if v["verdict"] == "reject":
            try:
                normalize_shelf_location(v["in"])
            except ApiError as e:
                assert e.code == "E-VAL-001"
            else:
                raise AssertionError(f"vector should reject: {v['in']!r}")
        else:
            assert normalize_shelf_location(v["in"]) == v["out"], v
