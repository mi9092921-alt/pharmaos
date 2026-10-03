"""VAT per tax_profile (P2-M6). Prices are VAT-INCLUSIVE, so the customer total
equals the sum of shelf prices and VAT is EXTRACTED from it. Medicines follow the
profile's medicine_vat_rate (NULL = exempt, the Egyptian default); non-medicine
SKUs use the standard vat_rate. Every invoice + line snapshots the tax at issue.
"""

import datetime as dt
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.models import (
    Branch,
    InvoiceItem,
    Medication,
    MedicationBarcode,
    MedicationPackaging,
    User,
)
from pharmaos_api.services import inventory_service, sales_service, tax_service
from pharmaos_api.services.sales_service import SaleLine


@pytest.fixture
async def actor(db_session: AsyncSession, seeded_user: dict) -> User:
    return (
        await db_session.execute(select(User).where(User.username == seeded_user["username"]))
    ).scalar_one()


@pytest.fixture
async def branch(db_session: AsyncSession) -> Branch:
    # EG country is seeded (tax_profile: 14% standard, medicine exempt/NULL).
    b = Branch(name=f"فرع {uuid.uuid4().hex[:6]}", country_code="EG", currency_code="EGP")
    db_session.add(b)
    await db_session.commit()
    return b


async def _make_med(db_session: AsyncSession, *, is_medicine: bool, price: str) -> tuple[str, str]:
    """A single-level (box) sellable med at `price`. Returns (med_id, barcode)."""
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
        trade_name=f"Vat {uuid.uuid4().hex[:6]}", trade_name_ar="صنف", is_medicine=is_medicine
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
    barcode = f"626{uuid.uuid4().int % 10**10:010d}"
    db_session.add(MedicationBarcode(medication_id=med.id, packaging_id=box.id, barcode=barcode))
    await db_session.commit()
    return str(med.id), barcode


async def _receive(
    db_session: AsyncSession, actor: User, branch: Branch, med_id: str, *, qty: str
) -> None:
    await inventory_service.receive_stock(
        db_session,
        actor=actor,
        branch_id=branch.id,
        medication_id=uuid.UUID(med_id),
        batch_number=f"B-{uuid.uuid4().hex[:6]}",
        expiry_date=dt.date.today() + dt.timedelta(days=365),
        quantity=Decimal(qty),
        purchase_price=Decimal("1.00"),
    )


# ------------------------------ pure computation ------------------------------


def test_split_inclusive_extracts_vat() -> None:
    net, vat = tax_service.split_inclusive(Decimal("114.00"), Decimal("14"))
    assert net == Decimal("100.00") and vat == Decimal("14.00")
    # Rounding (half-up) on a non-clean gross.
    net2, vat2 = tax_service.split_inclusive(Decimal("100.00"), Decimal("14"))
    assert vat2 == Decimal("12.28") and net2 == Decimal("87.72")
    # Exempt / no rate — the whole amount is net.
    assert tax_service.split_inclusive(Decimal("50.00"), Decimal("0")) == (
        Decimal("50.00"),
        Decimal("0.00"),
    )


def test_rate_for_medicine_vs_standard() -> None:
    exempt = tax_service.TaxProfile("EG", Decimal("14"), None, "eta_ereceipt")
    assert tax_service.rate_for(exempt, is_medicine=True) == Decimal("0")
    assert tax_service.rate_for(exempt, is_medicine=False) == Decimal("14")
    reduced = tax_service.TaxProfile("X", Decimal("14"), Decimal("5"), None)
    assert tax_service.rate_for(reduced, is_medicine=True) == Decimal("5")
    # No profile configured -> 0% either way.
    assert tax_service.rate_for(None, is_medicine=False) == Decimal("0")


async def test_resolve_for_branch_eg(db_session: AsyncSession, branch: Branch) -> None:
    profile = await tax_service.resolve_for_branch(db_session, branch.id)
    assert profile is not None
    assert profile.vat_rate == Decimal("14.00")
    assert profile.medicine_vat_rate is None  # Egyptian medicines are exempt
    assert profile.einvoice_system == "eta_ereceipt"


# ------------------------------ sale VAT ------------------------------


