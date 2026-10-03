"""Customer management + loyalty (P2-M5): PII encrypted at rest (AES-256-GCM),
Arabic-name / phone search, the CRUD permission tiers, and the minimal loyalty
system — points accrue atomically on a sale, manual adjust cannot go negative,
and the derived balance always equals the ledger sum.
"""

import datetime as dt
import uuid
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.errors import ApiError, ErrorCode
from pharmaos_api.models import (
    Branch,
    Medication,
    MedicationBarcode,
    MedicationPackaging,
    Role,
    User,
)
from pharmaos_api.security.passwords import hash_password
from pharmaos_api.services import customer_service, inventory_service, sales_service
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


async def _make_sellable_med(db_session: AsyncSession) -> tuple[str, str]:
    """Strip-barcode med (10 tablets/strip @ 30.00). Returns (med_id, barcode)."""
    unit_id = (
        await db_session.execute(
            text(
                "INSERT INTO units (name_ar) VALUES ('شريط') "
                "ON CONFLICT (name_ar) DO UPDATE SET name_ar=EXCLUDED.name_ar RETURNING id"
            )
        )
    ).scalar_one()
    await db_session.commit()
    med = Medication(trade_name=f"M5Med {uuid.uuid4().hex[:6]}", trade_name_ar="دواء للبيع")
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
    barcode = f"625{uuid.uuid4().int % 10**10:010d}"
    db_session.add(MedicationBarcode(medication_id=med.id, packaging_id=strip.id, barcode=barcode))
    await db_session.commit()
    return str(med.id), barcode


# ------------------------------ PII encryption ------------------------------


async def test_pii_encrypted_at_rest_and_roundtrip(db_session: AsyncSession, actor: User) -> None:
    national_id = "29901234501234"
    customer = await customer_service.create_customer(
        db_session,
        actor=actor,
        name="أحمد محمد",
        phone="01000000001",
        national_id=national_id,
        insurance_number="INS-77812",
    )
    # The stored column is ciphertext bytes — never the plaintext.
    raw = (
        await db_session.execute(
            text("SELECT national_id_encrypted FROM customers WHERE id = :i").bindparams(
                i=customer.id
            )
        )
    ).scalar_one()
    raw_bytes = bytes(raw)
    assert national_id.encode() not in raw_bytes
    assert len(raw_bytes) > 12  # nonce + ciphertext + tag

    # An authorized detail read decrypts back to the originals.
    view = customer_service.detail(customer)
    assert view["national_id"] == national_id
    assert view["insurance_number"] == "INS-77812"
    # name + phone stay plaintext (searchable).
    assert view["name"] == "أحمد محمد" and view["phone"] == "01000000001"


async def test_update_reencrypts_and_can_clear(db_session: AsyncSession, actor: User) -> None:
    customer = await customer_service.create_customer(
        db_session, actor=actor, name="سعاد", national_id="11111111111111"
    )
    await customer_service.update_customer(
        db_session, actor=actor, customer=customer, updates={"national_id": "22222222222222"}
    )
    assert customer_service.detail(customer)["national_id"] == "22222222222222"
    # Clearing with an empty string nulls the ciphertext.
    await customer_service.update_customer(
        db_session, actor=actor, customer=customer, updates={"national_id": ""}
    )
    assert customer.national_id_encrypted is None
    assert customer_service.detail(customer)["national_id"] is None


async def test_search_by_arabic_name_and_phone(db_session: AsyncSession, actor: User) -> None:
    tag = uuid.uuid4().hex[:6]
    # A digit-only phone token distinct from the names, so a phone search matches
    # exactly one customer (a shared token would trigram-cross-match the names).
    phone = f"0111{uuid.uuid4().int % 10**7:07d}"
    await customer_service.create_customer(
        db_session, actor=actor, name=f"مصطفى {tag}", phone=phone
    )
    await customer_service.create_customer(db_session, actor=actor, name=f"خالد {tag}")

    by_name, total_name = await customer_service.list_customers(db_session, search=f"مصطفى {tag}")
    assert total_name >= 1 and any(f"مصطفى {tag}" == r["name"] for r in by_name)

    by_phone, total_phone = await customer_service.list_customers(db_session, search=phone)
    assert total_phone == 1 and by_phone[0]["name"] == f"مصطفى {tag}"
    # List view never leaks PII.
    assert "national_id" not in by_phone[0]


# ------------------------------ loyalty accrual ------------------------------


async def _receive(db_session: AsyncSession, actor: User, branch: Branch, med_id: str) -> None:
    await inventory_service.receive_stock(
        db_session,
        actor=actor,
        branch_id=branch.id,
        medication_id=uuid.UUID(med_id),
        batch_number=f"B-{uuid.uuid4().hex[:6]}",
        expiry_date=dt.date.today() + dt.timedelta(days=365),
        quantity=Decimal(100),
        purchase_price=Decimal("2.00"),
    )


