"""Hardening (P1-M11): expiry sweep (status + derived cache + ledger), the
receipt's tendered/change rows, clean 422/404 on unknown ids in receiving, and
the production printer-host lock."""

import datetime as dt
import uuid
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.models import (
    Branch,
    Medication,
    MedicationBarcode,
    MedicationPackaging,
    Role,
    User,
)
from pharmaos_api.printing.escpos import ReceiptData, ReceiptLine, build_receipt
from pharmaos_api.services import inventory_service, receipt_service, sales_service
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


async def _make_med(db_session: AsyncSession) -> tuple[str, str]:
    """Strip-barcode med at 30.00/strip (10 tablets). Returns (med_id, barcode)."""
    unit_id = (
        await db_session.execute(
            text(
                "INSERT INTO units (name_ar) VALUES ('شريط') "
                "ON CONFLICT (name_ar) DO UPDATE SET name_ar=EXCLUDED.name_ar RETURNING id"
            )
        )
    ).scalar_one()
    await db_session.commit()
    med = Medication(trade_name=f"HrdMed {uuid.uuid4().hex[:6]}", trade_name_ar="دواء التقسية")
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
    return str(med.id), barcode


# ------------------------------ expiry sweep ------------------------------


async def test_expiry_sweep_marks_batches_and_keeps_cache_consistent(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    bid = branch.id
    med_id, _ = await _make_med(db_session)

    # Canonical receiving keeps the cache consistent: 100 fresh + 30 soon-to-expire.
    await inventory_service.receive_stock(
        db_session,
        actor=actor,
        branch_id=bid,
        medication_id=uuid.UUID(med_id),
        batch_number=f"FRESH-{uuid.uuid4().hex[:6]}",
        expiry_date=dt.date.today() + dt.timedelta(days=365),
        quantity=Decimal(100),
        purchase_price=Decimal("2.00"),
    )
    doomed = await inventory_service.receive_stock(
        db_session,
        actor=actor,
        branch_id=bid,
        medication_id=uuid.UUID(med_id),
        batch_number=f"DOOM-{uuid.uuid4().hex[:6]}",
        expiry_date=dt.date.today() + dt.timedelta(days=30),
        quantity=Decimal(30),
        purchase_price=Decimal("2.00"),
    )
    # Time passes: the batch is now past expiry (bypass receive's guard).
    await db_session.execute(
        text("UPDATE medication_batches SET expiry_date = :d WHERE id = :i").bindparams(
            d=dt.date.today() - dt.timedelta(days=1), i=doomed.id
        )
    )
    await db_session.commit()

    result = await inventory_service.expiry_sweep(db_session)
    assert result["swept"] >= 1

    await db_session.refresh(doomed)
    assert doomed.status == "expired"
    # Sellable cache dropped to the fresh batch only; invariant holds.
    cached = (
        await db_session.execute(
            text(
                "SELECT cached_quantity FROM branch_inventory "
                "WHERE branch_id = :b AND medication_id = CAST(:m AS uuid)"
            ).bindparams(b=bid, m=med_id)
        )
    ).scalar_one()
    assert Decimal(cached) == Decimal(100)
    assert await inventory_service.drift_check(db_session, bid) == []
    # Ledger row written with the schema's expiry_writeoff type.
    moves = (
        await db_session.execute(
            text(
                "SELECT movement_type, reason FROM stock_movements "
                "WHERE batch_id = :i AND movement_type = 'expiry_writeoff'"
            ).bindparams(i=doomed.id)
        )
    ).all()
    assert len(moves) == 1 and moves[0][1] == "expiry_sweep"

    # Idempotent: nothing left to sweep for this batch.
    again = await inventory_service.expiry_sweep(db_session)
    swept_ids = (
        await db_session.execute(
            text(
                "SELECT COUNT(*) FROM stock_movements "
                "WHERE batch_id = :i AND movement_type = 'expiry_writeoff'"
            ).bindparams(i=doomed.id)
        )
    ).scalar_one()
    assert swept_ids == 1 and again["swept"] >= 0


async def test_boot_maintenance_includes_sweep(db_session: AsyncSession, branch: Branch) -> None:
    summary = await inventory_service.boot_check_and_heal(db_session)
    assert "_expiry_sweep" in summary
    assert str(branch.id) in summary


# --------------------------- receipt tendered/change ---------------------------


def test_builder_renders_tendered_and_change_rows() -> None:
    base: dict[str, object] = {
        "pharmacy_name": "صيدلية",
        "branch_name": "فرع",
        "invoice_number": "INV-1",
        "created_at_display": "2026-07-11 22:50",
        "lines": [
            ReceiptLine(
                name="دواء", quantity=Decimal(1), unit_name="شريط", line_total=Decimal("30.00")
            )
        ],
        "subtotal": Decimal("30.00"),
        "discount": Decimal("0.00"),
        "total": Decimal("30.00"),
        "currency_symbol": "ج.م",
        "thank_you_message": "شكراً",
    }
    plain = build_receipt(ReceiptData(**base))  # type: ignore[arg-type]
    with_cash = build_receipt(
        ReceiptData(**base, tendered=Decimal("50.00"), change_due=Decimal("20.00"))  # type: ignore[arg-type]
    )
    assert "المدفوع".encode() not in plain and "الباقي".encode() not in plain
    assert "المدفوع".encode() in with_cash and "الباقي".encode() in with_cash
    assert b"50.00" in with_cash and b"20.00" in with_cash


async def test_receipt_carries_invoice_cash_math(
    client: httpx.AsyncClient, db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_id, barcode = await _make_med(db_session)
    await inventory_service.receive_stock(
        db_session,
        actor=actor,
        branch_id=branch.id,
        medication_id=uuid.UUID(med_id),
        batch_number=f"RCV-{uuid.uuid4().hex[:6]}",
        expiry_date=dt.date.today() + dt.timedelta(days=365),
        quantity=Decimal(100),
        purchase_price=Decimal("2.00"),
    )
    invoice = await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(quantity=Decimal(2), barcode=barcode)],
        cashier=actor,
        tendered=Decimal("100.00"),
    )
    receipt = await receipt_service.load_invoice_receipt(db_session, invoice.id)
    assert receipt.tendered == Decimal("100.00") and receipt.change_due == Decimal("40.00")
    payload = receipt_service.to_escpos(receipt, open_drawer=False)
    assert "المدفوع".encode() in payload and "الباقي".encode() in payload

    # And through the JSON endpoint (browser printing).
    await _login(client, await _seed_role_user(db_session, "cashier"))
    r = await client.get(f"/api/v1/pos/invoices/{invoice.id}/receipt")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["tendered_amount"] == "100.00" and data["change_amount"] == "40.00"


