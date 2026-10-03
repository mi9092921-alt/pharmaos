"""Cash sessions (P1-M10): open/close lifecycle with the audit trail
(cash_session.opened/closed/discrepancy), sale linkage + tendered/change
persistence, drawer math (expected = float + cash totals), the day Z-report
buckets, and the HTTP layer with the cashier permission tiers.

P2-M7 reconciliation: expected_cash/cash_total/card_total are sourced from the
payments ledger (net of refunds) — a cash/card refund issued mid-shift must be
reflected in the SAME session's drawer math and survive into close_session's
frozen discrepancy, while a store_credit refund must touch neither."""

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
from pharmaos_api.services import (
    cashier_service,
    expense_service,
    return_service,
    sales_service,
)
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


async def _make_med(db_session: AsyncSession, branch_id: uuid.UUID, tablets: int = 500) -> str:
    """Strip-barcode med at 30.00/strip (10 tablets); stocked with one batch."""
    unit_id = (
        await db_session.execute(
            text(
                "INSERT INTO units (name_ar) VALUES ('شريط') "
                "ON CONFLICT (name_ar) DO UPDATE SET name_ar=EXCLUDED.name_ar RETURNING id"
            )
        )
    ).scalar_one()
    await db_session.commit()
    med = Medication(trade_name=f"CshMed {uuid.uuid4().hex[:6]}", trade_name_ar="دواء الكاشير")
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
            batch_number=f"CSH-{uuid.uuid4().hex[:8]}",
            expiry_date=dt.date.today() + dt.timedelta(days=365),
            quantity=Decimal(tablets),
            purchase_price=Decimal("2.00"),
        )
    )
    await db_session.commit()
    return barcode


async def _audit_count(db_session: AsyncSession, action: str, entity_id: uuid.UUID) -> int:
    return int(
        (
            await db_session.execute(
                text(
                    "SELECT COUNT(*) FROM audit_logs WHERE action = :a AND entity_id = :e"
                ).bindparams(a=action, e=entity_id)
            )
        ).scalar_one()
    )


async def _first_item(db_session: AsyncSession, invoice: Invoice) -> InvoiceItem:
    item = (
        (await db_session.execute(select(InvoiceItem).where(InvoiceItem.invoice_id == invoice.id)))
        .scalars()
        .first()
    )
    assert item is not None
    return item


# ------------------------------ lifecycle ------------------------------


async def test_open_session_audits_and_blocks_duplicates(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    row = await cashier_service.open_session(
        db_session, actor=actor, branch_id=branch.id, opening_float=Decimal("200.00")
    )
    assert row.status == "open" and row.opening_float == Decimal("200.00")
    assert await _audit_count(db_session, "cash_session.opened", row.id) == 1

    with pytest.raises(ApiError) as exc:
        await cashier_service.open_session(
            db_session, actor=actor, branch_id=branch.id, opening_float=Decimal(0)
        )
    assert exc.value.code == "E-CSH-001" and exc.value.http_status == 409

    # A different branch is a different drawer — allowed.
    other = Branch(name=f"فرع {uuid.uuid4().hex[:6]}", country_code="EG", currency_code="EGP")
    db_session.add(other)
    await db_session.commit()
    second = await cashier_service.open_session(
        db_session, actor=actor, branch_id=other.id, opening_float=Decimal(0)
    )
    assert second.branch_id == other.id


async def test_sale_links_session_and_persists_tendered_change(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    cash_session = await cashier_service.open_session(
        db_session, actor=actor, branch_id=branch.id, opening_float=Decimal("100.00")
    )
    barcode = await _make_med(db_session, branch.id)

    # Cash sale: 2 strips = 60.00, customer pays 100 -> change 40.
    invoice = await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(quantity=Decimal(2), barcode=barcode)],
        cashier=actor,
        tendered=Decimal("100.00"),
    )
    assert invoice.cash_session_id == cash_session.id
    assert invoice.tendered_amount == Decimal("100.00")
    assert invoice.change_amount == Decimal("40.00")

    # Card sale: linked to the session, no tendered/change.
    card = await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(quantity=Decimal(1), barcode=barcode)],
        cashier=actor,
        payment_method="card",
    )
    assert card.cash_session_id == cash_session.id
    assert card.tendered_amount is None and card.change_amount is None

    summary = await cashier_service.session_summary(db_session, cash_session)
    assert summary["cash_count"] == 1 and summary["cash_total"] == "60.00"
    assert summary["card_count"] == 1 and summary["card_total"] == "30.00"
    assert summary["tendered_total"] == "100.00" and summary["change_total"] == "40.00"
    assert summary["expected_cash"] == "160.00"  # 100 float + 60 cash


