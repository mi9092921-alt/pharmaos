"""Supplier performance + customer analytics (P3-M5).

Suppliers: PO activity/value per supplier (non-cancelled orders in range by
ORDER_DATE), and delivery quality — fill rate (Σ received / Σ ordered, line
level), full-supply rate (share of delivered orders that arrived complete) and
approval→last-receipt lead time — computed ONLY over orders whose delivery has
begun or finished (status in received | partially_received), so orders that
merely haven't been delivered YET never drag the rates down.

Customers: top spenders with raw RFM components (R = days since last purchase,
F = invoice count, M = net spend = invoices.subtotal − returns.subtotal; credit
notes carry the original invoice's customer_id so refunds net correctly) and
the current derived loyalty balance. Walk-in sales (customer_id NULL) are
excluded. The load-bearing test for the customers side is
test_customer_analytics_refund_only_customer: a customer whose ONLY in-range
event is a refund (bought before the range) must still appear with a NEGATIVE
net spend and no recency — omitting them would make the summary's refund totals
and the per-customer rows contradict each other.

Each test uses its OWN branch (the `branch` fixture) so branch-scoped reports
are isolated from data other tests leave in the shared test DB.
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
    Customer,
    Invoice,
    Medication,
    MedicationBarcode,
    MedicationBatch,
    MedicationPackaging,
    Role,
    Supplier,
    User,
)
from pharmaos_api.services import (
    purchase_service,
    reporting_service,
    return_service,
    sales_service,
)
from pharmaos_api.services.purchase_service import PurchaseLineIn, ReceiptLineIn
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


async def _make_med(
    db_session: AsyncSession, branch_id: uuid.UUID, *, purchase_price: str = "2.00"
) -> tuple[str, uuid.UUID, uuid.UUID]:
    """Strip (10 tablets) med at 30.00/strip with one stocked batch.

    Returns (barcode, medication_id, tablet_packaging_id) — the tablet packaging
    is what PO lines order against (qty_ordered is in smallest units anyway)."""
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
    med = Medication(trade_name=f"M5Med {uuid.uuid4().hex[:6]}", trade_name_ar="دواء M5")
    db_session.add(med)
    await db_session.flush()
    strip = MedicationPackaging(
        medication_id=med.id,
        level=2,
        unit_id=unit_ids["شريط"],
        name_ar="شريط",
        qty_in_parent=Decimal(10),
        selling_price=Decimal("30.00"),
        is_default_sale=True,
    )
    tablet = MedicationPackaging(
        medication_id=med.id,
        level=3,
        unit_id=unit_ids["قرص"],
        name_ar="قرص",
        qty_in_parent=Decimal(10),
        selling_price=Decimal("3.00"),
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
            batch_number=f"M5-{uuid.uuid4().hex[:8]}",
            expiry_date=dt.date.today() + dt.timedelta(days=365),
            quantity=Decimal(1000),
            purchase_price=Decimal(purchase_price),
        )
    )
    await db_session.commit()
    return barcode, med.id, tablet.id


async def _po(
    db_session: AsyncSession,
    actor: User,
    branch: Branch,
    supplier: Supplier,
    med_id: uuid.UUID,
    packaging_id: uuid.UUID,
    *,
    lines: list[tuple[str, str]],  # (qty_ordered, unit_cost) in tablets
) -> tuple[object, list[object]]:
    po, items = await purchase_service.create_purchase_order(
        db_session,
        actor=actor,
        branch_id=branch.id,
        supplier_id=supplier.id,
        lines=[
            PurchaseLineIn(
                medication_id=med_id,
                packaging_id=packaging_id,
                qty_ordered=Decimal(qty),
                unit_cost=Decimal(cost),
            )
            for qty, cost in lines
        ],
    )
    return po, items


async def _receive_lines(
    db_session: AsyncSession,
    actor: User,
    po: object,
    items: list[object],
    *,
    receipts: list[tuple[int, str]],  # (items index, qty)
) -> None:
    await purchase_service.receive(
        db_session,
        actor=actor,
        po=po,  # type: ignore[arg-type]
        receipts=[
            ReceiptLineIn(
                purchase_item_id=items[idx].id,  # type: ignore[attr-defined]
                batch_number=f"RCV-{uuid.uuid4().hex[:8]}",
                expiry_date=dt.date.today() + dt.timedelta(days=365),
                quantity=Decimal(qty),
            )
            for idx, qty in receipts
        ],
    )


async def _sell(
    db_session: AsyncSession,
    actor: User,
    branch: Branch,
    barcode: str,
    strips: int,
    *,
    customer_id: uuid.UUID | None = None,
) -> Invoice:
    return await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(quantity=Decimal(strips), barcode=barcode)],
        cashier=actor,
        customer_id=customer_id,
    )


# ------------------------------ suppliers: service ------------------------------


async def test_supplier_performance_manual_full_and_partial(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    """Hand-derived: PO1 orders 150 tablets (100@1.00 + 50@2.00 = 200.00),
    receives line 1 fully + HALF of line 2 (25 tablets) → partially received,
    received value 100×1 + 25×2 = 150.00, fill 125/150 = 83.33%, full-supply
    0/1 = 0%. Then PO2 (10@1.00) fully received → fill 135/160 = 84.38%,
    full-supply 1/2 = 50%."""
    supplier = Supplier(name=f"مورد {uuid.uuid4().hex[:6]}")
    db_session.add(supplier)
    await db_session.commit()
    _, med_id, tablet_pkg = await _make_med(db_session, branch.id)

    po1, items1 = await _po(
        db_session,
        actor,
        branch,
        supplier,
        med_id,
        tablet_pkg,
        lines=[("100", "1.00"), ("50", "2.00")],
    )
    await purchase_service.submit(db_session, actor=actor, po=po1)  # type: ignore[arg-type]
    await purchase_service.approve(db_session, actor=actor, po=po1)  # type: ignore[arg-type]
    await _receive_lines(db_session, actor, po1, items1, receipts=[(0, "100"), (1, "25")])

    today = dt.date.today()
    partial = await reporting_service.supplier_performance_report(
        db_session, branch_id=branch.id, date_from=today, date_to=today
    )
    assert partial["suppliers"][0]["po_count"] == 1
    assert partial["suppliers"][0]["ordered_value"] == "200.00"
    assert partial["suppliers"][0]["received_value"] == "150.00"
    assert partial["suppliers"][0]["fill_rate_percent"] == "83.33"
    assert partial["suppliers"][0]["full_supply_rate_percent"] == "0.00"
    assert partial["suppliers"][0]["avg_lead_time_days"] is not None
    summary = partial["summary"]
    assert isinstance(summary, dict)
    assert summary["po_count"] == 1 and summary["received_po_count"] == 1
    assert summary["total_ordered_value"] == "200.00"
    assert summary["total_received_value"] == "150.00"
    assert summary["fill_rate_percent"] == "83.33"
    assert summary["full_supply_rate_percent"] == "0.00"

    po2, items2 = await _po(
        db_session, actor, branch, supplier, med_id, tablet_pkg, lines=[("10", "1.00")]
    )
    await purchase_service.submit(db_session, actor=actor, po=po2)  # type: ignore[arg-type]
    await purchase_service.approve(db_session, actor=actor, po=po2)  # type: ignore[arg-type]
    await _receive_lines(db_session, actor, po2, items2, receipts=[(0, "10")])

    full = await reporting_service.supplier_performance_report(
        db_session, branch_id=branch.id, date_from=today, date_to=today
    )
    row = full["suppliers"][0]
    assert row["po_count"] == 2
    assert row["ordered_value"] == "210.00"
    assert row["received_value"] == "160.00"  # 150 + 10×1.00
    assert row["fill_rate_percent"] == "84.38"  # 135/160
    assert row["full_supply_rate_percent"] == "50.00"  # 1 of 2 delivered orders complete
    assert full["summary"]["full_supply_rate_percent"] == "50.00"


async def test_supplier_performance_excludes_cancelled_and_undelivered_from_rates(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    """A cancelled PO vanishes from EVERY number. Draft + awaiting-approval
    orders count as activity (po_count/ordered_value) but contribute NOTHING to
    the delivery rates — an order that simply hasn't been delivered yet is not
    evidence about the supplier."""
    supplier = Supplier(name=f"مورد {uuid.uuid4().hex[:6]}")
    db_session.add(supplier)
    await db_session.commit()
    _, med_id, tablet_pkg = await _make_med(db_session, branch.id)

    draft_po, _ = await _po(
        db_session, actor, branch, supplier, med_id, tablet_pkg, lines=[("20", "1.00")]
    )  # stays draft
    pending_po, items = await _po(
        db_session, actor, branch, supplier, med_id, tablet_pkg, lines=[("30", "1.00")]
    )
    await purchase_service.submit(db_session, actor=actor, po=pending_po)  # type: ignore[arg-type]
    cancelled_po, _ = await _po(
        db_session, actor, branch, supplier, med_id, tablet_pkg, lines=[("99", "5.00")]
    )
    await purchase_service.cancel(db_session, actor=actor, po=cancelled_po)  # type: ignore[arg-type]

    today = dt.date.today()
    report = await reporting_service.supplier_performance_report(
        db_session, branch_id=branch.id, date_from=today, date_to=today
    )
    row = report["suppliers"][0]
    assert row["po_count"] == 2  # draft + pending; cancelled excluded
    assert row["ordered_value"] == "50.00"  # 20 + 30; the 495.00 cancelled is gone
    assert row["received_value"] == "0.00"
    assert row["fill_rate_percent"] is None  # no delivered orders → no rate
    assert row["full_supply_rate_percent"] is None
    assert row["avg_lead_time_days"] is None
    assert report["summary"]["received_po_count"] == 0

    # Sanity: an undelivered-but-approved order also stays rate-neutral.
    await purchase_service.approve(db_session, actor=actor, po=pending_po)  # type: ignore[arg-type]
    assert items  # lines exist but nothing received
    after = await reporting_service.supplier_performance_report(
        db_session, branch_id=branch.id, date_from=today, date_to=today
    )
    assert after["suppliers"][0]["po_count"] == 2
    assert after["suppliers"][0]["fill_rate_percent"] is None


async def test_supplier_performance_order_date_range_and_lead_time(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    """POs filter by ORDER_DATE (the business date). A PO backdated 40 days is
    excluded from a today-only range and included in the wide one. Lead time is
    approval → receipt: backdating approved_at by 2 days makes the receipt land
    2.0 days later."""
    supplier = Supplier(name=f"مورد {uuid.uuid4().hex[:6]}")
    db_session.add(supplier)
    await db_session.commit()
    _, med_id, tablet_pkg = await _make_med(db_session, branch.id)

    po, items = await _po(
        db_session, actor, branch, supplier, med_id, tablet_pkg, lines=[("10", "1.00")]
    )
    await purchase_service.submit(db_session, actor=actor, po=po)  # type: ignore[arg-type]
    await purchase_service.approve(db_session, actor=actor, po=po)  # type: ignore[arg-type]
    await db_session.execute(
        text(
            "UPDATE purchase_orders SET approved_at = NOW() - interval '2 days', "
            "order_date = CURRENT_DATE - 40 WHERE id = :i"
        ).bindparams(
            i=po.id
        )  # type: ignore[attr-defined]
    )
    await db_session.commit()
    await _receive_lines(db_session, actor, po, items, receipts=[(0, "10")])

    today = dt.date.today()
    wide = await reporting_service.supplier_performance_report(
        db_session, branch_id=branch.id, date_from=today - dt.timedelta(days=40), date_to=today
    )
    row = wide["suppliers"][0]
    assert row["po_count"] == 1
    lead = Decimal(str(row["avg_lead_time_days"]))
    assert abs(lead - Decimal("2.0")) < Decimal("0.05"), row["avg_lead_time_days"]

    narrow = await reporting_service.supplier_performance_report(
        db_session, branch_id=branch.id, date_from=today, date_to=today
    )
    assert narrow["suppliers"] == []
    assert narrow["summary"]["po_count"] == 0
    assert narrow["summary"]["avg_lead_time_days"] is None


async def test_supplier_performance_rejects_reversed_range(
    db_session: AsyncSession, branch: Branch
) -> None:
    today = dt.date.today()
    with pytest.raises(ApiError) as exc:
        await reporting_service.supplier_performance_report(
            db_session, branch_id=branch.id, date_from=today, date_to=today - dt.timedelta(days=1)
        )
    assert exc.value.http_status == 422


async def test_supplier_performance_empty_branch(db_session: AsyncSession, branch: Branch) -> None:
    today = dt.date.today()
    report = await reporting_service.supplier_performance_report(
        db_session, branch_id=branch.id, date_from=today, date_to=today
    )
    summary = report["summary"]
    assert isinstance(summary, dict)
    assert summary["supplier_count"] == 0 and summary["po_count"] == 0
    assert summary["total_ordered_value"] == "0.00"
    assert summary["fill_rate_percent"] is None
    assert summary["full_supply_rate_percent"] is None
    assert summary["avg_lead_time_days"] is None
    assert report["suppliers"] == []


# ------------------------------ customers: service ------------------------------


async def test_customer_analytics_manual_top_spenders_and_loyalty(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    """Hand-derived: A buys 3 strips (90.00) then refunds 1 (−30.00) → net 60.00;
    B buys 1 strip (30.00) → net 30.00. Top order A then B; summary = 2 customers,
    2 invoices, 1 refund, gross 120.00, refunds 30.00, net 90.00, avg 45.00.
    The loyalty balance comes from the real accrue-on-sale path (P2-M5)."""
    barcode, _, _ = await _make_med(db_session, branch.id)
    cust_a = Customer(name=f"عميل {uuid.uuid4().hex[:6]}")
    cust_b = Customer(name=f"عميل {uuid.uuid4().hex[:6]}")
    db_session.add_all([cust_a, cust_b])
    await db_session.commit()

    invoice_a = await _sell(db_session, actor, branch, barcode, strips=3, customer_id=cust_a.id)
    await _sell(db_session, actor, branch, barcode, strips=1, customer_id=cust_b.id)
    from pharmaos_api.models import InvoiceItem

    item = (
        (
            await db_session.execute(
                select(InvoiceItem).where(InvoiceItem.invoice_id == invoice_a.id)
            )
        )
        .scalars()
        .first()
    )
    assert item is not None
    await return_service.create_return(
        db_session,
        actor=actor,
        original_invoice_id=invoice_a.id,
        lines=[return_service.ReturnLine(invoice_item_id=item.id, quantity=Decimal(1))],
        refund_method="cash",
    )

    await db_session.refresh(cust_a)
    today = dt.date.today()
    report = await reporting_service.customer_analytics_report(
        db_session, branch_id=branch.id, date_from=today, date_to=today
    )
    summary = report["summary"]
    assert isinstance(summary, dict)
    assert summary["customer_count"] == 2
    assert summary["invoice_count"] == 2 and summary["refund_count"] == 1
    assert summary["gross_spend"] == "120.00"
    assert summary["refunds_total"] == "30.00"
    assert summary["net_spend"] == "90.00"
    assert summary["avg_spend_per_customer"] == "45.00"

    rows = report["customers"]
    assert isinstance(rows, list) and len(rows) == 2
    top, second = rows
    assert top["name"] == cust_a.name
    assert top["net_spend"] == "60.00"  # 90 − 30
    assert top["gross_spend"] == "90.00" and top["refunds_total"] == "30.00"
    assert top["invoice_count"] == 1 and top["refund_count"] == 1
    assert top["last_purchase"] == today.isoformat()
    assert top["recency_days"] == 0
    # Accrue-on-sale ran on the FULL 90.00 paid for the sale (returns don't claw
    # points back — P2 review fix C4 adjusts on the return path); just prove the
    # balance is live and matches the customer row's own derived value.
    assert top["loyalty_points"] == cust_a.loyalty_points > 0
    assert second["name"] == cust_b.name
    assert second["net_spend"] == "30.00"
    assert second["refund_count"] == 0


async def test_customer_analytics_walkin_excluded_and_recency_backdated(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    """Walk-in sales (no customer) never appear. A customer sale backdated 40
    days reports recency 40 in the wide range and vanishes from a today-only
    range (avg None on empty)."""
    barcode, _, _ = await _make_med(db_session, branch.id)
    cust = Customer(name=f"عميل {uuid.uuid4().hex[:6]}")
    db_session.add(cust)
    await db_session.commit()

    await _sell(db_session, actor, branch, barcode, strips=1)  # walk-in: no customer
    old = await _sell(db_session, actor, branch, barcode, strips=1, customer_id=cust.id)
    await db_session.execute(
        text(
            "UPDATE invoices SET created_at = NOW() - (:d || ' days')::interval WHERE id = :i"
        ).bindparams(d=40, i=old.id)
    )
    await db_session.commit()

    today = dt.date.today()
    wide = await reporting_service.customer_analytics_report(
        db_session, branch_id=branch.id, date_from=today - dt.timedelta(days=40), date_to=today
    )
    summary = wide["summary"]
    assert isinstance(summary, dict)
    assert summary["customer_count"] == 1  # the walk-in invoice is not a customer
    assert summary["net_spend"] == "30.00"  # only the customer sale counts
    rows = wide["customers"]
    assert isinstance(rows, list) and len(rows) == 1
    assert rows[0]["recency_days"] == 40
    assert rows[0]["last_purchase"] == (today - dt.timedelta(days=40)).isoformat()

    narrow = await reporting_service.customer_analytics_report(
        db_session, branch_id=branch.id, date_from=today, date_to=today
    )
    assert narrow["customers"] == []
    narrow_summary = narrow["summary"]
    assert isinstance(narrow_summary, dict)
    assert narrow_summary["customer_count"] == 0
    assert narrow_summary["avg_spend_per_customer"] is None


async def test_customer_analytics_refund_only_customer(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    """Bought 40 days ago, refunded TODAY: in a today-only range the customer's
    only event is the refund — they must appear with zero invoices, a negative
    net spend and NO recency (last_purchase None), keeping the per-customer rows
    consistent with the summary's refund totals."""
    barcode, _, _ = await _make_med(db_session, branch.id)
    cust = Customer(name=f"عميل {uuid.uuid4().hex[:6]}")
    db_session.add(cust)
    await db_session.commit()
    invoice = await _sell(db_session, actor, branch, barcode, strips=3, customer_id=cust.id)
    await db_session.execute(
        text(
            "UPDATE invoices SET created_at = NOW() - (:d || ' days')::interval WHERE id = :i"
        ).bindparams(d=40, i=invoice.id)
    )
    await db_session.commit()

    from pharmaos_api.models import InvoiceItem

    item = (
        (await db_session.execute(select(InvoiceItem).where(InvoiceItem.invoice_id == invoice.id)))
        .scalars()
        .first()
    )
    assert item is not None
    await return_service.create_return(
        db_session,
        actor=actor,
        original_invoice_id=invoice.id,
        lines=[return_service.ReturnLine(invoice_item_id=item.id, quantity=Decimal(1))],
        refund_method="cash",
    )

    today = dt.date.today()
    report = await reporting_service.customer_analytics_report(
        db_session, branch_id=branch.id, date_from=today, date_to=today
    )
    rows = report["customers"]
    assert isinstance(rows, list) and len(rows) == 1
    row = rows[0]
    assert row["customer_id"] == str(cust.id)
    assert row["invoice_count"] == 0 and row["refund_count"] == 1
    assert row["gross_spend"] == "0.00"
    assert row["refunds_total"] == "30.00"
    assert row["net_spend"] == "-30.00"
    assert row["last_purchase"] is None and row["recency_days"] is None
    summary = report["summary"]
    assert isinstance(summary, dict)
    assert summary["customer_count"] == 1 and summary["refund_count"] == 1
    assert summary["net_spend"] == "-30.00"


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