async def test_sale_accrues_loyalty_points_atomically(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_id, barcode = await _make_sellable_med(db_session)
    await _receive(db_session, actor, branch, med_id)
    customer = await customer_service.create_customer(db_session, actor=actor, name="زبون وفيّ")

    # 2 strips x 30.00 = 60.00 total -> 60 points (1 point / EGP).
    invoice = await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(quantity=Decimal(2), barcode=barcode)],
        cashier=actor,
        customer_id=customer.id,
    )
    assert invoice.customer_id == customer.id
    assert invoice.total == Decimal("60.00")

    await db_session.refresh(customer)
    assert customer.loyalty_points == 60
    # Balance equals the ledger sum (ledger is truth).
    assert await customer_service.recompute_points(db_session, customer.id) == 60
    txns, total = await customer_service.list_loyalty(db_session, customer_id=customer.id)
    assert total == 1 and txns[0]["txn_type"] == "earn" and txns[0]["points_delta"] == 60

    # And the sale appears in the customer's purchase history.
    history = await customer_service.customer_history(db_session, customer_id=customer.id)
    assert len(history) == 1 and history[0]["invoice_number"] == invoice.invoice_number


async def test_redeem_points_discounts_sale(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    """C3: redeeming points applies a 1:1 discount, consumes the points, then the
    customer earns on the amount actually paid — all atomic with the sale."""
    med_id, barcode = await _make_sellable_med(db_session)
    await _receive(db_session, actor, branch, med_id)
    customer = await customer_service.create_customer(db_session, actor=actor, name="مستبدِل")
    # Seed a balance with a first sale (2 x 30 = 60 -> 60 points).
    await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(quantity=Decimal(2), barcode=barcode)],
        cashier=actor,
        customer_id=customer.id,
    )
    await db_session.refresh(customer)
    assert customer.loyalty_points == 60

    # Second sale (gross 60) redeeming 20 points -> discount 20, total 40.
    invoice = await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(quantity=Decimal(2), barcode=barcode)],
        cashier=actor,
        customer_id=customer.id,
        redeem_points=20,
    )
    assert invoice.discount_amount == Decimal("20.00")
    assert invoice.total == Decimal("40.00")
    assert invoice.subtotal == Decimal("60.00")  # net stays gross-of-VAT (medicine exempt)

    # Balance: 60 - 20 redeemed + 40 earned on the paid amount = 80.
    await db_session.refresh(customer)
    assert customer.loyalty_points == 80
    assert await customer_service.recompute_points(db_session, customer.id) == 80

    # A discount is audited (invoice.discount_applied).
    audited = (
        await db_session.execute(
            text(
                "SELECT COUNT(*) FROM audit_logs WHERE action = 'invoice.discount_applied' "
                "AND entity_id = :i"
            ).bindparams(i=invoice.id)
        )
    ).scalar_one()
    assert audited == 1


