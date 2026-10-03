"""POS M8: scan with packaging levels + GS1 DataMatrix fallback, and sale lines
with explicit packaging (unit switching / name-search) — conversion, validation,
permissions, and the derived-cache invariant through the API path."""

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
async def cashier(db_session: AsyncSession, seeded_user: dict) -> User:
    return (
        await db_session.execute(select(User).where(User.username == seeded_user["username"]))
    ).scalar_one()


@pytest.fixture
async def branch(db_session: AsyncSession) -> Branch:
    b = Branch(name=f"فرع {uuid.uuid4().hex[:6]}", country_code="EG", currency_code="EGP")
    db_session.add(b)
    await db_session.commit()
    return b


async def _make_pos_med(
    db_session: AsyncSession,
    *,
    gtin: str | None = None,
    box_sellable: bool = True,
) -> dict[str, str]:
    """Med with box(90)/strip(30, default, barcode)/tablet(3.50) levels.
    Conversion: 1 box = 3 strips = 30 tablets; 1 strip = 10 tablets."""
    unit_ids = {}
    for name_ar in ("علبة", "شريط", "قرص"):
        unit_ids[name_ar] = (
            await db_session.execute(
                text(
                    "INSERT INTO units (name_ar) VALUES (:n) "
                    "ON CONFLICT (name_ar) DO UPDATE SET name_ar=EXCLUDED.name_ar RETURNING id"
                ).bindparams(n=name_ar)
            )
        ).scalar_one()
    await db_session.commit()  # release upsert lock (cross-session FK checks)

    med = Medication(
        trade_name=f"PosMed {uuid.uuid4().hex[:6]}", trade_name_ar="دواء نقطة البيع", gtin=gtin
    )
    db_session.add(med)
    await db_session.flush()
    box = MedicationPackaging(
        medication_id=med.id,
        level=1,
        unit_id=unit_ids["علبة"],
        name_ar="علبة",
        qty_in_parent=None,
        selling_price=Decimal("90.00"),
        is_sellable=box_sellable,
    )
    strip = MedicationPackaging(
        medication_id=med.id,
        level=2,
        unit_id=unit_ids["شريط"],
        name_ar="شريط",
        qty_in_parent=Decimal(3),
        selling_price=Decimal("30.00"),
        is_default_sale=True,
    )
    tablet = MedicationPackaging(
        medication_id=med.id,
        level=3,
        unit_id=unit_ids["قرص"],
        name_ar="قرص",
        qty_in_parent=Decimal(10),
        selling_price=Decimal("3.50"),
    )
    db_session.add_all([box, strip, tablet])
    await db_session.flush()
    barcode = f"622{uuid.uuid4().int % 10**10:010d}"
    db_session.add(MedicationBarcode(medication_id=med.id, packaging_id=strip.id, barcode=barcode))
    await db_session.commit()
    return {
        "med_id": str(med.id),
        "box_id": str(box.id),
        "strip_id": str(strip.id),
        "tablet_id": str(tablet.id),
        "barcode": barcode,
    }


async def _stock_tablets(
    db_session: AsyncSession, branch_id: uuid.UUID, med_id: str, tablets: int
) -> None:
    db_session.add(
        MedicationBatch(
            branch_id=branch_id,
            medication_id=uuid.UUID(med_id),
            batch_number=f"POS-{uuid.uuid4().hex[:8]}",
            expiry_date=dt.date.today() + dt.timedelta(days=365),
            quantity=Decimal(tablets),
            purchase_price=Decimal("2.00"),
        )
    )
    await db_session.commit()


# ------------------------- unit switching (service) -------------------------