async def test_sale_without_session_and_tendered_validation(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    bid = branch.id
    barcode = await _make_med(db_session, bid)

    # No open session -> the sale still completes, unlinked (pharmacist flow).
    invoice = await sales_service.create_sale(
        db_session,
        branch_id=bid,
        lines=[SaleLine(quantity=Decimal(1), barcode=barcode)],
        cashier=actor,
    )
    assert invoice.cash_session_id is None

    # Tendered below the AUTHORITATIVE total -> 422, nothing persisted.
    with pytest.raises(ApiError):
        await sales_service.create_sale(
            db_session,
            branch_id=bid,
            lines=[SaleLine(quantity=Decimal(2), barcode=barcode)],
            cashier=actor,
            tendered=Decimal("10.00"),
        )
    await db_session.rollback()
    await db_session.refresh(actor)

    # Tendered on a card sale -> 422.
    with pytest.raises(ApiError):
        await sales_service.create_sale(
            db_session,
            branch_id=bid,
            lines=[SaleLine(quantity=Decimal(1), barcode=barcode)],
            cashier=actor,
            payment_method="card",
            tendered=Decimal("100.00"),
        )
    await db_session.rollback()


async def test_close_freezes_z_numbers_and_audits_discrepancy(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    cash_session = await cashier_service.open_session(
        db_session, actor=actor, branch_id=branch.id, opening_float=Decimal("50.00")
    )
    barcode = await _make_med(db_session, branch.id)
    await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(quantity=Decimal(3), barcode=barcode)],  # 90.00 cash
        cashier=actor,
        tendered=Decimal("100.00"),
    )

    # Counted 135 vs expected 140 -> discrepancy -5, audited independently.
    closed = await cashier_service.close_session(
        db_session,
        actor=actor,
        cash_session=cash_session,
        counted_cash=Decimal("135.00"),
        notes="عجز ٥ جنيهات",
    )
    assert closed.status == "closed"
    assert closed.expected_cash == Decimal("140.00")
    assert closed.counted_cash == Decimal("135.00")
    assert closed.discrepancy == Decimal("-5.00")
    assert closed.closing_notes == "عجز ٥ جنيهات"
    assert closed.closed_at is not None and closed.closed_by == actor.id
    assert await _audit_count(db_session, "cash_session.closed", closed.id) == 1
    assert await _audit_count(db_session, "cash_session.discrepancy", closed.id) == 1

    # Closing again -> E-CSH-002.
    with pytest.raises(ApiError) as exc:
        await cashier_service.close_session(
            db_session, actor=actor, cash_session=closed, counted_cash=Decimal(0)
        )
    assert exc.value.code == "E-CSH-002"
    await db_session.rollback()


