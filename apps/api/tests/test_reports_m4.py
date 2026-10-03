"""Profit & loss analytics (P3-M4): COGS from the batch (decision D3), margins,
operating net after expenses.

The load-bearing test is test_profit_loss_manual_computation: a fully manual
purchase→sale→margin example (buy tablets at 2.00, sell strips of 10 at 30.00)
whose every number is derived by hand in the assertions — the P&L chain
(revenue → COGS → gross profit → margin → operating net) is verified against
arithmetic, not against itself. test_profit_loss_refunds_net_out then proves the
"استبعاد المرتجعات" requirement symmetrically: a credit note must reduce BOTH
revenue (returns.subtotal) AND COGS (return_items × the return batch's
purchase_price, which return_service copies from the origin batch), so a return
can never fake a profit by refunding revenue while keeping its cost booked.

Each test uses its OWN branch (the `branch` fixture) so branch-scoped reports
are isolated from invoices other tests leave in the shared test DB. EG medicine
VAT is exempt for these medications (medicine_vat_rate NULL → tax 0), so
subtotal == gross and margins are clean integers — the VAT machinery is
exercised in test_vat_m6/test_reports_m1, not here.
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
    ExpenseCategory,
    Invoice,
    InvoiceItem,
    Medication,
    MedicationBarcode,
    MedicationBatch,
    MedicationPackaging,
    Role,
    User,
)
from pharmaos_api.services import expense_service, reporting_service, return_service, sales_service
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


async def _make_strip_med(
    db_session: AsyncSession,
    branch_id: uuid.UUID,
    *,
    purchase_price: str = "2.00",
    selling_price: str = "30.00",
    tablets: int = 1000,
) -> str:
    """Strip-packaged med (10 tablets/strip) at 30.00/strip, stocked with one
    batch at the given per-TABLET purchase price — the manual-example shape.

    The FULL packaging chain matters: qty_smallest = product of the DEEPER
    levels' qty_in_parent (sales_service._smallest_unit_factor), so the level-3
    tablet row (qty_in_parent=10) is what makes 1 strip deduct 10 tablets from
    the batch — i.e. COGS prices each TABLET at the batch's purchase_price."""
    unit_ids: dict[str, uuid.UUID] = {}
    for unit_name in ("شريط", "قرص"):
        unit_ids[unit_name] = (
            await db_session.execute(
                text(
                    "INSERT INTO units (name_ar) VALUES (:u) "
                    "ON CONFLICT (name_ar) DO UPDATE SET name_ar=EXCLUDED.name_ar RETURNING id"
                ).bindparams(u=unit_name)
            )
        ).scalar_one()
    await db_session.commit()
    med = Medication(trade_name=f"M4Med {uuid.uuid4().hex[:6]}", trade_name_ar="دواء الربح")
    db_session.add(med)
    await db_session.flush()
    strip = MedicationPackaging(
        medication_id=med.id,
        level=2,
        unit_id=unit_ids["شريط"],
        name_ar="شريط",
        qty_in_parent=Decimal(10),
        selling_price=Decimal(selling_price),
        is_default_sale=True,
    )
    tablet = MedicationPackaging(
        medication_id=med.id,
        level=3,
        unit_id=unit_ids["قرص"],
        name_ar="قرص",
        qty_in_parent=Decimal(10),  # 10 tablets per strip — the COGS factor
        selling_price=Decimal(selling_price) / 10,  # not sold directly; sane price
        is_sellable=False,
    )
    db_session.add(strip)
    db_session.add(tablet)
    await db_session.flush()
    barcode = f"622{uuid.uuid4().int % 10**10:010d}"
    db_session.add(MedicationBarcode(medication_id=med.id, packaging_id=strip.id, barcode=barcode))
    db_session.add(
        MedicationBatch(
            branch_id=branch_id,
            medication_id=med.id,
            batch_number=f"M4-{uuid.uuid4().hex[:8]}",
            expiry_date=dt.date.today() + dt.timedelta(days=365),
            quantity=Decimal(tablets),
            purchase_price=Decimal(purchase_price),
        )
    )
    await db_session.commit()
    return barcode