async def test_exempt_medicine_sale_has_zero_vat(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_id, barcode = await _make_med(db_session, is_medicine=True, price="90.00")
    await _receive(db_session, actor, branch, med_id, qty="10")
    invoice = await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(quantity=Decimal(1), barcode=barcode)],
        cashier=actor,
    )
    assert invoice.tax_amount == Decimal("0.00")
    assert invoice.subtotal == invoice.total == Decimal("90.00")
    item = (
        await db_session.execute(select(InvoiceItem).where(InvoiceItem.invoice_id == invoice.id))
    ).scalar_one()
    assert item.tax_rate == Decimal("0.00") and item.tax_amount == Decimal("0.00")


async def test_non_medicine_sale_extracts_inclusive_vat(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_id, barcode = await _make_med(db_session, is_medicine=False, price="114.00")
    await _receive(db_session, actor, branch, med_id, qty="10")
    invoice = await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(quantity=Decimal(1), barcode=barcode)],
        cashier=actor,
    )
    # 114.00 inclusive @ 14% -> 14.00 VAT, 100.00 net; customer total stays 114.
    assert invoice.total == Decimal("114.00")
    assert invoice.tax_amount == Decimal("14.00")
    assert invoice.subtotal == Decimal("100.00")
    item = (
        await db_session.execute(select(InvoiceItem).where(InvoiceItem.invoice_id == invoice.id))
    ).scalar_one()
    assert item.tax_rate == Decimal("14.00") and item.tax_amount == Decimal("14.00")


async def test_mixed_basket_taxes_only_non_medicine(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_id, med_bc = await _make_med(db_session, is_medicine=True, price="50.00")
    other_id, other_bc = await _make_med(db_session, is_medicine=False, price="114.00")
    await _receive(db_session, actor, branch, med_id, qty="10")
    await _receive(db_session, actor, branch, other_id, qty="10")

    invoice = await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[
            SaleLine(quantity=Decimal(1), barcode=med_bc),
            SaleLine(quantity=Decimal(1), barcode=other_bc),
        ],
        cashier=actor,
    )
    # Only the non-medicine line carries VAT (14 of its 114).
    assert invoice.total == Decimal("164.00")
    assert invoice.tax_amount == Decimal("14.00")
    assert invoice.subtotal == Decimal("150.00")


async def test_multi_batch_line_vat_sums_to_line(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_id, barcode = await _make_med(db_session, is_medicine=False, price="114.00")
    # Two batches of 1 unit each -> a 2-unit sale is split across both (FEFO).
    await _receive(db_session, actor, branch, med_id, qty="1")
    await _receive(db_session, actor, branch, med_id, qty="1")

    invoice = await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(quantity=Decimal(2), barcode=barcode)],
        cashier=actor,
    )
    assert invoice.total == Decimal("228.00")
    assert invoice.tax_amount == Decimal("28.00")
    items = list(
        (
            await db_session.execute(
                select(InvoiceItem).where(InvoiceItem.invoice_id == invoice.id)
            )
        ).scalars()
    )
    assert len(items) == 2
    assert sum((i.tax_amount for i in items), Decimal(0)) == Decimal("28.00")
    assert all(i.tax_rate == Decimal("14.00") for i in items)


# ------------------------------ receipt VAT line ------------------------------


async def test_receipt_renders_vat_line_only_when_taxed(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    from pharmaos_api.services import receipt_service

    taxed_id, taxed_bc = await _make_med(db_session, is_medicine=False, price="114.00")
    exempt_id, exempt_bc = await _make_med(db_session, is_medicine=True, price="90.00")
    await _receive(db_session, actor, branch, taxed_id, qty="10")
    await _receive(db_session, actor, branch, exempt_id, qty="10")

    taxed = await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(quantity=Decimal(1), barcode=taxed_bc)],
        cashier=actor,
    )
    receipt = await receipt_service.load_invoice_receipt(db_session, taxed.id)
    assert receipt.tax == Decimal("14.00")
    payload = receipt_service.to_escpos(receipt, open_drawer=False)
    assert "ض.ق.م".encode() in payload and b"14.00" in payload

    exempt = await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(quantity=Decimal(1), barcode=exempt_bc)],
        cashier=actor,
    )
    exempt_receipt = await receipt_service.load_invoice_receipt(db_session, exempt.id)
    assert exempt_receipt.tax == Decimal("0.00")
    exempt_payload = receipt_service.to_escpos(exempt_receipt, open_drawer=False)
    assert "ض.ق.م".encode() not in exempt_payload