async def test_close_balanced_drawer_has_no_discrepancy_audit(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    cash_session = await cashier_service.open_session(
        db_session, actor=actor, branch_id=branch.id, opening_float=Decimal("20.00")
    )
    closed = await cashier_service.close_session(
        db_session, actor=actor, cash_session=cash_session, counted_cash=Decimal("20.00")
    )
    assert closed.discrepancy == Decimal("0.00")
    assert await _audit_count(db_session, "cash_session.closed", closed.id) == 1
    assert await _audit_count(db_session, "cash_session.discrepancy", closed.id) == 0


async def test_day_report_buckets(db_session: AsyncSession, actor: User, branch: Branch) -> None:
    bid = branch.id
    barcode = await _make_med(db_session, bid)

    # Outside any session first (30.00 cash).
    await sales_service.create_sale(
        db_session,
        branch_id=bid,
        lines=[SaleLine(quantity=Decimal(1), barcode=barcode)],
        cashier=actor,
    )
    # Then in-session: 60 cash + 30 card.
    cash_session = await cashier_service.open_session(
        db_session, actor=actor, branch_id=bid, opening_float=Decimal(0)
    )
    await sales_service.create_sale(
        db_session,
        branch_id=bid,
        lines=[SaleLine(quantity=Decimal(2), barcode=barcode)],
        cashier=actor,
        tendered=Decimal("60.00"),
    )
    await sales_service.create_sale(
        db_session,
        branch_id=bid,
        lines=[SaleLine(quantity=Decimal(1), barcode=barcode)],
        cashier=actor,
        payment_method="card",
    )

    report = await cashier_service.day_report(db_session, branch_id=bid, day=dt.date.today())
    assert report["cash_in_session"] == {"count": 1, "total": "60.00"}
    assert report["card_in_session"] == {"count": 1, "total": "30.00"}
    assert report["cash_outside_sessions"] == {"count": 1, "total": "30.00"}
    assert report["invoice_count"] == 3
    assert report["total_sales"] == "120.00"
    sessions = report["sessions"]
    assert isinstance(sessions, list) and len(sessions) == 1
    assert sessions[0]["id"] == str(cash_session.id)


# ------------------------------ P2-M7 payments reconciliation ------------------------------


async def test_cash_refund_reduces_expected_cash(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    cash_session = await cashier_service.open_session(
        db_session, actor=actor, branch_id=branch.id, opening_float=Decimal("50.00")
    )
    barcode = await _make_med(db_session, branch.id)
    invoice = await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(quantity=Decimal(3), barcode=barcode)],  # 90.00 cash
        cashier=actor,
        tendered=Decimal("100.00"),
    )
    before = await cashier_service.session_summary(db_session, cash_session)
    assert before["cash_total"] == "90.00" and before["expected_cash"] == "140.00"

    # Return 1 of the 3 strips (30.00) for a cash refund — mid-shift.
    item = await _first_item(db_session, invoice)
    await return_service.create_return(
        db_session,
        actor=actor,
        original_invoice_id=invoice.id,
        lines=[return_service.ReturnLine(invoice_item_id=item.id, quantity=Decimal(1))],
        refund_method="cash",
    )

    after = await cashier_service.session_summary(db_session, cash_session)
    assert after["cash_total"] == "60.00"  # 90 sold - 30 refunded (net, from payments)
    assert after["cash_refund_count"] == 1 and after["cash_refunded"] == "30.00"
    assert after["cash_count"] == 1  # still one SALE payment; the refund is tracked separately
    assert after["expected_cash"] == "110.00"  # 50 float + 60 net cash
    # tendered/change stay invoice-sourced and unaffected by the refund.
    assert after["tendered_total"] == "100.00" and after["change_total"] == "10.00"


async def test_cash_expense_reduces_expected_cash(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    """C5: a cash expense taken from the drawer reduces expected cash; a non-cash
    expense does not; close then reconciles with zero discrepancy."""
    cash_session = await cashier_service.open_session(
        db_session, actor=actor, branch_id=branch.id, opening_float=Decimal("50.00")
    )
    barcode = await _make_med(db_session, branch.id)
    await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(quantity=Decimal(3), barcode=barcode)],  # 90.00 cash
        cashier=actor,
        tendered=Decimal("100.00"),
    )
    category = await expense_service.create_category(db_session, actor=actor, name_ar="نثريات")

    # A 25.00 CASH expense out of the drawer.
    await expense_service.create_expense(
        db_session,
        actor=actor,
        branch_id=branch.id,
        expense_category_id=category.id,
        amount=Decimal("25.00"),
        expense_date=dt.date.today(),
        payment_method="cash",
    )
    # A 100.00 bank-transfer expense never touches the drawer.
    await expense_service.create_expense(
        db_session,
        actor=actor,
        branch_id=branch.id,
        expense_category_id=category.id,
        amount=Decimal("100.00"),
        expense_date=dt.date.today(),
        payment_method="bank_transfer",
    )

    summary = await cashier_service.session_summary(db_session, cash_session)
    assert summary["cash_total"] == "90.00"
    assert summary["cash_expense_count"] == 1 and summary["cash_expenses"] == "25.00"
    # 50 float + 90 cash sales - 25 cash expense = 115 (bank-transfer excluded).
    assert summary["expected_cash"] == "115.00"

    closed = await cashier_service.close_session(
        db_session, actor=actor, cash_session=cash_session, counted_cash=Decimal("115.00")
    )
    assert closed.discrepancy == Decimal("0.00")