async def test_redeem_requires_customer(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_id, barcode = await _make_sellable_med(db_session)
    await _receive(db_session, actor, branch, med_id)
    with pytest.raises(ApiError) as exc:
        await sales_service.create_sale(
            db_session,
            branch_id=branch.id,
            lines=[SaleLine(quantity=Decimal(1), barcode=barcode)],
            cashier=actor,
            redeem_points=5,
        )
    assert exc.value.code == ErrorCode.VALIDATION_FAILED


async def test_redeem_over_balance_rolls_back(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_id, barcode = await _make_sellable_med(db_session)
    await _receive(db_session, actor, branch, med_id)
    customer = await customer_service.create_customer(db_session, actor=actor, name="بلا رصيد")
    # No points yet; redeeming 10 (also within the 30.00 sale) must be refused.
    with pytest.raises(ApiError) as exc:
        await sales_service.create_sale(
            db_session,
            branch_id=branch.id,
            lines=[SaleLine(quantity=Decimal(1), barcode=barcode)],
            cashier=actor,
            customer_id=customer.id,
            redeem_points=10,
        )
    assert exc.value.code == ErrorCode.VALIDATION_FAILED
    await db_session.rollback()
    await db_session.refresh(customer)
    assert customer.loyalty_points == 0
    count = (
        await db_session.execute(
            text("SELECT COUNT(*) FROM invoices WHERE customer_id = :c").bindparams(c=customer.id)
        )
    ).scalar_one()
    assert count == 0


async def test_sale_with_unknown_customer_rolls_back(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_id, barcode = await _make_sellable_med(db_session)
    await _receive(db_session, actor, branch, med_id)
    ghost = uuid.uuid4()

    with pytest.raises(ApiError) as exc:
        await sales_service.create_sale(
            db_session,
            branch_id=branch.id,
            lines=[SaleLine(quantity=Decimal(1), barcode=barcode)],
            cashier=actor,
            customer_id=ghost,
        )
    assert exc.value.code == ErrorCode.VALIDATION_FAILED
    await db_session.rollback()
    # Atomicity: nothing persisted for that ghost customer.
    count = (
        await db_session.execute(
            text("SELECT COUNT(*) FROM invoices WHERE customer_id = :c").bindparams(c=ghost)
        )
    ).scalar_one()
    assert count == 0


async def test_manual_adjust_cannot_go_negative(db_session: AsyncSession, actor: User) -> None:
    customer = await customer_service.create_customer(db_session, actor=actor, name="حساب نقاط")
    await customer_service.adjust_points(
        db_session, actor=actor, customer=customer, points_delta=50, reason="ترحيل يدوي"
    )
    assert customer.loyalty_points == 50

    # The guard raises from pure Python BEFORE any write — the session stays
    # clean and the balance is untouched (no rollback needed).
    with pytest.raises(ApiError) as exc:
        await customer_service.adjust_points(
            db_session, actor=actor, customer=customer, points_delta=-100, reason="خصم"
        )
    assert exc.value.code == ErrorCode.VALIDATION_FAILED
    assert customer.loyalty_points == 50

    await customer_service.adjust_points(
        db_session, actor=actor, customer=customer, points_delta=-30, reason="استبدال"
    )
    assert customer.loyalty_points == 20
    assert await customer_service.recompute_points(db_session, customer.id) == 20


# ------------------------------ API layer + permissions ------------------------------


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


async def test_crud_permission_tiers(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    # cashier holds customers.create -> can register a walk-in customer.
    cashier_csrf = await _login(client, await _seed_user(db_session, "cashier"))
    created = await client.post(
        "/api/v1/customers",
        headers={"X-CSRF-Token": cashier_csrf},
        json={"name": "عميل الكاشير", "phone": "01200000000", "national_id": "30001011200000"},
    )
    assert created.status_code == 200, created.text
    cid = created.json()["data"]["id"]
    assert created.json()["data"]["national_id"] == "30001011200000"  # decrypted on the write echo

    # data_entry lacks customers.create.
    de_csrf = await _login(client, await _seed_user(db_session, "data_entry"))
    forbidden = await client.post(
        "/api/v1/customers", headers={"X-CSRF-Token": de_csrf}, json={"name": "x"}
    )
    assert forbidden.status_code == 403 and forbidden.json()["error"]["code"] == "E-AUTH-002"

    # cashier can VIEW but NOT edit (customers.edit excludes cashier).
    detail = await client.get(f"/api/v1/customers/{cid}")
    assert detail.status_code == 200 and detail.json()["data"]["name"] == "عميل الكاشير"
    cashier_edit = await client.patch(
        f"/api/v1/customers/{cid}",
        headers={"X-CSRF-Token": cashier_csrf},
        json={"name": "محاولة"},
    )
    assert cashier_edit.status_code == 403

    # pharmacist can edit.
    ph_csrf = await _login(client, await _seed_user(db_session, "pharmacist"))
    ok_edit = await client.patch(
        f"/api/v1/customers/{cid}",
        headers={"X-CSRF-Token": ph_csrf},
        json={"phone": "01099999999"},
    )
    assert ok_edit.status_code == 200 and ok_edit.json()["data"]["phone"] == "01099999999"

    # delete is super_admin only: pharmacist forbidden, super_admin ok.
    ph_delete = await client.delete(f"/api/v1/customers/{cid}", headers={"X-CSRF-Token": ph_csrf})
    assert ph_delete.status_code == 403
    sa_csrf = await _login(client, await _seed_user(db_session, "super_admin"))
    sa_delete = await client.delete(f"/api/v1/customers/{cid}", headers={"X-CSRF-Token": sa_csrf})
    assert sa_delete.status_code == 200 and sa_delete.json()["data"]["deleted"] is True


async def test_loyalty_endpoints(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    sa_csrf = await _login(client, await _seed_user(db_session, "super_admin"))
    cid = (
        await client.post(
            "/api/v1/customers", headers={"X-CSRF-Token": sa_csrf}, json={"name": "نقاط API"}
        )
    ).json()["data"]["id"]

    adjust = await client.post(
        f"/api/v1/customers/{cid}/loyalty",
        headers={"X-CSRF-Token": sa_csrf},
        json={"points_delta": 120, "reason": "رصيد افتتاحي"},
    )
    assert adjust.status_code == 200 and adjust.json()["data"]["loyalty_points"] == 120

    ledger = await client.get(f"/api/v1/customers/{cid}/loyalty")
    assert ledger.status_code == 200
    body = ledger.json()["data"]
    assert body["balance"] == 120 and body["transactions"][0]["txn_type"] == "adjust"

    # CSRF is mandatory on the mutation.
    no_csrf = await client.post(
        f"/api/v1/customers/{cid}/loyalty", json={"points_delta": 5, "reason": "x"}
    )
    assert no_csrf.status_code == 403 and no_csrf.json()["error"]["code"] == "E-AUTH-004"
