"""Medications catalog (P1-M5): GS1 parsing, CRUD + permission tiers, Arabic
search (FTS + trigram fallback, <100ms), price history, and delete audit."""

import datetime as dt
import uuid

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.gs1 import GS, Gs1ParseError, parse_gs1
from pharmaos_api.models import Role, User
from pharmaos_api.security.passwords import hash_password

# ------------------------------ GS1 parser ------------------------------


def test_gs1_full_code_any_order() -> None:
    code = f"]d2010622400000001717270830" f"10ABC123{GS}21SER0001"
    pack = parse_gs1(code)
    assert pack.gtin == "06224000000017"
    assert pack.expiry_date == dt.date(2027, 8, 30)
    assert pack.batch_number == "ABC123"
    assert pack.serial_number == "SER0001"

    reordered = f"10ABC123{GS}21SER0001{GS}" f"0106224000000017" f"17270830"
    pack2 = parse_gs1(reordered)
    assert (pack2.gtin, pack2.batch_number, pack2.serial_number) == (
        pack.gtin,
        pack.batch_number,
        pack.serial_number,
    )


def test_gs1_day_zero_means_end_of_month() -> None:
    pack = parse_gs1("01" + "06224000000017" + "17" + "270200")  # day 00
    assert pack.expiry_date == dt.date(2027, 2, 28)


def test_gs1_rejects_malformed() -> None:
    with pytest.raises(Gs1ParseError):
        parse_gs1("99XXXX")  # unsupported AI
    with pytest.raises(Gs1ParseError):
        parse_gs1("0106224")  # truncated GTIN
    with pytest.raises(Gs1ParseError):
        parse_gs1(f"10ABC{GS}10DEF")  # repeated AI


# ------------------------------ API fixtures ------------------------------


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


async def _unit_id(db_session: AsyncSession, name: str = "قرص") -> str:
    uid = (
        await db_session.execute(
            text(
                "INSERT INTO units (name_ar) VALUES (:n) "
                "ON CONFLICT (name_ar) DO UPDATE SET name_ar=EXCLUDED.name_ar RETURNING id"
            ).bindparams(n=name)
        )
    ).scalar_one()
    # COMMIT releases the upsert's row lock — the app writes from ANOTHER session
    # whose FK check on units would otherwise block on our open transaction.
    await db_session.commit()
    return str(uid)


# ------------------------------ CRUD + search ------------------------------


async def test_create_search_arabic_fts_and_trigram(admin, db_session: AsyncSession):
    client, csrf = admin
    marker = uuid.uuid4().hex[:6]
    r = await client.post(
        "/api/v1/medications",
        headers={"X-CSRF-Token": csrf},
        json={"trade_name": f"Congestal {marker}", "trade_name_ar": "كونجيستال"},
    )
    assert r.status_code == 200, r.text

    # FTS: hamza/taa variations must match through normalize_arabic
    r2 = await client.get("/api/v1/medications", params={"search": "كونجيستال"})
    names = [m["trade_name_ar"] for m in r2.json()["data"]]
    assert "كونجيستال" in names
    # The < 100ms search target is guaranteed structurally by the GIN index
    # (test_query_plans.py asserts the plan) and verified end-to-end on the
    # target device (docs/pilot-checklist.md) — never a wall-clock assert on
    # a shared CI runner, which is variable and produces false failures.

    # trigram fallback: typo (missing letter) still finds it
    r3 = await client.get("/api/v1/medications", params={"search": "كونجستال"})
    assert any(m["trade_name_ar"] == "كونجيستال" for m in r3.json()["data"])