async def test_card_refund_reduces_card_total_only(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    cash_session = await cashier_service.open_session(
        db_session, actor=actor, branch_id=branch.id, opening_float=Decimal(0)
    )
    barcode = await _make_med(db_session, branch.id)
    card_invoice = await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(quantity=Decimal(2), barcode=barcode)],  # 60.00 card
        cashier=actor,
        payment_method="card",
    )
    await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(quantity=Decimal(1), barcode=barcode)],  # 30.00 cash
        cashier=actor,
    )

    item = await _first_item(db_session, card_invoice)
    await return_service.create_return(
        db_session,
        actor=actor,
        original_invoice_id=card_invoice.id,
        lines=[return_service.ReturnLine(invoice_item_id=item.id, quantity=Decimal(1))],
        refund_method="card",
    )

    summary = await cashier_service.session_summary(db_session, cash_session)
    assert summary["card_total"] == "30.00"  # 60 - 30 refunded
    assert summary["card_refund_count"] == 1 and summary["card_refunded"] == "30.00"
    # The cash side (and expected_cash) is untouched by a CARD refund.
    assert summary["cash_total"] == "30.00" and summary["expected_cash"] == "30.00"
    assert summary["cash_refund_count"] == 0


async def test_store_credit_refund_does_not_touch_cash_or_card(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    cash_session = await cashier_service.open_session(
        db_session, actor=actor, branch_id=branch.id, opening_float=Decimal(0)
    )
    barcode = await _make_med(db_session, branch.id)
    invoice = await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(quantity=Decimal(2), barcode=barcode)],  # 60.00 cash
        cashier=actor,
    )
    item = await _first_item(db_session, invoice)
    await return_service.create_return(
        db_session,
        actor=actor,
        original_invoice_id=invoice.id,
        lines=[return_service.ReturnLine(invoice_item_id=item.id, quantity=Decimal(1))],
        refund_method="store_credit",
    )

    summary = await cashier_service.session_summary(db_session, cash_session)
    assert summary["cash_total"] == "60.00" and summary["expected_cash"] == "60.00"
    assert summary["cash_refund_count"] == 0 and summary["card_refund_count"] == 0
    assert summary["store_credit_refunded"] == "30.00"


async def test_close_session_discrepancy_reflects_mid_shift_refund(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    """Full reconciliation: a cash refund issued BEFORE close must already be
    netted into expected_cash — closing with the exact post-refund drawer count
    balances (discrepancy 0), proving close_session picks up the corrected
    number rather than the pre-refund total."""
    cash_session = await cashier_service.open_session(
        db_session, actor=actor, branch_id=branch.id, opening_float=Decimal("50.00")
    )
    barcode = await _make_med(db_session, branch.id)
    invoice = await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(quantity=Decimal(3), barcode=barcode)],  # 90.00 cash
        cashier=actor,
        tendered=Decimal("100.00"),
    )
    item = await _first_item(db_session, invoice)
    await return_service.create_return(
        db_session,
        actor=actor,
        original_invoice_id=invoice.id,
        lines=[return_service.ReturnLine(invoice_item_id=item.id, quantity=Decimal(1))],
        refund_method="cash",
    )
    # Drawer truth: 50 float + 90 sold - 30 refunded = 110, physically counted.
    closed = await cashier_service.close_session(
        db_session, actor=actor, cash_session=cash_session, counted_cash=Decimal("110.00")
    )
    assert closed.expected_cash == Decimal("110.00")
    assert closed.discrepancy == Decimal("0.00")
    assert await _audit_count(db_session, "cash_session.discrepancy", closed.id) == 0

    metadata_row = (
        await db_session.execute(
            text(
                "SELECT metadata FROM audit_logs WHERE action = 'cash_session.closed' "
                "AND entity_id = :e"
            ).bindparams(e=closed.id)
        )
    ).scalar_one()
    assert metadata_row["expected_cash"] == "110.00"


