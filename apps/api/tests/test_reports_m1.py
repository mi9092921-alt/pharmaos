"""Sales reports (P3-M1): on-demand SQL aggregation over a branch's invoices.

Covers the reporting_service aggregations (gross/net/by-payment/by-refund, time
trend + bucketing, top items, date-range filtering, validation) and the HTTP
layer (reports.sales / reports.export permission tiers, CSV export shape).

Each test uses its OWN branch (the `branch` fixture) so branch-scoped reports are
isolated from invoices other tests leave in the shared test DB.
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
    Invoice,
    InvoiceItem,
    Medication,
    MedicationBarcode,
    MedicationBatch,
    MedicationPackaging,
    Role,
    User,
)
from pharmaos_api.services import reporting_service, return_service, sales_service
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


async def _make_med(db_session: AsyncSession, branch_id: uuid.UUID, tablets: int = 1000) -> str:
    """Strip-barcode med at 30.00/strip (10 tablets), stocked with one batch."""
    unit_id = (
        await db_session.execute(
            text(
                "INSERT INTO units (name_ar) VALUES ('شريط') "
                "ON CONFLICT (name_ar) DO UPDATE SET name_ar=EXCLUDED.name_ar RETURNING id"
            )
        )
    ).scalar_one()
    await db_session.commit()
    med = Medication(trade_name=f"RepMed {uuid.uuid4().hex[:6]}", trade_name_ar="دواء التقرير")
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
    db_session.add(
        MedicationBatch(
            branch_id=branch_id,
            medication_id=med.id,
            batch_number=f"REP-{uuid.uuid4().hex[:8]}",
            expiry_date=dt.date.today() + dt.timedelta(days=365),
            quantity=Decimal(tablets),
            purchase_price=Decimal("2.00"),
        )
    )
    await db_session.commit()
    return barcode


async def _first_item(db_session: AsyncSession, invoice: Invoice) -> InvoiceItem:
    item = (
        (await db_session.execute(select(InvoiceItem).where(InvoiceItem.invoice_id == invoice.id)))
        .scalars()
        .first()
    )
    assert item is not None
    return item


async def _backdate_invoice(db_session: AsyncSession, invoice_id: uuid.UUID, days: int) -> None:
    await db_session.execute(
        text(
            "UPDATE invoices SET created_at = NOW() - (:d || ' days')::interval WHERE id = :i"
        ).bindparams(d=days, i=invoice_id)
    )
    await db_session.commit()


# ------------------------------ service ------------------------------


async def test_sales_report_totals_and_breakdown(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    barcode = await _make_med(db_session, branch.id)
    # 2 cash sales (60 + 30) + 1 card sale (30) — all today.
    await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(quantity=Decimal(2), barcode=barcode)],
        cashier=actor,
    )
    await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(quantity=Decimal(1), barcode=barcode)],
        cashier=actor,
    )
    await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(quantity=Decimal(1), barcode=barcode)],
        cashier=actor,
        payment_method="card",
    )

    today = dt.date.today()
    report = await reporting_service.sales_report(
        db_session, branch_id=branch.id, date_from=today, date_to=today
    )
    summary = report["summary"]
    assert isinstance(summary, dict)
    assert summary["gross_sales"] == "120.00"
    assert summary["net_sales"] == "120.00"  # no refunds
    assert summary["invoice_count"] == 3
    assert summary["refund_count"] == 0
    assert summary["avg_invoice"] == "40.00"  # 120 / 3
    # EG medicine VAT is exempt (medicine_vat_rate NULL) -> tax 0, subtotal == gross.
    assert summary["tax_total"] == "0.00"
    assert summary["subtotal"] == "120.00"

    by_pm = {r["method"]: r for r in report["by_payment_method"]}
    assert by_pm["cash"]["count"] == 2 and by_pm["cash"]["total"] == "90.00"
    assert by_pm["card"]["count"] == 1 and by_pm["card"]["total"] == "30.00"

    trend = report["trend"]
    assert isinstance(trend, list) and len(trend) == 1
    assert trend[0]["bucket"] == today.isoformat() and trend[0]["total"] == "120.00"

    top = report["top_items"]
    assert isinstance(top, list) and len(top) == 1
    assert top[0]["name_ar"] == "دواء التقرير"
    assert top[0]["revenue"] == "120.00"
    assert top[0]["qty_smallest"] == "4.000"  # 2+1+1 strips (the smallest sold unit)


async def test_sales_report_refund_nets_and_by_refund_method(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    barcode = await _make_med(db_session, branch.id)
    invoice = await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(quantity=Decimal(3), barcode=barcode)],
        cashier=actor,
    )  # 90.00 cash
    item = await _first_item(db_session, invoice)
    await return_service.create_return(
        db_session,
        actor=actor,
        original_invoice_id=invoice.id,
        lines=[return_service.ReturnLine(invoice_item_id=item.id, quantity=Decimal(1))],
        refund_method="cash",
    )  # refund 30.00

    today = dt.date.today()
    report = await reporting_service.sales_report(
        db_session, branch_id=branch.id, date_from=today, date_to=today
    )
    summary = report["summary"]
    assert isinstance(summary, dict)
    assert summary["gross_sales"] == "90.00"  # gross is unchanged by refunds
    assert summary["refunds_total"] == "30.00"
    assert summary["net_sales"] == "60.00"  # 90 - 30
    assert summary["refund_count"] == 1
    by_ref = {r["method"]: r for r in report["by_refund_method"]}
    assert by_ref["cash"]["count"] == 1 and by_ref["cash"]["total"] == "30.00"


async def test_sales_report_date_range_and_bucketing(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    barcode = await _make_med(db_session, branch.id)
    # Today: 60.00.
    await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(quantity=Decimal(2), barcode=barcode)],
        cashier=actor,
    )
    # 40 days ago: 30.00 (backdated).
    old = await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(quantity=Decimal(1), barcode=barcode)],
        cashier=actor,
    )
    await _backdate_invoice(db_session, old.id, 40)

    today = dt.date.today()

    # Narrow window (today only) excludes the backdated sale.
    narrow = await reporting_service.sales_report(
        db_session, branch_id=branch.id, date_from=today, date_to=today
    )
    narrow_summary = narrow["summary"]
    assert isinstance(narrow_summary, dict)
    assert narrow_summary["gross_sales"] == "60.00" and narrow_summary["invoice_count"] == 1
    assert isinstance(narrow["trend"], list) and len(narrow["trend"]) == 1

    # Wide window (last 40 days) includes both, in two distinct day buckets.
    wide = await reporting_service.sales_report(
        db_session,
        branch_id=branch.id,
        date_from=today - dt.timedelta(days=40),
        date_to=today,
        granularity="day",
    )
    wide_summary = wide["summary"]
    assert isinstance(wide_summary, dict)
    assert wide_summary["gross_sales"] == "90.00" and wide_summary["invoice_count"] == 2
    wide_trend = wide["trend"]
    assert isinstance(wide_trend, list) and len(wide_trend) == 2
    assert sum(Decimal(row["total"]) for row in wide_trend) == Decimal("90.00")
    assert [row["bucket"] for row in wide_trend] == sorted(row["bucket"] for row in wide_trend)


async def test_sales_report_rejects_reversed_range(
    db_session: AsyncSession, branch: Branch
) -> None:
    today = dt.date.today()
    with pytest.raises(ApiError) as exc:
        await reporting_service.sales_report(
            db_session, branch_id=branch.id, date_from=today, date_to=today - dt.timedelta(days=1)
        )
    assert exc.value.http_status == 422


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


async def test_sales_report_permission_matrix(
    client: httpx.AsyncClient, db_session: AsyncSession, branch: Branch
) -> None:
    today = dt.date.today().isoformat()
    params = {"branch_id": str(branch.id), "date_from": today, "date_to": today}

    # cashier: no reports.sales -> 403.
    await _login(client, await _seed_role_user(db_session, "cashier"))
    denied = await client.get("/api/v1/reports/sales", params=params)
    assert denied.status_code == 403

    # pharmacist: has reports.inventory but NOT reports.sales -> 403.
    await _login(client, await _seed_role_user(db_session, "pharmacist"))
    denied_ph = await client.get("/api/v1/reports/sales", params=params)
    assert denied_ph.status_code == 403

    # branch_manager: allowed.
    await _login(client, await _seed_role_user(db_session, "branch_manager"))
    ok = await client.get("/api/v1/reports/sales", params=params)
    assert ok.status_code == 200, ok.text
    data = ok.json()["data"]
    assert set(data) >= {"summary", "trend", "by_payment_method", "top_items"}
    assert data["summary"]["gross_sales"] == "0.00"  # empty branch


async def test_sales_csv_export_permission_and_shape(
    client: httpx.AsyncClient, db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    barcode = await _make_med(db_session, branch.id)
    await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(quantity=Decimal(2), barcode=barcode)],
        cashier=actor,
    )  # 60.00 today
    today = dt.date.today().isoformat()
    params = {"branch_id": str(branch.id), "date_from": today, "date_to": today}

    # cashier lacks reports.export.
    await _login(client, await _seed_role_user(db_session, "cashier"))
    denied = await client.get("/api/v1/reports/sales/export", params=params)
    assert denied.status_code == 403

    # branch_manager can export.
    await _login(client, await _seed_role_user(db_session, "branch_manager"))
    csv_resp = await client.get("/api/v1/reports/sales/export", params=params)
    assert csv_resp.status_code == 200, csv_resp.text
    assert csv_resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in csv_resp.headers.get("content-disposition", "")
    body = csv_resp.text
    assert body.startswith("\ufeff")  # Excel BOM
    assert "period,invoice_count,gross_total" in body
    assert f"{today},1,60.00" in body
