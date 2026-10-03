"""Returns / credit notes + payments ledger (P2-M7).

A return is a separate credit note (never modifies the invoice — CLAUDE.md rule
14): stock goes back to its batch (return_in), the customer is credited at the
original price + VAT, and a NEGATIVE payment (refund) is booked. Over-returns are
refused. Sales also book a positive payment.
"""

import datetime as dt
import uuid
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.errors import ApiError, ErrorCode
from pharmaos_api.models import (
    Branch,
    InvoiceItem,
    Medication,
    MedicationBarcode,
    MedicationBatch,
    MedicationPackaging,
    Payment,
    Role,
    Settings,
    User,
)
from pharmaos_api.security.passwords import hash_password
from pharmaos_api.services import (
    customer_service,
    inventory_service,
    return_service,
    sales_service,
)
from pharmaos_api.services.sales_service import SaleLine


async def _return_batches(
    db_session: AsyncSession, branch: Branch, med_id: str
) -> list[MedicationBatch]:
    """Batches created to hold returned stock (return_in movement), newest first."""
    return list(
        (
            await db_session.execute(
                select(MedicationBatch)
                .join(
                    return_service.StockMovement,
                    return_service.StockMovement.batch_id == MedicationBatch.id,
                )
                .where(
                    MedicationBatch.branch_id == branch.id,
                    MedicationBatch.medication_id == uuid.UUID(med_id),
                    return_service.StockMovement.movement_type == "return_in",
                )
                .order_by(MedicationBatch.received_at.desc())
            )
        )
        .scalars()
        .all()
    )


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


async def _make_med(db_session: AsyncSession, *, is_medicine: bool, price: str) -> tuple[str, str]:
    unit_id = (
        await db_session.execute(
            text(
                "INSERT INTO units (name_ar) VALUES ('علبة') "
                "ON CONFLICT (name_ar) DO UPDATE SET name_ar=EXCLUDED.name_ar RETURNING id"
            )
        )
    ).scalar_one()
    await db_session.commit()
    med = Medication(
        trade_name=f"Ret {uuid.uuid4().hex[:6]}", trade_name_ar="صنف", is_medicine=is_medicine
    )
    db_session.add(med)
    await db_session.flush()
    box = MedicationPackaging(
        medication_id=med.id,
        level=1,
        unit_id=unit_id,
        name_ar="علبة",
        qty_in_parent=None,
        selling_price=Decimal(price),
        is_default_sale=True,
    )
    db_session.add(box)
    await db_session.flush()
    barcode = f"627{uuid.uuid4().int % 10**10:010d}"
    db_session.add(MedicationBarcode(medication_id=med.id, packaging_id=box.id, barcode=barcode))
    await db_session.commit()
    return str(med.id), barcode


async def _receive(db_session, actor, branch, med_id, *, qty):  # type: ignore[no-untyped-def]
    return await inventory_service.receive_stock(
        db_session,
        actor=actor,
        branch_id=branch.id,
        medication_id=uuid.UUID(med_id),
        batch_number=f"B-{uuid.uuid4().hex[:6]}",
        expiry_date=dt.date.today() + dt.timedelta(days=365),
        quantity=Decimal(qty),
        purchase_price=Decimal("1.00"),
    )


async def _sell(db_session, actor, branch, barcode, qty):  # type: ignore[no-untyped-def]
    return await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(quantity=Decimal(qty), barcode=barcode)],
        cashier=actor,
    )


async def _first_item(db_session: AsyncSession, invoice_id: uuid.UUID) -> InvoiceItem:
    item = (
        (await db_session.execute(select(InvoiceItem).where(InvoiceItem.invoice_id == invoice_id)))
        .scalars()
        .first()
    )
    assert item is not None
    return item


async def _cached(db_session: AsyncSession, branch: Branch, med_id: str) -> Decimal:
    return Decimal(
        (
            await db_session.execute(
                text(
                    "SELECT cached_quantity FROM branch_inventory "
                    "WHERE branch_id = :b AND medication_id = CAST(:m AS uuid)"
                ).bindparams(b=branch.id, m=med_id)
            )
        ).scalar_one()
    )


# ------------------------------ roundtrip + ledger ------------------------------


