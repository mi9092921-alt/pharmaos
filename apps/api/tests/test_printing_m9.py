"""Receipt printing (P1-M9): ESC/POS builder extensions (QR / signature /
header / drawer control), receipt composition from a real invoice (FEFO slice
re-aggregation + branch-settings merge), and the print endpoints — including a
true end-to-end run against a fake network printer (local TCP server)."""

import asyncio
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
    Settings,
    User,
)
from pharmaos_api.printing.escpos import (
    DRAWER_PULSE,
    INIT,
    QR_PREFIX,
    ReceiptData,
    ReceiptLine,
    build_receipt,
    qr_code,
)
from pharmaos_api.services import receipt_service, sales_service
from pharmaos_api.services.sales_service import SaleLine

# ------------------------------ builder units ------------------------------


def _minimal_receipt(**overrides: object) -> ReceiptData:
    base: dict[str, object] = {
        "pharmacy_name": "صيدلية الاختبار",
        "branch_name": "الفرع الرئيسي",
        "invoice_number": "INV-20260711-0042",
        "created_at_display": "2026-07-11 22:00",
        "lines": [
            ReceiptLine(
                name="بنادول ٥٠٠",
                quantity=Decimal(2),
                unit_name="شريط",
                line_total=Decimal("60.00"),
            )
        ],
        "subtotal": Decimal("60.00"),
        "discount": Decimal("0.00"),
        "total": Decimal("60.00"),
        "currency_symbol": "ج.م",
        "thank_you_message": "شكراً لزيارتكم",
    }
    base.update(overrides)
    return ReceiptData(**base)  # type: ignore[arg-type]


def test_qr_code_bytes_structure() -> None:
    payload = qr_code("PHARMAOS|INV-1|60.00")
    assert payload.startswith(QR_PREFIX)  # model command first
    assert b"PHARMAOS|INV-1|60.00" in payload  # data stored
    assert payload.endswith(QR_PREFIX + b"\x03\x00\x31\x51\x30")  # print symbol last


def test_receipt_qr_only_when_content_present() -> None:
    without = build_receipt(_minimal_receipt())
    with_qr = build_receipt(_minimal_receipt(qr_content="PHARMAOS|INV-20260711-0042|60.00"))
    assert QR_PREFIX not in without
    assert QR_PREFIX in with_qr
    assert b"PHARMAOS|INV-20260711-0042|60.00" in with_qr


def test_receipt_signature_and_header_fields() -> None:
    payload = build_receipt(
        _minimal_receipt(
            address="١٢ شارع الجمهورية",
            phone="0100000000",
            license_number="LIC-9",
            tax_registration_no="TAX-77",
            payment_method_display="نقدي",
            show_signature=True,
        )
    )
    for needle in (
        "١٢ شارع الجمهورية",
        "هاتف: 0100000000",
        "ترخيص: LIC-9",
        "رقم التسجيل الضريبي: TAX-77",
        "طريقة الدفع: نقدي",
        "توقيع الصيدلاني",
    ):
        assert needle.encode() in payload, needle


def test_receipt_drawer_pulse_is_conditional() -> None:
    cash = build_receipt(_minimal_receipt(), open_drawer=True)
    card = build_receipt(_minimal_receipt(), open_drawer=False)
    assert cash.startswith(INIT) and cash.endswith(DRAWER_PULSE)
    assert DRAWER_PULSE not in card


# ------------------------------ fixtures ------------------------------


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


async def _make_med_with_stock(
    db_session: AsyncSession, branch_id: uuid.UUID, *, batches: list[tuple[int, int]]
) -> str:
    """Strip-barcode med (30.00/strip = 10 tablets); batches = [(tablets, days)]."""
    unit_id = (
        await db_session.execute(
            text(
                "INSERT INTO units (name_ar) VALUES ('شريط') "
                "ON CONFLICT (name_ar) DO UPDATE SET name_ar=EXCLUDED.name_ar RETURNING id"
            )
        )
    ).scalar_one()
    await db_session.commit()
    med = Medication(trade_name=f"PrnMed {uuid.uuid4().hex[:6]}", trade_name_ar="دواء الطباعة")
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
    for tablets, days in batches:
        db_session.add(
            MedicationBatch(
                branch_id=branch_id,
                medication_id=med.id,
                batch_number=f"PRN-{uuid.uuid4().hex[:8]}",
                expiry_date=dt.date.today() + dt.timedelta(days=days),
                quantity=Decimal(tablets),
                purchase_price=Decimal("2.00"),
            )
        )
    await db_session.commit()
    return barcode