# ------------------------------ catalog is_medicine flag (API) ------------------------------


async def test_catalog_create_non_medicine_via_api(client, db_session: AsyncSession) -> None:  # type: ignore[no-untyped-def]
    from pharmaos_api.models import Role
    from pharmaos_api.security.passwords import hash_password

    role = (await db_session.execute(select(Role).where(Role.code == "super_admin"))).scalar_one()
    username = f"sa_{uuid.uuid4().hex[:8]}"
    db_session.add(
        User(
            username=username,
            full_name="م",
            password_hash=hash_password("T3st@user!"),
            role_id=role.id,
        )
    )
    await db_session.commit()
    login = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": "T3st@user!"}
    )
    csrf = login.json()["data"]["csrf_token"]

    created = await client.post(
        "/api/v1/medications",
        headers={"X-CSRF-Token": csrf},
        json={"trade_name": "Cosmetic X", "is_medicine": False},
    )
    assert created.status_code == 200, created.text
    assert created.json()["data"]["is_medicine"] is False
    # Default stays medicine when the flag is omitted.
    default = await client.post(
        "/api/v1/medications",
        headers={"X-CSRF-Token": csrf},
        json={"trade_name": "Panadol Y"},
    )
    assert default.json()["data"]["is_medicine"] is True


# ------------------------------ tax profile settings (API) ------------------------------


async def _seed(db_session: AsyncSession, role_code: str) -> str:
    from pharmaos_api.models import Role
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


async def _login(client, username: str) -> str:  # type: ignore[no-untyped-def]
    r = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": "T3st@user!"}
    )
    assert r.status_code == 200, r.text
    return r.json()["data"]["csrf_token"]


async def test_tax_profile_read_and_update_api(  # type: ignore[no-untyped-def]
    client, db_session: AsyncSession, branch: Branch
) -> None:
    from pharmaos_api.models import TaxProfile

    # super_admin reads the branch's effective (seeded EG) profile.
    sa_csrf = await _login(client, await _seed(db_session, "super_admin"))
    got = await client.get(f"/api/v1/branches/{branch.id}/tax-profile")
    assert got.status_code == 200, got.text
    data = got.json()["data"]
    assert data["vat_rate"] == "14.00"
    assert data["medicine_vat_rate"] is None
    assert data["einvoice_system"] == "eta_ereceipt"

    # Update a THROWAWAY profile (isolated from the shared EG profile the sale
    # tests depend on) — proves the edit path + audit without cross-test drift.
    throwaway = TaxProfile(name="Test VAT", vat_rate=Decimal("14.00"))
    db_session.add(throwaway)
    await db_session.commit()
    patched = await client.patch(
        f"/api/v1/tax-profiles/{throwaway.id}",
        headers={"X-CSRF-Token": sa_csrf},
        json={
            "name": "Test VAT",
            "vat_rate": "15.00",
            "medicine_vat_rate": "5.00",
            "einvoice_system": "eta_ereceipt",
        },
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["data"]["vat_rate"] == "15.00"
    assert patched.json()["data"]["medicine_vat_rate"] == "5.00"

    # CSRF is mandatory on the mutation.
    no_csrf = await client.patch(
        f"/api/v1/tax-profiles/{throwaway.id}", json={"name": "x", "vat_rate": "10"}
    )
    assert no_csrf.status_code == 403 and no_csrf.json()["error"]["code"] == "E-AUTH-004"

    # branch_manager may VIEW but not EDIT; cashier may not view at all.
    bm_csrf = await _login(client, await _seed(db_session, "branch_manager"))
    assert (await client.get(f"/api/v1/branches/{branch.id}/tax-profile")).status_code == 200
    bm_edit = await client.patch(
        f"/api/v1/tax-profiles/{throwaway.id}",
        headers={"X-CSRF-Token": bm_csrf},
        json={"name": "x", "vat_rate": "10"},
    )
    assert bm_edit.status_code == 403 and bm_edit.json()["error"]["code"] == "E-AUTH-002"

    await _login(client, await _seed(db_session, "cashier"))
    assert (await client.get(f"/api/v1/branches/{branch.id}/tax-profile")).status_code == 403
