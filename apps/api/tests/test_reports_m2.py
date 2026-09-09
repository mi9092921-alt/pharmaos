"""Inventory reports (P3-M2): stock level, valuation, movement analysis.

Covers the inventory_service report additions (stock_level_report reusing
list_inventory + branch summary counts, inventory_valuation_report reusing
batch_status_report's totals, movement_report's by-type breakdown + fast/slow
movers) and the HTTP layer (reports.inventory / reports.export permission
tiers — note reports.inventory, unlike reports.sales, includes pharmacist).

Each test uses its OWN branch (the `branch` fixture) so branch-scoped reports
are isolated from batches/movements other tests leave in the shared test DB.
"""

import datetime as dt
import uuid
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.errors import ApiError
from pharmaos_api.models import (
    Branch,
    Medication,
    MedicationBarcode,
    MedicationBatch,
    MedicationPackaging,
    Role,
    User,
)
from pharmaos_api.services import inventory_service, sales_service
from pharmaos_api.services.sales_service import SaleLine


@pytest.fixture
async def actor(db_session: AsyncSession, seeded_user: dict) -> User:
    return (
        await db_session.execute(select(User).where(User.username == seeded_user["username"]))
    ).scalar_one()


@pytest.fixture
async def branch(db_session: AsyncSession) -> Branch:
    b = Branch(name=f"فرع {uuid.uuid4().hex[:6]}", country_code="EG", currency_code="EGP")
    db_session.add(b)
    await db_session.commit()
    return b


async def _stock_med(
    db_session: AsyncSession,
    actor: User,
    branch_id: uuid.UUID,
    *,
    tablets: int = 1000,
    purchase_price: Decimal = Decimal("2.00"),
) -> uuid.UUID:
    """Strip-barcode med (10 tablets/strip), RECEIVED via
    inventory_service.receive_stock (not a bare INSERT) so branch_inventory's
    cache is populated exactly as production receiving would — required for
    the stock-level/valuation reports, which read that cache."""
    unit_id = (
        await db_session.execute(
            text(
                "INSERT INTO units (name_ar) VALUES ('شريط') "
                "ON CONFLICT (name_ar) DO UPDATE SET name_ar=EXCLUDED.name_ar RETURNING id"
            )
        )
    ).scalar_one()
    await db_session.commit()
    med = Medication(trade_name=f"InvMed {uuid.uuid4().hex[:6]}", trade_name_ar="دواء المخزون")
    db_session.add(med)
    await db_session.flush()
    strip = MedicationPackaging(
        medication_id=med.id,
        level=2,
        unit_id=unit_id,
        name_ar="شريط",
        qty_in_parent=Decimal(10),
        selling_price=Decimal("30.00"),
        is_default_sale=True,
    )
    db_session.add(strip)
    await db_session.flush()
    barcode = f"622{uuid.uuid4().int % 10**10:010d}"
    db_session.add(MedicationBarcode(medication_id=med.id, packaging_id=strip.id, barcode=barcode))
    await db_session.commit()

    await inventory_service.receive_stock(
        db_session,
        actor=actor,
        branch_id=branch_id,
        medication_id=med.id,
        batch_number=f"INV-{uuid.uuid4().hex[:8]}",
        expiry_date=dt.date.today() + dt.timedelta(days=365),
        quantity=Decimal(tablets),
        purchase_price=purchase_price,
    )
    return med.id


async def _set_reorder_point(
    db_session: AsyncSession, branch_id: uuid.UUID, medication_id: uuid.UUID, value: int
) -> None:
    # No write path exists for reorder_point yet (a pre-existing gap outside
    # M2's scope) — set it directly, same convention _backdate_invoice (M1)
    # uses for a field with no service writer.
    await db_session.execute(
        text(
            "UPDATE branch_inventory SET reorder_point = :v "
            "WHERE branch_id = :b AND medication_id = :m"
        ).bindparams(v=value, b=branch_id, m=medication_id)
    )
    await db_session.commit()


# ------------------------------ service: stock level ------------------------------