async def test_packaging_price_change_writes_history(admin, db_session: AsyncSession):
    client, csrf = admin
    unit = await _unit_id(db_session)
    med = (
        await client.post(
            "/api/v1/medications",
            headers={"X-CSRF-Token": csrf},
            json={"trade_name": f"Priced {uuid.uuid4().hex[:6]}"},
        )
    ).json()["data"]

    body = {
        "levels": [
            {
                "level": 3,
                "unit_id": unit,
                "name_ar": "قرص",
                "qty_in_parent": "10",
                "selling_price": "3.50",
                "is_sellable": True,
                "is_default_sale": True,
            }
        ],
        "price_source": "manual",
    }
    r1 = await client.put(
        f"/api/v1/medications/{med['id']}/packaging", headers={"X-CSRF-Token": csrf}, json=body
    )
    assert r1.status_code == 200, r1.text

    body["levels"][0]["selling_price"] = "4.00"
    body["price_source"] = "provider"
    r2 = await client.put(
        f"/api/v1/medications/{med['id']}/packaging", headers={"X-CSRF-Token": csrf}, json=body
    )
    assert r2.status_code == 200
    assert r2.json()["data"][0]["selling_price"] == "4.00"
    assert r2.json()["data"][0]["price_source"] == "provider"

    rows = (
        await db_session.execute(
            text(
                "SELECT old_price, new_price, price_source FROM medication_price_history "
                "WHERE medication_id = CAST(:m AS uuid) ORDER BY created_at"
            ).bindparams(m=med["id"])
        )
    ).all()
    assert len(rows) == 2
    assert rows[0][0] is None and str(rows[0][1]) == "3.50"  # first price
    assert str(rows[1][0]) == "3.50" and str(rows[1][1]) == "4.00" and rows[1][2] == "provider"


async def test_duplicate_barcode_rejected(admin):
    client, csrf = admin
    med1 = (
        await client.post(
            "/api/v1/medications",
            headers={"X-CSRF-Token": csrf},
            json={"trade_name": f"A {uuid.uuid4().hex[:6]}"},
        )
    ).json()["data"]
    med2 = (
        await client.post(
            "/api/v1/medications",
            headers={"X-CSRF-Token": csrf},
            json={"trade_name": f"B {uuid.uuid4().hex[:6]}"},
        )
    ).json()["data"]
    barcode = f"629{uuid.uuid4().int % 10**10:010d}"
    ok = await client.post(
        f"/api/v1/medications/{med1['id']}/barcodes",
        headers={"X-CSRF-Token": csrf},
        json={"barcode": barcode},
    )
    assert ok.status_code == 200
    dup = await client.post(
        f"/api/v1/medications/{med2['id']}/barcodes",
        headers={"X-CSRF-Token": csrf},
        json={"barcode": barcode},
    )
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "E-CAT-001"


async def test_delete_audits_and_hides(admin, db_session: AsyncSession):
    client, csrf = admin
    med = (
        await client.post(
            "/api/v1/medications",
            headers={"X-CSRF-Token": csrf},
            json={"trade_name": f"Del {uuid.uuid4().hex[:6]}"},
        )
    ).json()["data"]
    r = await client.delete(f"/api/v1/medications/{med['id']}", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    assert (await client.get(f"/api/v1/medications/{med['id']}")).status_code == 404
    audited = (
        await db_session.execute(
            text(
                "SELECT COUNT(*) FROM audit_logs WHERE action='medication.deleted' "
                "AND entity_id = CAST(:m AS uuid)"
            ).bindparams(m=med["id"])
        )
    ).scalar_one()
    assert audited == 1


async def test_gs1_endpoint_resolves_gtin(admin):
    client, csrf = admin
    gtin = f"0622{uuid.uuid4().int % 10**10:010d}"
    await client.post(
        "/api/v1/medications",
        headers={"X-CSRF-Token": csrf},
        json={"trade_name": f"G {uuid.uuid4().hex[:6]}", "gtin": gtin},
    )
    code = f"01{gtin}17280630"
    r = await client.get("/api/v1/catalog/parse-gs1", params={"code": code})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["gtin"] == gtin
    assert data["expiry_date"] == "2028-06-30"
    assert data["medication"] is not None and data["medication"]["gtin"] == gtin


async def test_permission_tiers(client: httpx.AsyncClient, db_session: AsyncSession):
    """data_entry may add (inventory.add) but not edit (inventory.edit)."""
    csrf = await _login(client, await _seed_user(db_session, "data_entry"))
    created = await client.post(
        "/api/v1/medications",
        headers={"X-CSRF-Token": csrf},
        json={"trade_name": f"DE {uuid.uuid4().hex[:6]}"},
    )
    assert created.status_code == 200  # inventory.add includes data_entry
    r = await client.patch(
        f"/api/v1/medications/{created.json()['data']['id']}",
        headers={"X-CSRF-Token": csrf},
        json={"manufacturer": "x"},
    )
    assert r.status_code == 403  # inventory.edit excludes data_entry
    assert r.json()["error"]["code"] == "E-AUTH-002"