async def test_full_return_roundtrip(db_session: AsyncSession, actor: User, branch: Branch) -> None:
    med_id, bc = await _make_med(db_session, is_medicine=True, price="50.00")
    batch = await _receive(db_session, actor, branch, med_id, qty="100")
    invoice = await _sell(db_session, actor, branch, bc, "3")
    item = await _first_item(db_session, invoice.id)

    credit = await return_service.create_return(
        db_session,
        actor=actor,
        original_invoice_id=invoice.id,
        lines=[return_service.ReturnLine(invoice_item_id=item.id, quantity=Decimal(2))],
        reason="تالف",
        refund_method="cash",
    )
    assert credit.total == Decimal("100.00")  # 2 x 50, medicine exempt -> tax 0
    assert credit.tax_amount == Decimal("0.00")

    # Returned stock lands in a DISTINCT quarantined batch (plan D3 default) — the
    # ORIGINAL batch is untouched (97 = 100 - 3 sold) and the returned 2 are NOT
    # yet sellable, so the derived cache stays at 97 until a pharmacist releases.
    await db_session.refresh(batch)
    assert batch.quantity == Decimal("97") and batch.status == "active"
    assert await _cached(db_session, branch, med_id) == Decimal("97")
    ret_batches = await _return_batches(db_session, branch, med_id)
    assert len(ret_batches) == 1
    assert ret_batches[0].quantity == Decimal("2") and ret_batches[0].status == "quarantined"

    # A negative payment (refund) is booked against the return.
    refund = (
        await db_session.execute(select(Payment).where(Payment.return_id == credit.id))
    ).scalar_one()
    assert refund.amount == Decimal("-100.00") and refund.method == "cash"

    # The original invoice is UNCHANGED (rule 14).
    await db_session.refresh(invoice)
    assert invoice.status == "completed" and invoice.total == Decimal("150.00")

    # Audited.
    audited = (
        await db_session.execute(
            text("SELECT COUNT(*) FROM audit_logs WHERE action = 'return.created'")
        )
    ).scalar_one()
    assert audited >= 1