async def test_supplier_report_permission_matrix_and_shape(
    client: httpx.AsyncClient, db_session: AsyncSession, branch: Branch
) -> None:
    today = dt.date.today().isoformat()
    params = {"branch_id": str(branch.id), "date_from": today, "date_to": today}

    await _login(client, await _seed_role_user(db_session, "cashier"))
    denied = await client.get("/api/v1/reports/suppliers/performance", params=params)
    assert denied.status_code == 403

    # pharmacist: reports.inventory but NOT reports.financial -> 403.
    await _login(client, await _seed_role_user(db_session, "pharmacist"))
    denied_ph = await client.get("/api/v1/reports/suppliers/performance", params=params)
    assert denied_ph.status_code == 403

    await _login(client, await _seed_role_user(db_session, "branch_manager"))
    ok = await client.get("/api/v1/reports/suppliers/performance", params=params)
    assert ok.status_code == 200, ok.text
    data = ok.json()["data"]
    assert set(data) >= {"summary", "suppliers"}
    assert set(data["summary"]) >= {
        "supplier_count",
        "po_count",
        "total_ordered_value",
        "fill_rate_percent",
        "full_supply_rate_percent",
        "avg_lead_time_days",
    }
    assert data["summary"]["po_count"] == 0  # empty branch

    bad = await client.get(
        "/api/v1/reports/suppliers/performance",
        params={**params, "date_to": "2020-01-01"},
    )
    assert bad.status_code == 422