async def _sell(
    db_session: AsyncSession,
    actor: User,
    branch: Branch,
    barcode: str,
    strips: int,
    *,
    payment_method: str = "cash",
) -> Invoice:
    return await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(quantity=Decimal(strips), barcode=barcode)],
        cashier=actor,
        payment_method=payment_method,
    )


async def _first_item(db_session: AsyncSession, invoice: Invoice) -> InvoiceItem:
    item = (
        (await db_session.execute(select(InvoiceItem).where(InvoiceItem.invoice_id == invoice.id)))
        .scalars()
        .first()
    )
    assert item is not None
    return item


async def _add_expense(
    db_session: AsyncSession,
    actor: User,
    branch: Branch,
    *,
    amount: str,
    expense_date: dt.date | None = None,
) -> None:
    cat = ExpenseCategory(name_ar=f"مصروف {uuid.uuid4().hex[:6]}")
    db_session.add(cat)
    await db_session.commit()
    await expense_service.create_expense(
        db_session,
        actor=actor,
        branch_id=branch.id,
        expense_category_id=cat.id,
        amount=Decimal(amount),
        expense_date=expense_date or dt.date.today(),
    )


async def _backdate_invoice(db_session: AsyncSession, invoice_id: uuid.UUID, days: int) -> None:
    await db_session.execute(
        text(
            "UPDATE invoices SET created_at = NOW() - (:d || ' days')::interval WHERE id = :i"
        ).bindparams(d=days, i=invoice_id)
    )
    await db_session.commit()


async def _categorize_med(db_session: AsyncSession, barcode: str) -> None:
    """Attach a fresh category to the medication behind a barcode — categories
    has no ORM model, so raw SQL mirrors how the report itself reads them."""
    cat_id = (
        await db_session.execute(
            text("INSERT INTO categories (name_ar) VALUES ('فئة M4') RETURNING id")
        )
    ).scalar_one()
    await db_session.execute(
        text(
            "UPDATE medications SET category_id = :c WHERE id = "
            "(SELECT medication_id FROM medication_barcodes WHERE barcode = :b)"
        ).bindparams(c=cat_id, b=barcode)
    )
    await db_session.commit()


# ------------------------------ service ------------------------------