async def test_sale_books_positive_payment(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_id, bc = await _make_med(db_session, is_medicine=True, price="30.00")
    await _receive(db_session, actor, branch, med_id, qty="10")
    invoice = await _sell(db_session, actor, branch, bc, "2")
    payment = (
        await db_session.execute(select(Payment).where(Payment.invoice_id == invoice.id))
    ).scalar_one()
    assert payment.amount == Decimal("60.00") and payment.method == "cash"


async def test_over_return_rejected(db_session: AsyncSession, actor: User, branch: Branch) -> None:
    med_id, bc = await _make_med(db_session, is_medicine=True, price="50.00")
    await _receive(db_session, actor, branch, med_id, qty="100")
    invoice = await _sell(db_session, actor, branch, bc, "3")
    item = await _first_item(db_session, invoice.id)

    await return_service.create_return(
        db_session,
        actor=actor,
        original_invoice_id=invoice.id,
        lines=[return_service.ReturnLine(invoice_item_id=item.id, quantity=Decimal(2))],
    )
    # Only 1 remains returnable; asking for 2 more is refused.
    with pytest.raises(ApiError) as exc:
        await return_service.create_return(
            db_session,
            actor=actor,
            original_invoice_id=invoice.id,
            lines=[return_service.ReturnLine(invoice_item_id=item.id, quantity=Decimal(2))],
        )
    assert exc.value.code == ErrorCode.VALIDATION_FAILED


async def test_partial_return_and_returnable_view(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_id, bc = await _make_med(db_session, is_medicine=True, price="50.00")
    await _receive(db_session, actor, branch, med_id, qty="100")
    invoice = await _sell(db_session, actor, branch, bc, "3")
    item = await _first_item(db_session, invoice.id)

    await return_service.create_return(
        db_session,
        actor=actor,
        original_invoice_id=invoice.id,
        lines=[return_service.ReturnLine(invoice_item_id=item.id, quantity=Decimal(1))],
    )
    view = await return_service.get_returnable(db_session, invoice.id)
    line = view["lines"][0]  # type: ignore[index]
    assert line["sold_qty"] == "3.000"
    assert line["returned_qty"] == "1.000"
    assert line["returnable_qty"] == "2.000"


async def test_lookup_by_invoice_number(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    """The returns UI resolves a human-facing invoice number — never a UUID."""
    med_id, bc = await _make_med(db_session, is_medicine=True, price="40.00")
    await _receive(db_session, actor, branch, med_id, qty="10")
    invoice = await _sell(db_session, actor, branch, bc, "1")

    view = await return_service.get_returnable_by_number(
        db_session, branch_id=branch.id, invoice_number=invoice.invoice_number
    )
    assert view["invoice_id"] == str(invoice.id)
    assert view["lines"][0]["returnable_qty"] == "1.000"  # type: ignore[index]

    with pytest.raises(ApiError) as exc:
        await return_service.get_returnable_by_number(
            db_session, branch_id=branch.id, invoice_number="INV-NOPE-0000"
        )
    assert exc.value.code == ErrorCode.VALIDATION_FAILED

    # A number that exists but in a DIFFERENT branch must not resolve either.
    other_branch = Branch(
        name=f"فرع {uuid.uuid4().hex[:6]}", country_code="EG", currency_code="EGP"
    )
    db_session.add(other_branch)
    await db_session.commit()
    with pytest.raises(ApiError):
        await return_service.get_returnable_by_number(
            db_session, branch_id=other_branch.id, invoice_number=invoice.invoice_number
        )


async def test_return_summary_carries_original_invoice_number(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_id, bc = await _make_med(db_session, is_medicine=True, price="50.00")
    await _receive(db_session, actor, branch, med_id, qty="100")
    invoice = await _sell(db_session, actor, branch, bc, "2")
    item = await _first_item(db_session, invoice.id)
    credit = await return_service.create_return(
        db_session,
        actor=actor,
        original_invoice_id=invoice.id,
        lines=[return_service.ReturnLine(invoice_item_id=item.id, quantity=Decimal(1))],
    )

    detail = await return_service.get_return(db_session, credit.id)
    assert detail["original_invoice_number"] == invoice.invoice_number

    rows, total = await return_service.list_returns(db_session, branch_id=branch.id)
    assert total >= 1
    assert any(
        r["id"] == str(credit.id) and r["original_invoice_number"] == invoice.invoice_number
        for r in rows
    )


async def test_taxable_return_credits_vat(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_id, bc = await _make_med(db_session, is_medicine=False, price="114.00")
    await _receive(db_session, actor, branch, med_id, qty="10")
    invoice = await _sell(db_session, actor, branch, bc, "1")
    item = await _first_item(db_session, invoice.id)

    credit = await return_service.create_return(
        db_session,
        actor=actor,
        original_invoice_id=invoice.id,
        lines=[return_service.ReturnLine(invoice_item_id=item.id, quantity=Decimal(1))],
    )
    # 114 inclusive @ 14% -> credit 114, VAT 14, net 100 (mirrors the sale).
    assert credit.total == Decimal("114.00")
    assert credit.tax_amount == Decimal("14.00")
    assert credit.subtotal == Decimal("100.00")


async def test_returned_stock_quarantined_by_default(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    """Plan D3 / review C6: returned stock is quarantined (not resold) by default,
    and only becomes sellable once a pharmacist releases the return batch."""
    med_id, bc = await _make_med(db_session, is_medicine=True, price="20.00")
    batch = await _receive(db_session, actor, branch, med_id, qty="2")
    invoice = await _sell(db_session, actor, branch, bc, "2")  # depletes the batch
    await db_session.refresh(batch)
    assert batch.status == "depleted" and batch.quantity == Decimal("0")
    assert await _cached(db_session, branch, med_id) == Decimal("0")

    item = await _first_item(db_session, invoice.id)
    await return_service.create_return(
        db_session,
        actor=actor,
        original_invoice_id=invoice.id,
        lines=[return_service.ReturnLine(invoice_item_id=item.id, quantity=Decimal(1))],
    )
    # The original batch stays depleted; the returned unit sits in a NEW
    # quarantined batch and is NOT counted as sellable stock yet.
    await db_session.refresh(batch)
    assert batch.status == "depleted" and batch.quantity == Decimal("0")
    assert await _cached(db_session, branch, med_id) == Decimal("0")
    ret_batches = await _return_batches(db_session, branch, med_id)
    assert len(ret_batches) == 1 and ret_batches[0].status == "quarantined"
    assert ret_batches[0].quantity == Decimal("1")

    # A pharmacist releasing the return batch makes it sellable (cache catches up).
    await inventory_service.set_batch_status(
        db_session, actor=actor, batch=ret_batches[0], status="active", reason="inspected"
    )
    assert await _cached(db_session, branch, med_id) == Decimal("1")


async def test_returned_stock_active_when_branch_opts_in(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    """A branch may opt in to returning stock straight to sellable (D3 setting)."""
    db_session.add(
        Settings(branch_id=branch.id, pharmacy_name="صيدلية", returned_stock_to_active=True)
    )
    await db_session.commit()
    med_id, bc = await _make_med(db_session, is_medicine=True, price="20.00")
    await _receive(db_session, actor, branch, med_id, qty="5")
    invoice = await _sell(db_session, actor, branch, bc, "3")
    assert await _cached(db_session, branch, med_id) == Decimal("2")

    item = await _first_item(db_session, invoice.id)
    await return_service.create_return(
        db_session,
        actor=actor,
        original_invoice_id=invoice.id,
        lines=[return_service.ReturnLine(invoice_item_id=item.id, quantity=Decimal(1))],
    )
    ret_batches = await _return_batches(db_session, branch, med_id)
    assert len(ret_batches) == 1 and ret_batches[0].status == "active"
    # Immediately sellable: 5 - 3 sold + 1 returned-to-active = 3.
    assert await _cached(db_session, branch, med_id) == Decimal("3")


async def test_payment_source_is_xor(db_session: AsyncSession, branch: Branch) -> None:
    """Review D3: the payments source constraint is a true XOR — a row linked to
    NEITHER a sale nor a return (and, symmetrically, one linked to both) is
    rejected by the database, not merely by convention."""
    with pytest.raises(IntegrityError):
        await db_session.execute(
            text(
                "INSERT INTO payments (branch_id, amount, method) "
                "VALUES (:b, 10, 'cash')"  # neither invoice_id nor return_id
            ).bindparams(b=branch.id)
        )
    await db_session.rollback()


async def test_loyalty_points_reversed_on_return(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    """Points earned on a sale are reversed (clamped) when it is returned (C4)."""
    med_id, bc = await _make_med(db_session, is_medicine=True, price="50.00")
    await _receive(db_session, actor, branch, med_id, qty="10")
    customer = await customer_service.create_customer(db_session, actor=actor, name="عميل ولاء")

    invoice = await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(quantity=Decimal(2), barcode=bc)],
        cashier=actor,
        customer_id=customer.id,
    )
    await db_session.refresh(customer)
    assert customer.loyalty_points == 100  # 2 x 50, 1 pt / EGP

    item = await _first_item(db_session, invoice.id)
    await return_service.create_return(
        db_session,
        actor=actor,
        original_invoice_id=invoice.id,
        lines=[return_service.ReturnLine(invoice_item_id=item.id, quantity=Decimal(1))],
    )
    # Returned 1 of 2 (50 EGP) → 50 points reversed, balance 100 - 50 = 50.
    await db_session.refresh(customer)
    assert customer.loyalty_points == 50


# ------------------------------ API permissions ------------------------------


async def _seed(db_session: AsyncSession, role_code: str) -> str:
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


async def test_return_api_permissions_and_csrf(
    client: httpx.AsyncClient, db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_id, bc = await _make_med(db_session, is_medicine=True, price="50.00")
    await _receive(db_session, actor, branch, med_id, qty="100")
    invoice = await _sell(db_session, actor, branch, bc, "3")
    item = await _first_item(db_session, invoice.id)
    body = {
        "original_invoice_id": str(invoice.id),
        "lines": [{"invoice_item_id": str(item.id), "quantity": "1"}],
        "refund_method": "cash",
    }

    # cashier holds sales.view (can see returnable) but NOT sales.return.
    cashier_csrf = await _login(client, await _seed(db_session, "cashier"))
    assert (await client.get(f"/api/v1/invoices/{invoice.id}/returnable")).status_code == 200
    forbidden = await client.post(
        "/api/v1/returns", headers={"X-CSRF-Token": cashier_csrf}, json=body
    )
    assert forbidden.status_code == 403 and forbidden.json()["error"]["code"] == "E-AUTH-002"

    # pharmacist holds sales.return; CSRF is mandatory.
    ph_csrf = await _login(client, await _seed(db_session, "pharmacist"))
    no_csrf = await client.post("/api/v1/returns", json=body)
    assert no_csrf.status_code == 403 and no_csrf.json()["error"]["code"] == "E-AUTH-004"
    ok = await client.post("/api/v1/returns", headers={"X-CSRF-Token": ph_csrf}, json=body)
    assert ok.status_code == 200, ok.text
    data = ok.json()["data"]
    assert data["total"] == "50.00" and len(data["items"]) == 1


async def test_invoice_lookup_endpoint(
    client: httpx.AsyncClient, db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_id, bc = await _make_med(db_session, is_medicine=True, price="20.00")
    await _receive(db_session, actor, branch, med_id, qty="10")
    invoice = await _sell(db_session, actor, branch, bc, "1")

    await _login(client, await _seed(db_session, "cashier"))
    found = await client.get(
        "/api/v1/invoices/lookup",
        params={"branch_id": str(branch.id), "invoice_number": invoice.invoice_number},
    )
    assert found.status_code == 200, found.text
    assert found.json()["data"]["invoice_id"] == str(invoice.id)

    missing = await client.get(
        "/api/v1/invoices/lookup",
        params={"branch_id": str(branch.id), "invoice_number": "INV-NOPE-9999"},
    )
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "E-VAL-001"