async def test_sale_unit_switch_converts_quantities(
    db_session: AsyncSession, branch: Branch, cashier: User
) -> None:
    ids = await _make_pos_med(db_session)
    await _stock_tablets(db_session, branch.id, ids["med_id"], 100)

    # Scanned the STRIP barcode but sells 5 TABLETS + 1 BOX (unit switching).
    invoice = await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[
            SaleLine(
                quantity=Decimal(5),
                barcode=ids["barcode"],
                packaging_id=uuid.UUID(ids["tablet_id"]),
            ),
            SaleLine(
                quantity=Decimal(1),
                barcode=ids["barcode"],
                packaging_id=uuid.UUID(ids["box_id"]),
            ),
        ],
        cashier=cashier,
    )
    # 5 tablets x 3.50 + 1 box x 90.00
    assert invoice.total == Decimal("107.50")
    remaining = (
        await db_session.execute(
            text(
                "SELECT SUM(quantity) FROM medication_batches "
                "WHERE medication_id = CAST(:m AS uuid)"
            ).bindparams(m=ids["med_id"])
        )
    ).scalar_one()
    assert Decimal(remaining) == Decimal(65)  # 100 - 5 - 30


async def test_sale_line_by_medication_and_packaging(
    db_session: AsyncSession, branch: Branch, cashier: User
) -> None:
    """Name-search flow: no barcode — explicit medication_id + packaging_id."""
    ids = await _make_pos_med(db_session)
    await _stock_tablets(db_session, branch.id, ids["med_id"], 50)
    invoice = await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[
            SaleLine(
                quantity=Decimal(2),
                medication_id=uuid.UUID(ids["med_id"]),
                packaging_id=uuid.UUID(ids["strip_id"]),
            )
        ],
        cashier=cashier,
    )
    assert invoice.total == Decimal("60.00")  # 2 strips x 30.00 = 20 tablets deducted


async def test_sale_rejects_foreign_or_unsellable_packaging(
    db_session: AsyncSession, branch: Branch, cashier: User
) -> None:
    bid = branch.id  # plain values survive rollback-expiry of ORM objects
    ids = await _make_pos_med(db_session)
    other = await _make_pos_med(db_session)
    await _stock_tablets(db_session, bid, ids["med_id"], 50)

    # Packaging belongs to ANOTHER medication -> 422, nothing persisted.
    with pytest.raises(ApiError) as exc:
        await sales_service.create_sale(
            db_session,
            branch_id=bid,
            lines=[
                SaleLine(
                    quantity=Decimal(1),
                    barcode=ids["barcode"],
                    packaging_id=uuid.UUID(other["strip_id"]),
                )
            ],
            cashier=cashier,
        )
    assert exc.value.code == "E-VAL-001" and exc.value.http_status == 422
    await db_session.rollback()
    await db_session.refresh(cashier)  # rollback expired it too

    # Non-sellable level -> 422.
    unsellable = await _make_pos_med(db_session, box_sellable=False)
    await _stock_tablets(db_session, bid, unsellable["med_id"], 50)
    with pytest.raises(ApiError) as exc2:
        await sales_service.create_sale(
            db_session,
            branch_id=bid,
            lines=[
                SaleLine(
                    quantity=Decimal(1),
                    barcode=unsellable["barcode"],
                    packaging_id=uuid.UUID(unsellable["box_id"]),
                )
            ],
            cashier=cashier,
        )
    assert exc2.value.http_status == 422
    await db_session.rollback()


async def test_sale_line_requires_identifiers(
    db_session: AsyncSession, branch: Branch, cashier: User
) -> None:
    with pytest.raises(ApiError) as exc:
        await sales_service.create_sale(
            db_session,
            branch_id=branch.id,
            lines=[SaleLine(quantity=Decimal(1))],
            cashier=cashier,
        )
    assert exc.value.http_status == 422
    await db_session.rollback()


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