def _full_settings(branch_id: uuid.UUID, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "branch_id": branch_id,
        "pharmacy_name": "صيدلية النور",
        "address": "١٢ شارع الجمهورية، أسيوط",
        "phone": "0101111111",
        "license_number": "LIC-2026-9",
        "tax_registration_no": "TAX-556-77",
        "thank_you_message": "نتمنى لكم دوام الصحة",
        "paper_size": "80mm",
        "show_qr_code": True,
        "show_pharmacist_signature": True,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


async def _sale(
    db_session: AsyncSession, branch_id: uuid.UUID, barcode: str, cashier: User, qty: int = 3
):
    return await sales_service.create_sale(
        db_session,
        branch_id=branch_id,
        lines=[SaleLine(quantity=Decimal(qty), barcode=barcode)],
        cashier=cashier,
    )


# ------------------------------ composition ------------------------------


async def test_receipt_aggregates_fefo_slices_and_merges_settings(
    db_session: AsyncSession, branch: Branch, cashier: User
) -> None:
    db_session.add(_full_settings(branch.id))
    await db_session.commit()
    # 3 strips = 30 tablets across batches of 15 + 100 -> TWO invoice item slices.
    barcode = await _make_med_with_stock(db_session, branch.id, batches=[(15, 30), (100, 365)])
    invoice = await _sale(db_session, branch.id, barcode, cashier)

    receipt = await receipt_service.load_invoice_receipt(db_session, invoice.id)
    assert len(receipt.lines) == 1  # slices re-aggregated for the customer
    line = receipt.lines[0]
    assert line.name == "دواء الطباعة" and line.unit_name == "شريط"
    assert line.quantity == Decimal("3.000")
    assert line.line_total == Decimal("90.00")

    assert receipt.pharmacy_name == "صيدلية النور"
    assert receipt.thank_you_message == "نتمنى لكم دوام الصحة"
    assert receipt.show_qr_code and receipt.show_pharmacist_signature
    assert receipt.qr_content is not None
    assert invoice.invoice_number in receipt.qr_content
    assert receipt.currency_symbol == "ج.م"
    assert receipt.payment_method_display == "نقدي"

    payload = receipt_service.to_escpos(receipt, open_drawer=True)
    assert payload.startswith(INIT) and payload.endswith(DRAWER_PULSE)
    assert "صيدلية النور".encode() in payload
    assert QR_PREFIX in payload


async def test_receipt_defaults_without_settings_row(
    db_session: AsyncSession, branch: Branch, cashier: User
) -> None:
    barcode = await _make_med_with_stock(db_session, branch.id, batches=[(100, 365)])
    invoice = await _sale(db_session, branch.id, barcode, cashier)
    receipt = await receipt_service.load_invoice_receipt(db_session, invoice.id)
    assert receipt.pharmacy_name == branch.name
    assert receipt.thank_you_message == receipt_service.DEFAULT_THANK_YOU
    assert receipt.paper_size == "80mm"
    assert receipt.qr_content is None and not receipt.show_pharmacist_signature


async def test_receipt_unknown_invoice_404(db_session: AsyncSession) -> None:
    with pytest.raises(ApiError) as exc:
        await receipt_service.load_invoice_receipt(db_session, uuid.uuid4())
    assert exc.value.http_status == 404


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


async def test_receipt_endpoint_readable_by_viewer(
    client: httpx.AsyncClient, db_session: AsyncSession, branch: Branch, cashier: User
) -> None:
    db_session.add(_full_settings(branch.id))
    await db_session.commit()
    barcode = await _make_med_with_stock(db_session, branch.id, batches=[(100, 365)])
    invoice = await _sale(db_session, branch.id, barcode, cashier)

    await _login(client, await _seed_role_user(db_session, "viewer"))  # sales.view: all roles
    r = await client.get(f"/api/v1/pos/invoices/{invoice.id}/receipt")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["invoice_number"] == invoice.invoice_number
    assert data["pharmacy_name"] == "صيدلية النور"
    assert data["show_qr_code"] is True and data["qr_content"]
    assert data["thermal_ready"] is False  # no printer configured in test env
    assert data["lines"][0]["unit_name"] == "شريط"

    missing = await client.get(f"/api/v1/pos/invoices/{uuid.uuid4()}/receipt")
    assert missing.status_code == 404


async def test_print_requires_configured_printer_and_thermal_paper(
    client: httpx.AsyncClient, db_session: AsyncSession, branch: Branch, cashier: User
) -> None:
    barcode = await _make_med_with_stock(db_session, branch.id, batches=[(100, 365)])
    invoice = await _sale(db_session, branch.id, barcode, cashier)
    csrf = await _login(client, await _seed_role_user(db_session, "cashier"))

    # No PRINTER_HOST in the test env and no override -> E-PRN-001.
    r = await client.post(
        f"/api/v1/pos/invoices/{invoice.id}/print", headers={"X-CSRF-Token": csrf}, json={}
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "E-PRN-001"

    # A4 paper -> thermal refused with E-PRN-003 (browser printing instead).
    db_session.add(_full_settings(branch.id, paper_size="A4"))
    await db_session.commit()
    r2 = await client.post(
        f"/api/v1/pos/invoices/{invoice.id}/print",
        headers={"X-CSRF-Token": csrf},
        json={"printer_host": "127.0.0.1"},
    )
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "E-PRN-003"


async def test_print_endpoint_sends_escpos_to_network_printer(
    client: httpx.AsyncClient, db_session: AsyncSession, branch: Branch, cashier: User
) -> None:
    """End-to-end: POST print -> raw ESC/POS bytes arrive at a (fake) printer."""
    db_session.add(_full_settings(branch.id))
    await db_session.commit()
    barcode = await _make_med_with_stock(db_session, branch.id, batches=[(100, 365)])
    invoice = await _sale(db_session, branch.id, barcode, cashier)
    csrf = await _login(client, await _seed_role_user(db_session, "cashier"))

    received = bytearray()
    got_payload = asyncio.Event()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        received.extend(await reader.read(-1))
        got_payload.set()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        r = await client.post(
            f"/api/v1/pos/invoices/{invoice.id}/print",
            headers={"X-CSRF-Token": csrf},
            json={"printer_host": "127.0.0.1", "printer_port": port},
        )
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["printed"] is True and data["drawer"] is True  # cash sale
        await asyncio.wait_for(got_payload.wait(), timeout=5)
    finally:
        server.close()
        await server.wait_closed()

    assert bytes(received).startswith(INIT)
    assert invoice.invoice_number.encode() in received
    assert "صيدلية النور".encode() in received
    assert QR_PREFIX in received  # settings.show_qr_code
    assert bytes(received).endswith(DRAWER_PULSE)  # drawer opens for cash
    assert data["bytes"] == len(received)

    # Reprint with the drawer explicitly closed -> no pulse in the stream.
    received.clear()
    got_payload.clear()
    server2 = await asyncio.start_server(handle, "127.0.0.1", 0)
    port2 = server2.sockets[0].getsockname()[1]
    try:
        r2 = await client.post(
            f"/api/v1/pos/invoices/{invoice.id}/print",
            headers={"X-CSRF-Token": csrf},
            json={"printer_host": "127.0.0.1", "printer_port": port2, "open_drawer": False},
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["data"]["drawer"] is False
        await asyncio.wait_for(got_payload.wait(), timeout=5)
    finally:
        server2.close()
        await server2.wait_closed()
    assert DRAWER_PULSE not in received


async def test_print_unreachable_printer_returns_503(
    client: httpx.AsyncClient, db_session: AsyncSession, branch: Branch, cashier: User
) -> None:
    barcode = await _make_med_with_stock(db_session, branch.id, batches=[(100, 365)])
    invoice = await _sale(db_session, branch.id, barcode, cashier)
    csrf = await _login(client, await _seed_role_user(db_session, "cashier"))

    # A closed port on localhost -> immediate connection refusal -> E-PRN-002.
    probe = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    dead_port = probe.sockets[0].getsockname()[1]
    probe.close()
    await probe.wait_closed()

    r = await client.post(
        f"/api/v1/pos/invoices/{invoice.id}/print",
        headers={"X-CSRF-Token": csrf},
        json={"printer_host": "127.0.0.1", "printer_port": dead_port},
    )
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "E-PRN-002"


async def test_print_permissions_and_csrf(
    client: httpx.AsyncClient, db_session: AsyncSession, branch: Branch, cashier: User
) -> None:
    barcode = await _make_med_with_stock(db_session, branch.id, batches=[(100, 365)])
    invoice = await _sale(db_session, branch.id, barcode, cashier)

    await _login(client, await _seed_role_user(db_session, "cashier"))
    no_csrf = await client.post(f"/api/v1/pos/invoices/{invoice.id}/print", json={})
    assert no_csrf.status_code == 403  # logged in, but the CSRF header is missing
    assert no_csrf.json()["error"]["code"] == "E-AUTH-004"

    viewer_csrf = await _login(client, await _seed_role_user(db_session, "viewer"))
    r = await client.post(
        f"/api/v1/pos/invoices/{invoice.id}/print",
        headers={"X-CSRF-Token": viewer_csrf},
        json={},
    )
    assert r.status_code == 403  # sales.create excludes viewer
    assert r.json()["error"]["code"] == "E-AUTH-002"
