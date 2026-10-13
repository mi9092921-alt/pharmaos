"""Shelf locations M4: inventory filter/sort + POS scan enrichment + report shape.

  - `shelf` filters one EXACT slot (normalized first: b7 -> B7);
    `zone` filters one letter prefix; invalid filters are E-VAL-001.
  - `sort=location` walks shelves numerically (A2 before A10, NULLS LAST)
    and explicitly replaces the default low-stock-first order.
  - /pos/scan accepts an optional branch_id and returns the branch-scoped
    shelf_location (None when absent/unknown/never slotted) — the catalog
    stays the source of truth for the medication itself.
  - stock_level_report passes sort through; the CSV carries shelf_location.
"""

import datetime as dt
import uuid

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.models import Branch, Medication, Role, User
from pharmaos_api.security.passwords import hash_password

FUTURE = (dt.date.today() + dt.timedelta(days=365)).isoformat()


async def _seed_user(db_session: AsyncSession, role_code: str) -> str:
    role = (await db_session.execute(select(Role).where(Role.code == role_code))).scalar_one()
    username = f"{role_code}_{uuid.uuid4().hex[:8]}"
    db_session.add(
        User(username=username, full_name=f"م {role_code}",
             password_hash=hash_password("T3st@user!"), role_id=role.id)
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


async def _make_med(db_session: AsyncSession, tag: str) -> str:
    med = Medication(trade_name=f"ShelfM4 {tag} {uuid.uuid4().hex[:6]}", trade_name_ar="دواء رف")
    db_session.add(med)
    await db_session.commit()
    return str(med.id)


async def _receive(client, csrf, *, branch_id, medication_id, qty="10"):
    body = {"branch_id": branch_id, "medication_id": medication_id,
            "batch_number": f"B-{uuid.uuid4().hex[:6]}", "expiry_date": FUTURE,
            "quantity": qty, "purchase_price": "2.50"}
    r = await client.post("/api/v1/inventory/receive", headers={"X-CSRF-Token": csrf}, json=body)
    assert r.status_code == 200, r.text
    return r.json()["data"]


async def _slot(client, csrf, branch_id, med_id, shelf):
    r = await client.patch(
        "/api/v1/inventory/settings", headers={"X-CSRF-Token": csrf},
        json={"branch_id": branch_id, "medication_id": med_id, "shelf_location": shelf},
    )
    assert r.status_code == 200, r.text


async def _inv(client, branch_id, **params):
    r = await client.get("/api/v1/inventory", params={"branch_id": branch_id, **params})
    assert r.status_code == 200, r.text
    return r.json()["data"]


# ------------------------------ filters ------------------------------


async def test_shelf_filter_exact_and_normalized(admin, branch, db_session: AsyncSession):
    client, csrf = admin
    bid = str(branch.id)
    m1 = await _make_med(db_session, "one")
    m2 = await _make_med(db_session, "two")
    await _receive(client, csrf, branch_id=bid, medication_id=m1)
    await _receive(client, csrf, branch_id=bid, medication_id=m2)
    await _slot(client, csrf, bid, m1, "B7")
    await _slot(client, csrf, bid, m2, "A3")

    exact = await _inv(client, bid, shelf="B7")
    assert [x["medication_id"] for x in exact] == [m1]
    # lowercase filter input is normalized first, not rejected
    assert [x["medication_id"] for x in await _inv(client, bid, shelf="b7")] == [m1]
    assert await _inv(client, bid, shelf="C9") == []


async def test_shelf_filter_rejects_invalid(admin, branch, db_session: AsyncSession):
    client, csrf = admin
    bid = str(branch.id)
    r = await client.get("/api/v1/inventory", params={"branch_id": bid, "shelf": "A0"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "E-VAL-001"


async def test_zone_filter_prefix(admin, branch, db_session: AsyncSession):
    client, csrf = admin
    bid = str(branch.id)
    m1 = await _make_med(db_session, "one")
    m2 = await _make_med(db_session, "two")
    m3 = await _make_med(db_session, "three")
    for m in (m1, m2, m3):
        await _receive(client, csrf, branch_id=bid, medication_id=m)
    await _slot(client, csrf, bid, m1, "B7")
    await _slot(client, csrf, bid, m2, "B12")
    await _slot(client, csrf, bid, m3, "A3")

    got = {x["medication_id"] for x in await _inv(client, bid, zone="B")}
    assert got == {m1, m2}
    got_lower = {x["medication_id"] for x in await _inv(client, bid, zone="b")}
    assert got_lower == {m1, m2}
    for bad in ("1", "AB", ""):
        r = await client.get("/api/v1/inventory", params={"branch_id": bid, "zone": bad})
        assert r.status_code == 422, (bad, r.text)


# ------------------------------ sort ------------------------------


async def test_sort_location_numeric_nulls_last(admin, branch, db_session: AsyncSession):
    client, csrf = admin
    bid = str(branch.id)
    ids = {}
    for tag, shelf in (("a10", "A10"), ("a2", "A2"), ("b1", "B1"), ("noslot", None)):
        m = await _make_med(db_session, tag)
        await _receive(client, csrf, branch_id=bid, medication_id=m)
        if shelf:
            await _slot(client, csrf, bid, m, shelf)
        ids[tag] = m
    rows = await _inv(client, bid, sort="location", limit="100")
    order = [x["medication_id"] for x in rows]
    assert order == [ids["a2"], ids["a10"], ids["b1"], ids["noslot"]]


async def test_sort_location_replaces_low_stock_first(admin, branch, db_session: AsyncSession):
    client, csrf = admin
    bid = str(branch.id)
    low = await _make_med(db_session, "low")
    ok = await _make_med(db_session, "ok")
    await _receive(client, csrf, branch_id=bid, medication_id=low, qty="5")
    await _receive(client, csrf, branch_id=bid, medication_id=ok, qty="50")
    await _slot(client, csrf, bid, low, "Z9")
    await _slot(client, csrf, bid, ok, "A1")
    # low-stock item gets a reorder point so the default sort flags it first
    r = await client.patch(
        "/api/v1/inventory/settings", headers={"X-CSRF-Token": csrf},
        json={"branch_id": bid, "medication_id": low, "reorder_point": "100"},
    )
    assert r.status_code == 200, r.text

    default = await _inv(client, bid, limit="100")
    assert default[0]["medication_id"] == low  # low-stock-first preserved by default
    walked = await _inv(client, bid, sort="location", limit="100")
    assert [x["medication_id"] for x in walked] == [ok, low]


async def test_sort_rejects_unknown(admin, branch):
    client, _csrf = admin
    r = await client.get("/api/v1/inventory",
                         params={"branch_id": str(branch.id), "sort": "price"})
    assert r.status_code == 422


# ------------------------------ POS scan ------------------------------


async def _make_barcoded_med(db_session: AsyncSession):
    from decimal import Decimal

    from sqlalchemy import text as _text

    from pharmaos_api.models import MedicationBarcode, MedicationPackaging

    unit_id = (
        await db_session.execute(
            _text(
                "INSERT INTO units (name_ar) VALUES ('شريط') "
                "ON CONFLICT (name_ar) DO UPDATE SET name_ar=EXCLUDED.name_ar RETURNING id"
            )
        )
    ).scalar_one()
    await db_session.commit()  # release upsert lock (cross-session FK checks)

    code = f"621{uuid.uuid4().int % 10**10:010d}"
    med = Medication(trade_name=f"Scan {uuid.uuid4().hex[:6]}", trade_name_ar="دواء مسح")
    db_session.add(med)
    await db_session.flush()
    strip = MedicationPackaging(
        medication_id=med.id, level=2, unit_id=unit_id, name_ar="شريط",
        qty_in_parent=Decimal(3), selling_price=Decimal("30.00"), is_default_sale=True,
    )
    db_session.add(strip)
    await db_session.flush()
    db_session.add(MedicationBarcode(medication_id=med.id, packaging_id=strip.id, barcode=code))
    await db_session.commit()
    return str(med.id), code


async def test_scan_branch_shelf_enrichment(admin, branch, db_session: AsyncSession):
    client, csrf = admin
    bid = str(branch.id)
    med_id, code = await _make_barcoded_med(db_session)
    await _receive(client, csrf, branch_id=bid, medication_id=med_id)
    await _slot(client, csrf, bid, med_id, "B7")

    bare = await client.get("/api/v1/pos/scan", params={"barcode": code})
    assert bare.status_code == 200, bare.text
    assert bare.json()["data"]["shelf_location"] is None

    enriched = await client.get("/api/v1/pos/scan", params={"barcode": code, "branch_id": bid})
    assert enriched.status_code == 200, enriched.text
    assert enriched.json()["data"]["shelf_location"] == "B7"

    # unknown branch never breaks the hot scan path
    ghost = await client.get(
        "/api/v1/pos/scan",
        params={"barcode": code, "branch_id": str(uuid.uuid4())},
    )
    assert ghost.status_code == 200
    assert ghost.json()["data"]["shelf_location"] is None


# ------------------------------ report passthrough ------------------------------


async def test_stock_level_report_sort_and_csv_shelf(admin, branch, db_session: AsyncSession):
    client, csrf = admin
    bid = str(branch.id)
    m1 = await _make_med(db_session, "one")
    m2 = await _make_med(db_session, "two")
    await _receive(client, csrf, branch_id=bid, medication_id=m1)
    await _receive(client, csrf, branch_id=bid, medication_id=m2)
    await _slot(client, csrf, bid, m1, "B7")
    await _slot(client, csrf, bid, m2, "A2")

    await _login(client, await _seed_user(db_session, "branch_manager"))
    rep = await client.get("/api/v1/reports/inventory/stock-level",
                           params={"branch_id": bid, "sort": "location"})
    assert rep.status_code == 200, rep.text
    items = rep.json()["data"]["items"]
    assert [(x["shelf_location"]) for x in items] == ["A2", "B7"]