# ------------------------------ clean 422/404 ------------------------------


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


async def test_receive_unknown_ids_return_clean_errors(
    client: httpx.AsyncClient, db_session: AsyncSession, branch: Branch
) -> None:
    csrf = await _login(client, await _seed_role_user(db_session, "branch_manager"))
    med_id, _ = await _make_med(db_session)
    future = (dt.date.today() + dt.timedelta(days=200)).isoformat()

    def body(**overrides: str) -> dict[str, str]:
        base = {
            "branch_id": str(branch.id),
            "medication_id": med_id,
            "batch_number": "X-1",
            "expiry_date": future,
            "quantity": "5",
            "purchase_price": "1.00",
        }
        base.update(overrides)
        return base

    bad_branch = await client.post(
        "/api/v1/inventory/receive",
        headers={"X-CSRF-Token": csrf},
        json=body(branch_id=str(uuid.uuid4())),
    )
    assert bad_branch.status_code == 422  # not a 500 FK blowup
    assert bad_branch.json()["error"]["code"] == "E-VAL-001"

    bad_med = await client.post(
        "/api/v1/inventory/receive",
        headers={"X-CSRF-Token": csrf},
        json=body(medication_id=str(uuid.uuid4())),
    )
    assert bad_med.status_code == 404
    assert bad_med.json()["error"]["code"] == "E-VAL-001"

    bad_supplier = await client.post(
        "/api/v1/inventory/receive",
        headers={"X-CSRF-Token": csrf},
        json=body(supplier_id=str(uuid.uuid4())),
    )
    assert bad_supplier.status_code == 422
    assert bad_supplier.json()["error"]["code"] == "E-VAL-001"


# --------------------------- printer host lock (prod) ---------------------------


async def test_printer_host_locked_on_configured_production_device(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    actor: User,
    branch: Branch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    med_id, barcode = await _make_med(db_session)
    await inventory_service.receive_stock(
        db_session,
        actor=actor,
        branch_id=branch.id,
        medication_id=uuid.UUID(med_id),
        batch_number=f"RCV-{uuid.uuid4().hex[:6]}",
        expiry_date=dt.date.today() + dt.timedelta(days=365),
        quantity=Decimal(50),
        purchase_price=Decimal("2.00"),
    )
    invoice = await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(quantity=Decimal(1), barcode=barcode)],
        cashier=actor,
    )
    csrf = await _login(client, await _seed_role_user(db_session, "cashier"))

    # Fake a CONFIGURED PRODUCTION device for the POS router ONLY — patching the
    # shared settings object would flip the keystore into production mode and
    # break JWT verification for the whole request.
    import types

    fake_cfg = types.SimpleNamespace(
        is_production=True,
        printer_host="10.77.0.9",
        printer_port=9100,
        printer_timeout_seconds=1.0,
    )
    monkeypatch.setattr("pharmaos_api.routers.pos.get_app_settings", lambda: fake_cfg)

    # A DIFFERENT host is refused outright (no connection attempt).
    other = await client.post(
        f"/api/v1/pos/invoices/{invoice.id}/print",
        headers={"X-CSRF-Token": csrf},
        json={"printer_host": "10.77.0.99", "printer_port": 9100},
    )
    assert other.status_code == 422
    assert other.json()["error"]["code"] == "E-VAL-001"

    # The CONFIGURED host passes the lock: the send is actually attempted and
    # fails only because nothing listens there in the test environment.
    fake_cfg.printer_host = "127.0.0.1"
    attempted = await client.post(
        f"/api/v1/pos/invoices/{invoice.id}/print",
        headers={"X-CSRF-Token": csrf},
        json={"printer_host": "127.0.0.1", "printer_port": 1},
    )
    assert attempted.status_code == 503  # lock passed; the dead port answered
    assert attempted.json()["error"]["code"] == "E-PRN-002"