async def test_day_report_refunds_and_net_total(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    bid = branch.id
    barcode = await _make_med(db_session, bid)
    cash_session = await cashier_service.open_session(
        db_session, actor=actor, branch_id=bid, opening_float=Decimal(0)
    )
    invoice = await sales_service.create_sale(
        db_session,
        branch_id=bid,
        lines=[SaleLine(quantity=Decimal(3), barcode=barcode)],  # 90.00 cash
        cashier=actor,
    )
    item = await _first_item(db_session, invoice)
    await return_service.create_return(
        db_session,
        actor=actor,
        original_invoice_id=invoice.id,
        lines=[return_service.ReturnLine(invoice_item_id=item.id, quantity=Decimal(1))],
        refund_method="cash",
    )

    report = await cashier_service.day_report(db_session, branch_id=bid, day=dt.date.today())
    # total_sales stays GROSS (unchanged pre-existing meaning) — 90.00.
    assert report["total_sales"] == "90.00"
    assert report["refunds_cash"] == {"count": 1, "total": "30.00"}
    assert report["refunds_card"] == {"count": 0, "total": "0.00"}
    assert report["total_refunds"] == "30.00"
    assert report["net_total_sales"] == "60.00"
    sessions = report["sessions"]
    assert isinstance(sessions, list) and len(sessions) == 1
    assert sessions[0]["id"] == str(cash_session.id)


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


async def test_cashier_opens_and_reads_current_session(
    client: httpx.AsyncClient, db_session: AsyncSession, branch: Branch
) -> None:
    csrf = await _login(client, await _seed_role_user(db_session, "cashier"))
    opened = await client.post(
        "/api/v1/cashier/sessions/open",
        headers={"X-CSRF-Token": csrf},
        json={"branch_id": str(branch.id), "opening_float": "150.00"},
    )
    assert opened.status_code == 200, opened.text
    data = opened.json()["data"]
    assert data["status"] == "open" and data["opening_float"] == "150.00"

    current = await client.get(
        "/api/v1/cashier/sessions/current", params={"branch_id": str(branch.id)}
    )
    assert current.status_code == 200
    body = current.json()["data"]
    assert body["session"]["id"] == data["id"]
    assert body["summary"]["expected_cash"] == "150.00"

    dup = await client.post(
        "/api/v1/cashier/sessions/open",
        headers={"X-CSRF-Token": csrf},
        json={"branch_id": str(branch.id), "opening_float": "0"},
    )
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "E-CSH-001"


async def test_close_permission_matrix_and_flow(
    client: httpx.AsyncClient, db_session: AsyncSession, branch: Branch
) -> None:
    cashier_csrf = await _login(client, await _seed_role_user(db_session, "cashier"))
    opened = await client.post(
        "/api/v1/cashier/sessions/open",
        headers={"X-CSRF-Token": cashier_csrf},
        json={"branch_id": str(branch.id), "opening_float": "75.00"},
    )
    session_id = opened.json()["data"]["id"]

    # The cashier cannot close their own drawer (cashier.close_session).
    denied = await client.post(
        f"/api/v1/cashier/sessions/{session_id}/close",
        headers={"X-CSRF-Token": cashier_csrf},
        json={"counted_cash": "75.00"},
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "E-AUTH-002"

    # The branch manager closes it (and CSRF stays mandatory).
    manager_csrf = await _login(client, await _seed_role_user(db_session, "branch_manager"))
    no_csrf = await client.post(
        f"/api/v1/cashier/sessions/{session_id}/close", json={"counted_cash": "75.00"}
    )
    assert no_csrf.status_code == 403

    closed = await client.post(
        f"/api/v1/cashier/sessions/{session_id}/close",
        headers={"X-CSRF-Token": manager_csrf},
        json={"counted_cash": "80.00", "notes": "زيادة"},
    )
    assert closed.status_code == 200, closed.text
    data = closed.json()["data"]
    assert data["status"] == "closed"
    assert data["expected_cash"] == "75.00" and data["discrepancy"] == "5.00"


async def test_view_cash_gates_lists_and_z_report(
    client: httpx.AsyncClient, db_session: AsyncSession, branch: Branch
) -> None:
    # cashier lacks cashier.view_cash.
    await _login(client, await _seed_role_user(db_session, "cashier"))
    denied = await client.get("/api/v1/cashier/z-report", params={"branch_id": str(branch.id)})
    assert denied.status_code == 403

    await _login(client, await _seed_role_user(db_session, "branch_manager"))
    report = await client.get("/api/v1/cashier/z-report", params={"branch_id": str(branch.id)})
    assert report.status_code == 200, report.text
    body = report.json()["data"]
    assert body["date"] == dt.date.today().isoformat()
    assert "total_sales" in body and "sessions" in body

    listed = await client.get(
        "/api/v1/cashier/sessions",
        params={"branch_id": str(branch.id), "day": dt.date.today().isoformat()},
    )
    assert listed.status_code == 200
    for row in listed.json()["data"]:
        assert "cashier_username" in row