async def test_customer_report_permission_matrix_and_shape(
    client: httpx.AsyncClient, db_session: AsyncSession, branch: Branch
) -> None:
    today = dt.date.today().isoformat()
    params = {"branch_id": str(branch.id), "date_from": today, "date_to": today}

    await _login(client, await _seed_role_user(db_session, "cashier"))
    denied = await client.get("/api/v1/reports/customers/analytics", params=params)
    assert denied.status_code == 403

    # pharmacist: reports.inventory but NOT reports.sales -> 403.
    await _login(client, await _seed_role_user(db_session, "pharmacist"))
    denied_ph = await client.get("/api/v1/reports/customers/analytics", params=params)
    assert denied_ph.status_code == 403

    await _login(client, await _seed_role_user(db_session, "branch_manager"))
    ok = await client.get("/api/v1/reports/customers/analytics", params=params)
    assert ok.status_code == 200, ok.text
    data = ok.json()["data"]
    assert set(data) >= {"summary", "customers"}
    assert set(data["summary"]) >= {
        "customer_count",
        "gross_spend",
        "refunds_total",
        "net_spend",
        "avg_spend_per_customer",
    }
    assert data["summary"]["customer_count"] == 0  # empty branch

    no_limit = await client.get(
        "/api/v1/reports/customers/analytics", params={**params, "top_limit": "0"}
    )
    assert no_limit.status_code == 422  # top_limit is ge=1