async def test_profit_loss_manual_computation(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    """THE manual example (plan verification): buy tablets @ 2.00, sell strips of
    10 @ 30.00. 2 strips sold today, 10.00 expense today:
      revenue  = 2 × 30.00                    = 60.00  (VAT exempt)
      cogs     = 2 × 10 tablets × 2.00        = 40.00
      gross    = 60 − 40                      = 20.00
      margin   = 20 / 60                      = 33.33%
      operating = 20 − 10                     = 10.00"""
    barcode = await _make_strip_med(db_session, branch.id)
    await _sell(db_session, actor, branch, barcode, strips=2)
    await _add_expense(db_session, actor, branch, amount="10.00")

    today = dt.date.today()
    report = await reporting_service.profit_loss_report(
        db_session, branch_id=branch.id, date_from=today, date_to=today
    )
    summary = report["summary"]
    assert isinstance(summary, dict)
    assert summary["gross_sales_subtotal"] == "60.00"
    assert summary["refunds_subtotal"] == "0.00"
    assert summary["net_revenue"] == "60.00"
    assert summary["cogs_sold"] == "40.00"
    assert summary["cogs_returned"] == "0.00"
    assert summary["net_cogs"] == "40.00"
    assert summary["gross_profit"] == "20.00"
    assert summary["gross_margin_percent"] == "33.33"
    assert summary["expenses_total"] == "10.00"
    assert summary["operating_profit"] == "10.00"
    assert summary["invoice_count"] == 1
    assert summary["refund_count"] == 0

    top = report["top_items"]
    assert isinstance(top, list) and len(top) == 1
    assert top[0]["qty_smallest"] == "20.000"  # 2 strips × 10 tablets
    assert top[0]["revenue"] == "60.00"
    assert top[0]["cogs"] == "40.00"
    assert top[0]["profit"] == "20.00"
    assert top[0]["margin_percent"] == "33.33"

    trend = report["trend"]
    assert isinstance(trend, list) and len(trend) == 1
    assert trend[0]["bucket"] == today.isoformat()
    assert trend[0] == {
        "bucket": today.isoformat(),
        "revenue": "60.00",
        "cogs": "40.00",
        "gross_profit": "20.00",
        "expenses": "10.00",
    }

    exp_cats = report["by_expense_category"]
    assert isinstance(exp_cats, list) and len(exp_cats) == 1
    assert exp_cats[0]["total"] == "10.00"
    assert exp_cats[0]["name_ar"].startswith("مصروف ")


async def test_profit_loss_refunds_net_out_revenue_and_cogs(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    """A credit note must reduce BOTH sides: revenue by returns.subtotal AND
    COGS by return_items.qty_smallest × the return batch's purchase_price
    (copied from the origin batch). Sell 3 strips (90.00, COGS 60.00), refund
    1 strip (30.00, COGS 20.00) → net economics identical to selling 2."""
    barcode = await _make_strip_med(db_session, branch.id)
    invoice = await _sell(db_session, actor, branch, barcode, strips=3)
    item = await _first_item(db_session, invoice)
    await return_service.create_return(
        db_session,
        actor=actor,
        original_invoice_id=invoice.id,
        lines=[return_service.ReturnLine(invoice_item_id=item.id, quantity=Decimal(1))],
        refund_method="cash",
    )

    today = dt.date.today()
    report = await reporting_service.profit_loss_report(
        db_session, branch_id=branch.id, date_from=today, date_to=today
    )
    summary = report["summary"]
    assert isinstance(summary, dict)
    assert summary["gross_sales_subtotal"] == "90.00"
    assert summary["refunds_subtotal"] == "30.00"
    assert summary["net_revenue"] == "60.00"
    assert summary["cogs_sold"] == "60.00"
    assert summary["cogs_returned"] == "20.00"
    assert summary["net_cogs"] == "40.00"
    assert summary["gross_profit"] == "20.00"
    assert summary["gross_margin_percent"] == "33.33"
    assert summary["invoice_count"] == 1 and summary["refund_count"] == 1

    top = report["top_items"]
    assert isinstance(top, list) and len(top) == 1
    # Net qty 3−1=2 strips; net profit identical to an outright 2-strip sale.
    assert top[0]["qty_smallest"] == "20.000"
    assert top[0]["revenue"] == "60.00"
    assert top[0]["cogs"] == "40.00"
    assert top[0]["profit"] == "20.00"


async def test_profit_loss_excludes_cancelled_and_deleted_invoices(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    """Only completed invoices are revenue — a cancelled invoice and a
    soft-deleted one must both vanish from every P&L line."""
    barcode = await _make_strip_med(db_session, branch.id)
    cancelled = await _sell(db_session, actor, branch, barcode, strips=1)
    await db_session.execute(
        text("UPDATE invoices SET status = 'cancelled' WHERE id = :i").bindparams(i=cancelled.id)
    )
    deleted = await _sell(db_session, actor, branch, barcode, strips=1)
    await db_session.execute(
        text("UPDATE invoices SET is_deleted = TRUE WHERE id = :i").bindparams(i=deleted.id)
    )
    await db_session.commit()

    today = dt.date.today()
    report = await reporting_service.profit_loss_report(
        db_session, branch_id=branch.id, date_from=today, date_to=today
    )
    summary = report["summary"]
    assert isinstance(summary, dict)
    assert summary["net_revenue"] == "0.00"
    assert summary["net_cogs"] == "0.00"
    assert summary["gross_profit"] == "0.00"
    assert summary["gross_margin_percent"] is None  # no revenue → no margin
    assert summary["invoice_count"] == 0
    assert report["top_items"] == []


async def test_profit_loss_trend_buckets_and_date_range(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    """Today: 2 strips (60.00 / COGS 40.00) + no expense. 40 days ago: 1 strip
    (30.00 / COGS 20.00) + a 5.00 expense. Day buckets must keep both periods'
    economics separate; a narrow window excludes the backdated one entirely."""
    barcode = await _make_strip_med(db_session, branch.id)
    await _sell(db_session, actor, branch, barcode, strips=2)
    old = await _sell(db_session, actor, branch, barcode, strips=1)
    await _backdate_invoice(db_session, old.id, 40)
    await _add_expense(
        db_session,
        actor,
        branch,
        amount="5.00",
        expense_date=dt.date.today() - dt.timedelta(days=40),
    )

    today = dt.date.today()
    wide = await reporting_service.profit_loss_report(
        db_session,
        branch_id=branch.id,
        date_from=today - dt.timedelta(days=40),
        date_to=today,
        granularity="day",
    )
    trend = wide["trend"]
    assert isinstance(trend, list) and len(trend) == 2
    old_bucket, today_bucket = trend[0], trend[1]
    assert old_bucket["bucket"] == (today - dt.timedelta(days=40)).isoformat()
    assert old_bucket["revenue"] == "30.00"
    assert old_bucket["cogs"] == "20.00"
    assert old_bucket["gross_profit"] == "10.00"
    assert old_bucket["expenses"] == "5.00"
    assert today_bucket["bucket"] == today.isoformat()
    assert today_bucket["revenue"] == "60.00"
    assert today_bucket["gross_profit"] == "20.00"
    assert today_bucket["expenses"] == "0.00"

    narrow = await reporting_service.profit_loss_report(
        db_session, branch_id=branch.id, date_from=today, date_to=today
    )
    narrow_summary = narrow["summary"]
    assert isinstance(narrow_summary, dict)
    assert narrow_summary["net_revenue"] == "60.00" and narrow_summary["invoice_count"] == 1
    assert narrow_summary["expenses_total"] == "0.00"
    narrow_trend = narrow["trend"]
    assert isinstance(narrow_trend, list) and len(narrow_trend) == 1


async def test_profit_loss_margin_by_category_and_top_items_order(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    """Two meds: one categorized (buy 1.00/tablet → 66.67% margin), one not
    (buy 4.00/tablet → −33.33% margin, sold at a loss). The per-category view
    must split named vs uncategorized (NULL) buckets, and top_items must rank
    by NET profit, not revenue (both have equal revenue 30.00)."""
    profitable = await _make_strip_med(db_session, branch.id, purchase_price="1.00")
    losing = await _make_strip_med(db_session, branch.id, purchase_price="4.00")
    await _sell(db_session, actor, branch, profitable, strips=1)
    await _sell(db_session, actor, branch, losing, strips=1)
    await _categorize_med(db_session, profitable)

    today = dt.date.today()
    report = await reporting_service.profit_loss_report(
        db_session, branch_id=branch.id, date_from=today, date_to=today
    )
    by_cat = report["by_category"]
    assert isinstance(by_cat, list) and len(by_cat) == 2
    # Named category first (sort puts NULL/unnamed last).
    assert by_cat[0]["name_ar"] == "فئة M4"
    assert by_cat[0]["revenue"] == "30.00"
    assert by_cat[0]["cogs"] == "10.00"
    assert by_cat[0]["profit"] == "20.00"
    assert by_cat[0]["margin_percent"] == "66.67"
    assert by_cat[1]["category_id"] is None and by_cat[1]["name_ar"] is None
    assert by_cat[1]["profit"] == "-10.00"

    top = report["top_items"]
    assert isinstance(top, list) and len(top) == 2
    assert top[0]["profit"] == "20.00" and top[0]["margin_percent"] == "66.67"
    assert top[1]["profit"] == "-10.00" and top[1]["margin_percent"] == "-33.33"


async def test_profit_loss_uncategorized_bucket_and_no_revenue_margin(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    """A sale with NO category lands in the uncategorized (NULL) bucket, and an
    expense-only range reports a negative operating profit with no margin."""
    barcode = await _make_strip_med(db_session, branch.id)
    await _sell(db_session, actor, branch, barcode, strips=1)  # 30.00, COGS 20.00
    await _add_expense(db_session, actor, branch, amount="50.00")

    today = dt.date.today()
    with_sales = await reporting_service.profit_loss_report(
        db_session, branch_id=branch.id, date_from=today, date_to=today
    )
    by_cat = with_sales["by_category"]
    assert isinstance(by_cat, list) and len(by_cat) == 1
    assert by_cat[0]["category_id"] is None
    assert by_cat[0]["name_ar"] is None  # uncategorized bucket carries no name
    assert by_cat[0]["profit"] == "10.00"

    # Empty branch (no sales): revenue 0 → margin None; operating = −expenses.
    empty = Branch(name=f"فرع {uuid.uuid4().hex[:6]}", country_code="EG", currency_code="EGP")
    db_session.add(empty)
    await db_session.commit()
    await _add_expense(db_session, actor, empty, amount="10.00")
    no_sales = await reporting_service.profit_loss_report(
        db_session, branch_id=empty.id, date_from=today, date_to=today
    )
    summary = no_sales["summary"]
    assert isinstance(summary, dict)
    assert summary["net_revenue"] == "0.00"
    assert summary["gross_margin_percent"] is None
    assert summary["expenses_total"] == "10.00"
    assert summary["operating_profit"] == "-10.00"
    assert no_sales["top_items"] == []
    assert no_sales["by_category"] == []
    cats = no_sales["by_expense_category"]
    assert isinstance(cats, list) and len(cats) == 1 and cats[0]["total"] == "10.00"


async def test_profit_loss_rejects_reversed_range(db_session: AsyncSession, branch: Branch) -> None:
    today = dt.date.today()
    with pytest.raises(ApiError) as exc:
        await reporting_service.profit_loss_report(
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


async def test_profit_loss_permission_matrix_and_shape(
    client: httpx.AsyncClient, db_session: AsyncSession, branch: Branch
) -> None:
    today = dt.date.today().isoformat()
    params = {"branch_id": str(branch.id), "date_from": today, "date_to": today}

    # cashier: no reports.financial -> 403.
    await _login(client, await _seed_role_user(db_session, "cashier"))
    denied = await client.get("/api/v1/reports/profit-loss", params=params)
    assert denied.status_code == 403

    # pharmacist: has reports.inventory but NOT reports.financial -> 403
    # (margins expose pricing policy; the inventory tier is deliberately wider
    # than the financial one).
    await _login(client, await _seed_role_user(db_session, "pharmacist"))
    denied_ph = await client.get("/api/v1/reports/profit-loss", params=params)
    assert denied_ph.status_code == 403

    # branch_manager: allowed.
    await _login(client, await _seed_role_user(db_session, "branch_manager"))
    ok = await client.get("/api/v1/reports/profit-loss", params=params)
    assert ok.status_code == 200, ok.text
    data = ok.json()["data"]
    assert set(data) >= {
        "summary",
        "by_expense_category",
        "by_category",
        "top_items",
        "trend",
    }
    assert set(data["summary"]) >= {
        "net_revenue",
        "net_cogs",
        "gross_profit",
        "gross_margin_percent",
        "expenses_total",
        "operating_profit",
    }
    assert data["summary"]["net_revenue"] == "0.00"  # empty branch

    bad = await client.get(
        "/api/v1/reports/profit-loss",
        params={**params, "date_to": "2020-01-01"},
    )
    assert bad.status_code == 422


async def test_profit_loss_csv_export_permission_and_shape(
    client: httpx.AsyncClient, db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    barcode = await _make_strip_med(db_session, branch.id)
    await _sell(db_session, actor, branch, barcode, strips=2)  # 60.00 / COGS 40.00
    today = dt.date.today().isoformat()
    params = {"branch_id": str(branch.id), "date_from": today, "date_to": today}

    # cashier lacks reports.export.
    await _login(client, await _seed_role_user(db_session, "cashier"))
    denied = await client.get("/api/v1/reports/profit-loss/export", params=params)
    assert denied.status_code == 403

    await _login(client, await _seed_role_user(db_session, "branch_manager"))
    ok = await client.get("/api/v1/reports/profit-loss/export", params=params)
    assert ok.status_code == 200, ok.text
    assert ok.headers["content-type"].startswith("text/csv")
    body = ok.content.decode("utf-8-sig")  # strips the BOM
    lines = body.strip().splitlines()
    assert lines[0] == "period,revenue,cogs,gross_profit,expenses"
    assert len(lines) == 2
    assert lines[1].endswith(",60.00,40.00,20.00,0.00")