async def test_scan_endpoint_returns_all_sellable_levels(
    client: httpx.AsyncClient, db_session: AsyncSession, branch: Branch
) -> None:
    await _login(client, await _seed_role_user(db_session, "cashier"))
    ids = await _make_pos_med(db_session)

    r = await client.get("/api/v1/pos/scan", params={"barcode": ids["barcode"]})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["packaging_id"] == ids["strip_id"]  # barcode-linked level wins
    assert data["controlled_substance"] is False
    levels = data["levels"]
    assert [x["level"] for x in levels] == [1, 2, 3]
    assert [x["id"] for x in levels] == [ids["box_id"], ids["strip_id"], ids["tablet_id"]]
    assert next(x for x in levels if x["is_default_sale"])["selling_price"] == "30.00"


async def test_scan_endpoint_resolves_gs1_datamatrix(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await _login(client, await _seed_role_user(db_session, "cashier"))
    gtin = f"0624{uuid.uuid4().int % 10**10:010d}"
    ids = await _make_pos_med(db_session, gtin=gtin)

    code = f"01{gtin}1728063010LOT7"  # AI01 GTIN + AI17 expiry + AI10 batch
    r = await client.get("/api/v1/pos/scan", params={"barcode": code})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["medication_id"] == ids["med_id"]
    assert data["packaging_id"] == ids["strip_id"]  # default sale level
    assert len(data["levels"]) == 3

    # Unknown GTIN inside a valid GS1 string -> 404 (unknown code).
    missing = await client.get(
        "/api/v1/pos/scan", params={"barcode": f"01{'06249999999999'}17280630"}
    )
    assert missing.status_code == 404


async def test_pos_sale_api_unit_switch_and_cache_invariant(
    client: httpx.AsyncClient, db_session: AsyncSession, branch: Branch, cashier: User
) -> None:
    """Cashier sells through the API with a unit switch; the derived cache stays
    consistent because stock entered through receive_stock (canonical path)."""
    csrf = await _login(client, await _seed_role_user(db_session, "cashier"))
    ids = await _make_pos_med(db_session)
    await inventory_service.receive_stock(
        db_session,
        actor=cashier,
        branch_id=branch.id,
        medication_id=uuid.UUID(ids["med_id"]),
        batch_number=f"RCV-{uuid.uuid4().hex[:6]}",
        expiry_date=dt.date.today() + dt.timedelta(days=365),
        quantity=Decimal(100),
        purchase_price=Decimal("2.00"),
    )

    r = await client.post(
        "/api/v1/pos/sale",
        headers={"X-CSRF-Token": csrf},
        json={
            "branch_id": str(branch.id),
            "lines": [
                {"barcode": ids["barcode"], "packaging_id": ids["tablet_id"], "quantity": "4"},
                {"medication_id": ids["med_id"], "packaging_id": ids["strip_id"], "quantity": "1"},
            ],
            "payment_method": "cash",
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["total"] == "44.00"  # 4x3.50 + 1x30.00
    assert data["payment_method"] == "cash"
    assert data["invoice_number"].startswith("INV-")

    # 100 - 4 - 10 = 86 tablets; cache invariant intact through the API path.
    cached = (
        await db_session.execute(
            text(
                "SELECT cached_quantity FROM branch_inventory "
                "WHERE branch_id = :b AND medication_id = CAST(:m AS uuid)"
            ).bindparams(b=branch.id, m=ids["med_id"])
        )
    ).scalar_one()
    assert Decimal(cached) == Decimal(86)
    assert await inventory_service.drift_check(db_session, branch.id) == []


async def test_pos_sale_forbidden_for_viewer(
    client: httpx.AsyncClient, db_session: AsyncSession, branch: Branch
) -> None:
    csrf = await _login(client, await _seed_role_user(db_session, "viewer"))
    ids = await _make_pos_med(db_session)
    r = await client.post(
        "/api/v1/pos/sale",
        headers={"X-CSRF-Token": csrf},
        json={
            "branch_id": str(branch.id),
            "lines": [{"barcode": ids["barcode"], "quantity": "1"}],
            "payment_method": "cash",
        },
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "E-AUTH-002"