async def test_supplier_and_customer_csv_export_permissions(
    client: httpx.AsyncClient, db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    barcode, med_id, tablet_pkg = await _make_med(db_session, branch.id)
    supplier = Supplier(name=f"مورد {uuid.uuid4().hex[:6]}")
    db_session.add(supplier)
    await db_session.commit()
    po, items = await _po(
        db_session, actor, branch, supplier, med_id, tablet_pkg, lines=[("10", "1.00")]
    )
    await purchase_service.submit(db_session, actor=actor, po=po)  # type: ignore[arg-type]
    await purchase_service.approve(db_session, actor=actor, po=po)  # type: ignore[arg-type]
    await _receive_lines(db_session, actor, po, items, receipts=[(0, "10")])
    await _sell(db_session, actor, branch, barcode, strips=1)  # walk-in (excluded)

    today = dt.date.today().isoformat()
    params = {"branch_id": str(branch.id), "date_from": today, "date_to": today}

    await _login(client, await _seed_role_user(db_session, "cashier"))
    assert (
        await client.get("/api/v1/reports/suppliers/performance/export", params=params)
    ).status_code == 403
    assert (
        await client.get("/api/v1/reports/customers/analytics/export", params=params)
    ).status_code == 403

    await _login(client, await _seed_role_user(db_session, "branch_manager"))
    sup_csv = await client.get("/api/v1/reports/suppliers/performance/export", params=params)
    assert sup_csv.status_code == 200, sup_csv.text
    assert sup_csv.headers["content-type"].startswith("text/csv")
    lines = sup_csv.content.decode("utf-8-sig").strip().splitlines()
    assert lines[0] == (
        "supplier,po_count,ordered_value,received_value,"
        "fill_rate_percent,full_supply_rate_percent,avg_lead_time_days"
    )
    assert len(lines) == 2 and lines[1].startswith(supplier.name)
    assert ",10.00,10.00,100.00,100.00," in lines[1]

    cust_csv = await client.get("/api/v1/reports/customers/analytics/export", params=params)
    assert cust_csv.status_code == 200, cust_csv.text
    clines = cust_csv.content.decode("utf-8-sig").strip().splitlines()
    assert clines[0] == (
        "customer,phone,invoice_count,net_spend,last_purchase,recency_days,loyalty_points"
    )
    assert len(clines) == 1  # walk-in sale only → no customer rows


async def test_supplier_and_customer_csv_neutralize_formula_injection(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    """The same cross-role vector test_reports_m2 pins for the stock-level CSV,
    from the M5 exports: supplier names and customer names/phones are free text
    created by roles narrower than reports.export (any cashier can create a
    customer), and a leading =/+/-/@ would execute as a formula in Excel/Sheets
    on open. Every free-text cell must come back single-quote-prefixed —
    including a leading '+' phone, which is ordinary international formatting,
    not even malicious input."""
    barcode, med_id, tablet_pkg = await _make_med(db_session, branch.id)

    evil_supplier = Supplier(name="=SUM(A1:A2)")
    db_session.add(evil_supplier)
    await db_session.commit()
    po, items = await _po(
        db_session, actor, branch, evil_supplier, med_id, tablet_pkg, lines=[("10", "1.00")]
    )
    await purchase_service.submit(db_session, actor=actor, po=po)  # type: ignore[arg-type]
    await purchase_service.approve(db_session, actor=actor, po=po)  # type: ignore[arg-type]
    await _receive_lines(db_session, actor, po, items, receipts=[(0, "10")])

    evil_customer = Customer(name="@cmd|calc", phone="+201000000000")
    db_session.add(evil_customer)
    await db_session.commit()
    await _sell(db_session, actor, branch, barcode, strips=1, customer_id=evil_customer.id)

    today = dt.date.today()
    sup_csv = await reporting_service.supplier_performance_csv(
        db_session, branch_id=branch.id, date_from=today, date_to=today
    )
    assert "'=SUM(A1:A2)" in sup_csv

    cust_csv = await reporting_service.customer_analytics_csv(
        db_session, branch_id=branch.id, date_from=today, date_to=today
    )
    assert "'@cmd|calc" in cust_csv
    assert "'+201000000000" in cust_csv