async def test_stock_level_report_flags_and_summary(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    ok_med = await _stock_med(db_session, actor, branch.id, tablets=1000)
    low_med = await _stock_med(db_session, actor, branch.id, tablets=50)
    await _set_reorder_point(db_session, branch.id, low_med, 100)
    out_med = await _stock_med(db_session, actor, branch.id, tablets=10)  # 10 strips received
    await _set_reorder_point(db_session, branch.id, out_med, 20)
    barcode = (
        await db_session.execute(
            select(MedicationBarcode.barcode).where(MedicationBarcode.medication_id == out_med)
        )
    ).scalar_one()
    await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(quantity=Decimal(10), barcode=barcode)],
        cashier=actor,
    )  # sells every strip received -> cached_quantity 0

    report = await inventory_service.stock_level_report(db_session, branch_id=branch.id)
    by_med = {i["medication_id"]: i for i in report["items"]}
    assert by_med[str(ok_med)]["status"] == "ok"
    assert by_med[str(low_med)]["status"] == "low_stock"
    assert by_med[str(out_med)]["status"] == "out_of_stock"

    summary = report["summary"]
    assert summary["total_skus"] == 3
    assert summary["low_stock_count"] == 1
    assert summary["out_of_stock_count"] == 1


async def test_stock_level_report_low_stock_only_filter(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    ok_med = await _stock_med(db_session, actor, branch.id, tablets=1000)
    low_med = await _stock_med(db_session, actor, branch.id, tablets=50)
    await _set_reorder_point(db_session, branch.id, low_med, 100)

    report = await inventory_service.stock_level_report(
        db_session, branch_id=branch.id, low_stock_only=True
    )
    ids = {i["medication_id"] for i in report["items"]}
    assert ids == {str(low_med)}
    assert str(ok_med) not in ids
    # Summary always covers the FULL branch, not just the filtered page.
    assert report["summary"]["total_skus"] == 2


# ------------------------------ service: valuation ------------------------------


async def test_inventory_valuation_report_totals_and_ranking(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_x = await _stock_med(
        db_session, actor, branch.id, tablets=1000, purchase_price=Decimal("5.00")
    )
    med_y = await _stock_med(
        db_session, actor, branch.id, tablets=500, purchase_price=Decimal("2.00")
    )
    # Locked (quarantined) capital for med_x — must NOT count toward its
    # active valuation, but MUST count toward locked_value.
    db_session.add(
        MedicationBatch(
            branch_id=branch.id,
            medication_id=med_x,
            batch_number=f"Q-{uuid.uuid4().hex[:8]}",
            expiry_date=dt.date.today() + dt.timedelta(days=180),
            quantity=Decimal(300),
            purchase_price=Decimal("5.00"),
            status="quarantined",
        )
    )
    await db_session.commit()

    report = await inventory_service.inventory_valuation_report(db_session, branch_id=branch.id)
    items = report["items"]
    assert isinstance(items, list) and len(items) == 2
    # Ranked by value DESC: X (1000*5=5000.00) before Y (500*2=1000.00).
    assert items[0]["medication_id"] == str(med_x)
    assert items[0]["quantity"] == "1000.000"
    assert items[0]["value"] == "5000.00"
    assert items[1]["medication_id"] == str(med_y)
    assert items[1]["value"] == "1000.00"

    totals = report["totals"]
    assert totals["sellable_value"] == "6000.00"  # active only: 5000 + 1000
    assert totals["locked_value"] == "1500.00"  # the quarantined 300 * 5.00


# ------------------------------ service: movement ------------------------------


async def test_movement_report_by_type_and_movers(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    fast_med = await _stock_med(db_session, actor, branch.id, tablets=1000)
    slow_med = await _stock_med(db_session, actor, branch.id, tablets=1000)
    barcode = (
        await db_session.execute(
            select(MedicationBarcode.barcode).where(MedicationBarcode.medication_id == fast_med)
        )
    ).scalar_one()
    await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(quantity=Decimal(3), barcode=barcode)],  # 3 of the batch's smallest unit
        cashier=actor,
    )

    today = dt.date.today()
    report = await inventory_service.movement_report(
        db_session, branch_id=branch.id, date_from=today, date_to=today
    )
    assert report["total_movements"] == 3  # 2 receives + 1 sale
    by_type = report["by_type"]
    assert by_type["purchase_in"] == {"count": 2, "net_quantity": "2000.000"}
    assert by_type["sale_out"] == {"count": 1, "net_quantity": "-3.000"}
    assert by_type["return_in"] == {"count": 0, "net_quantity": "0.000"}

    fast = {m["medication_id"]: m for m in report["fast_movers"]}
    assert str(fast_med) in fast
    assert fast[str(fast_med)]["qty_sold"] == "3.000"
    assert str(slow_med) not in fast  # never sold -> not in the sale_out ranking at all

    slow = report["slow_movers"]
    slow_ids = [m["medication_id"] for m in slow]
    assert slow_ids[0] == str(slow_med)  # zero sales ranks lowest (first)
    assert slow[0]["qty_sold"] == "0.000"
    assert str(fast_med) in slow_ids


async def test_movement_report_rejects_reversed_range(
    db_session: AsyncSession, branch: Branch
) -> None:
    today = dt.date.today()
    with pytest.raises(ApiError):
        await inventory_service.movement_report(
            db_session, branch_id=branch.id, date_from=today, date_to=today - dt.timedelta(days=1)
        )


# ------------------------------ HTTP layer ------------------------------


async def _seed_role_user(db_session: AsyncSession, role_code: str) -> str:
    from pharmaos_api.security.passwords import hash_password

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


async def test_stock_level_report_permission_matrix(
    client: httpx.AsyncClient, db_session: AsyncSession, branch: Branch
) -> None:
    params = {"branch_id": str(branch.id)}

    # cashier: no reports.inventory -> 403.
    await _login(client, await _seed_role_user(db_session, "cashier"))
    denied = await client.get("/api/v1/reports/inventory/stock-level", params=params)
    assert denied.status_code == 403

    # pharmacist: reports.inventory DOES include pharmacist (unlike reports.sales).
    await _login(client, await _seed_role_user(db_session, "pharmacist"))
    ok_ph = await client.get("/api/v1/reports/inventory/stock-level", params=params)
    assert ok_ph.status_code == 200, ok_ph.text

    # branch_manager: allowed.
    await _login(client, await _seed_role_user(db_session, "branch_manager"))
    ok = await client.get("/api/v1/reports/inventory/stock-level", params=params)
    assert ok.status_code == 200, ok.text
    data = ok.json()["data"]
    assert set(data) >= {"items", "pagination", "summary"}
    assert data["summary"]["total_skus"] == 0  # empty branch


async def test_stock_level_csv_export_permission_and_shape(
    client: httpx.AsyncClient, db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    await _stock_med(db_session, actor, branch.id, tablets=100)
    params = {"branch_id": str(branch.id)}

    # pharmacist has reports.inventory but NOT reports.export.
    await _login(client, await _seed_role_user(db_session, "pharmacist"))
    denied = await client.get("/api/v1/reports/inventory/stock-level/export", params=params)
    assert denied.status_code == 403

    # branch_manager can export.
    await _login(client, await _seed_role_user(db_session, "branch_manager"))
    csv_resp = await client.get("/api/v1/reports/inventory/stock-level/export", params=params)
    assert csv_resp.status_code == 200, csv_resp.text
    assert csv_resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in csv_resp.headers.get("content-disposition", "")
    body = csv_resp.text
    assert body.startswith("\ufeff")
    assert "trade_name_ar,trade_name,cached_quantity,reorder_point,status" in body


async def test_inventory_valuation_and_movement_endpoints_smoke(
    client: httpx.AsyncClient, db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    await _stock_med(db_session, actor, branch.id, tablets=200, purchase_price=Decimal("3.00"))
    await _login(client, await _seed_role_user(db_session, "branch_manager"))

    val = await client.get(
        "/api/v1/reports/inventory/valuation", params={"branch_id": str(branch.id)}
    )
    assert val.status_code == 200, val.text
    val_data = val.json()["data"]
    assert set(val_data) >= {"items", "pagination", "totals"}
    assert val_data["totals"]["sellable_value"] == "600.00"

    today = dt.date.today().isoformat()
    mv = await client.get(
        "/api/v1/reports/inventory/movement",
        params={"branch_id": str(branch.id), "date_from": today, "date_to": today},
    )
    assert mv.status_code == 200, mv.text
    mv_data = mv.json()["data"]
    assert set(mv_data) >= {"by_type", "fast_movers", "slow_movers", "total_movements"}
    assert mv_data["by_type"]["purchase_in"]["count"] == 1

    # reversed range -> 422 at the HTTP layer too.
    bad = await client.get(
        "/api/v1/reports/inventory/movement",
        params={"branch_id": str(branch.id), "date_from": today, "date_to": "2020-01-01"},
    )
    assert bad.status_code == 422
