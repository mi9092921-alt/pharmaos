"""Prescriptions + the controlled-substance register (P2-M8).

A requires_prescription medication must link a prescription item with enough
remaining quantity (else E-RX-001/002/003); dispensing increments the item and
recomputes the prescription's status. A controlled_substance medication ALWAYS
writes an append-only controlled_substance_log row per batch slice, regardless
of prescription linkage — and the register is immutable at the DB level.
"""

import datetime as dt
import uuid
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
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
from pharmaos_api.services import inventory_service, prescription_service, sales_service
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
    db_session: AsyncSession,
    *,
    requires_prescription: bool = False,
    controlled_substance: bool = False,
    price: str = "50.00",
) -> tuple[str, str, str]:
    """A single-level (box) sellable med. Returns (medication_id, packaging_id, barcode)."""
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
        trade_name=f"Rx {uuid.uuid4().hex[:6]}",
        trade_name_ar="صنف",
        requires_prescription=requires_prescription,
        controlled_substance=controlled_substance,
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
    barcode = f"628{uuid.uuid4().int % 10**10:010d}"
    db_session.add(MedicationBarcode(medication_id=med.id, packaging_id=box.id, barcode=barcode))
    await db_session.commit()
    return str(med.id), str(box.id), barcode


async def _receive(db_session, actor, branch, med_id, *, qty):  # type: ignore[no-untyped-def]
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


async def _make_prescription(
    db_session, actor, branch, med_id, packaging_id, *, qty  # type: ignore[no-untyped-def]
):
    return await prescription_service.create_prescription(
        db_session,
        actor=actor,
        branch_id=branch.id,
        customer_id=None,
        doctor_name="د. أحمد سمير",
        doctor_license_no="LIC-123",
        prescription_date=dt.date.today(),
        notes="حساسية من البنسلين",
        items=[
            prescription_service.NewPrescriptionItem(
                uuid.UUID(med_id), uuid.UUID(packaging_id), Decimal(qty)
            )
        ],
    )


async def _first_item_id(db_session: AsyncSession, prescription_id: uuid.UUID) -> uuid.UUID:
    from pharmaos_api.models import PrescriptionItem

    item_id = (
        await db_session.execute(
            select(PrescriptionItem.id).where(PrescriptionItem.prescription_id == prescription_id)
        )
    ).scalar_one()
    return item_id  # type: ignore[no-any-return]


# ------------------------------ prescription CRUD ------------------------------


async def test_create_and_encrypt_notes(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_id, pkg_id, _bc = await _make_med(db_session, requires_prescription=True)
    prescription = await _make_prescription(db_session, actor, branch, med_id, pkg_id, qty="20")

    raw = (
        await db_session.execute(
            text("SELECT notes_encrypted FROM prescriptions WHERE id = :i").bindparams(
                i=prescription.id
            )
        )
    ).scalar_one()
    assert b"\xd8\xa8\xd9\x86\xd8\xb3\xd9\x84\xd9\x8a\xd9\x86" not in bytes(raw)  # "بنسلين" utf8

    out = await prescription_service.get_prescription_out(db_session, prescription.id)
    assert out["notes"] == "حساسية من البنسلين"
    assert out["status"] == "pending"
    assert len(out["items"]) == 1  # type: ignore[arg-type]
    item = out["items"][0]  # type: ignore[index]
    assert item["prescribed_qty"] == "20.000" and item["remaining_qty_smallest"] == "20.000"


async def test_update_header_and_cancel(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_id, pkg_id, _bc = await _make_med(db_session, requires_prescription=True)
    prescription = await _make_prescription(db_session, actor, branch, med_id, pkg_id, qty="10")

    await prescription_service.update_prescription(
        db_session, actor=actor, prescription=prescription, updates={"doctor_name": "د. سارة"}
    )
    assert prescription.doctor_name == "د. سارة"

    await prescription_service.update_prescription(
        db_session, actor=actor, prescription=prescription, updates={"status": "cancelled"}
    )
    assert prescription.status == "cancelled"


# ------------------------------ sale-flow enforcement ------------------------------


async def test_missing_prescription_blocks_sale(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_id, _pkg, barcode = await _make_med(db_session, requires_prescription=True)
    await _receive(db_session, actor, branch, med_id, qty="10")

    with pytest.raises(ApiError) as exc:
        await sales_service.create_sale(
            db_session,
            branch_id=branch.id,
            lines=[SaleLine(quantity=Decimal(1), barcode=barcode)],
            cashier=actor,
        )
    assert exc.value.code == ErrorCode.PRESCRIPTION_REQUIRED


async def test_prescription_dispense_decrements_and_recomputes_status(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_id, pkg_id, barcode = await _make_med(db_session, requires_prescription=True, price="40.00")
    await _receive(db_session, actor, branch, med_id, qty="100")
    prescription = await _make_prescription(db_session, actor, branch, med_id, pkg_id, qty="5")
    item_id = await _first_item_id(db_session, prescription.id)

    # Partial dispense: 2 of 5 -> partially_fulfilled.
    await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(quantity=Decimal(2), barcode=barcode, prescription_item_id=item_id)],
        cashier=actor,
    )
    await db_session.refresh(prescription)
    assert prescription.status == "partially_fulfilled"
    view = await prescription_service.get_prescription_out(db_session, prescription.id)
    item = view["items"][0]  # type: ignore[index]
    assert item["dispensed_qty_smallest"] == "2.000"
    assert item["remaining_qty_smallest"] == "3.000"

    # Finish it off: 3 more -> fulfilled.
    await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(quantity=Decimal(3), barcode=barcode, prescription_item_id=item_id)],
        cashier=actor,
    )
    await db_session.refresh(prescription)
    assert prescription.status == "fulfilled"


async def test_prescription_exceeded_rejected(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_id, pkg_id, barcode = await _make_med(db_session, requires_prescription=True)
    await _receive(db_session, actor, branch, med_id, qty="100")
    prescription = await _make_prescription(db_session, actor, branch, med_id, pkg_id, qty="3")
    item_id = await _first_item_id(db_session, prescription.id)

    with pytest.raises(ApiError) as exc:
        await sales_service.create_sale(
            db_session,
            branch_id=branch.id,
            lines=[SaleLine(quantity=Decimal(4), barcode=barcode, prescription_item_id=item_id)],
            cashier=actor,
        )
    assert exc.value.code == ErrorCode.PRESCRIPTION_EXCEEDED

    # Nothing persisted (atomic rollback) — the item's dispensed total is untouched.
    await db_session.rollback()
    await db_session.refresh(prescription)
    view = await prescription_service.get_prescription_out(db_session, prescription.id)
    assert view["items"][0]["dispensed_qty_smallest"] == "0.000"  # type: ignore[index]


async def test_prescription_medication_mismatch_rejected(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_id, pkg_id, _bc = await _make_med(db_session, requires_prescription=True)
    other_med_id, _other_pkg, other_barcode = await _make_med(
        db_session, requires_prescription=True
    )
    await _receive(db_session, actor, branch, other_med_id, qty="10")
    prescription = await _make_prescription(db_session, actor, branch, med_id, pkg_id, qty="5")
    item_id = await _first_item_id(db_session, prescription.id)

    # item_id belongs to `med_id`, but the sale line is for `other_med_id`.
    with pytest.raises(ApiError) as exc:
        await sales_service.create_sale(
            db_session,
            branch_id=branch.id,
            lines=[
                SaleLine(quantity=Decimal(1), barcode=other_barcode, prescription_item_id=item_id)
            ],
            cashier=actor,
        )
    assert exc.value.code == ErrorCode.PRESCRIPTION_INVALID


async def test_cancelled_prescription_blocks_dispense(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_id, pkg_id, barcode = await _make_med(db_session, requires_prescription=True)
    await _receive(db_session, actor, branch, med_id, qty="10")
    prescription = await _make_prescription(db_session, actor, branch, med_id, pkg_id, qty="5")
    item_id = await _first_item_id(db_session, prescription.id)

    await prescription_service.update_prescription(
        db_session, actor=actor, prescription=prescription, updates={"status": "cancelled"}
    )

    with pytest.raises(ApiError) as exc:
        await sales_service.create_sale(
            db_session,
            branch_id=branch.id,
            lines=[SaleLine(quantity=Decimal(1), barcode=barcode, prescription_item_id=item_id)],
            cashier=actor,
        )
    assert exc.value.code == ErrorCode.PRESCRIPTION_INVALID


# ------------------------------ controlled-substance register ------------------------------


async def test_controlled_substance_auto_logs_and_audits(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_id, _pkg, barcode = await _make_med(db_session, controlled_substance=True, price="60.00")
    await _receive(db_session, actor, branch, med_id, qty="10")

    invoice = await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(quantity=Decimal(2), barcode=barcode)],
        cashier=actor,
    )

    rows = (
        await db_session.execute(
            text(
                "SELECT quantity_dispensed, dispensed_by, invoice_id, prescription_id "
                "FROM controlled_substance_log WHERE invoice_id = :i"
            ).bindparams(i=invoice.id)
        )
    ).all()
    assert len(rows) == 1
    assert Decimal(rows[0][0]) == Decimal("2") and rows[0][1] == actor.id
    assert rows[0][3] is None  # not prescription-linked in this test

    # Scoped to THIS test's branch (a fresh one per test) — an unscoped COUNT
    # would accumulate rows committed by other controlled-substance tests
    # against the shared persistent test database.
    audited = (
        await db_session.execute(
            text(
                "SELECT COUNT(*) FROM audit_logs "
                "WHERE action = 'controlled_substance.dispensed' AND branch_id = :b"
            ).bindparams(b=branch.id)
        )
    ).scalar_one()
    assert audited == 1


async def test_controlled_substance_multi_batch_split_sums_correctly(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_id, _pkg, barcode = await _make_med(db_session, controlled_substance=True, price="10.00")
    # Two separate batches of 1 each -> a 2-unit sale splits across both (FEFO).
    await _receive(db_session, actor, branch, med_id, qty="1")
    await _receive(db_session, actor, branch, med_id, qty="1")

    invoice = await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(quantity=Decimal(2), barcode=barcode)],
        cashier=actor,
    )
    rows = (
        await db_session.execute(
            text(
                "SELECT quantity_dispensed, batch_id FROM controlled_substance_log "
                "WHERE invoice_id = :i"
            ).bindparams(i=invoice.id)
        )
    ).all()
    assert len(rows) == 2  # one row per batch slice
    assert len({r[1] for r in rows}) == 2  # two DISTINCT batches
    assert sum(Decimal(r[0]) for r in rows) == Decimal("2")


async def test_controlled_substance_with_prescription_links_both(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_id, pkg_id, barcode = await _make_med(
        db_session, requires_prescription=True, controlled_substance=True, price="90.00"
    )
    await _receive(db_session, actor, branch, med_id, qty="10")
    prescription = await _make_prescription(db_session, actor, branch, med_id, pkg_id, qty="5")
    item_id = await _first_item_id(db_session, prescription.id)

    invoice = await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(quantity=Decimal(1), barcode=barcode, prescription_item_id=item_id)],
        cashier=actor,
    )
    row = (
        await db_session.execute(
            text(
                "SELECT prescription_id FROM controlled_substance_log WHERE invoice_id = :i"
            ).bindparams(i=invoice.id)
        )
    ).scalar_one()
    assert row == prescription.id


async def test_controlled_substance_log_is_immutable(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    """The DB trigger blocks UPDATE and DELETE for every role — the strongest,
    role-independent enforcement of CLAUDE.md's 'never truly delete'."""
    med_id, _pkg, barcode = await _make_med(db_session, controlled_substance=True)
    await _receive(db_session, actor, branch, med_id, qty="10")
    invoice = await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(quantity=Decimal(1), barcode=barcode)],
        cashier=actor,
    )
    log_id = (
        await db_session.execute(
            text("SELECT id FROM controlled_substance_log WHERE invoice_id = :i").bindparams(
                i=invoice.id
            )
        )
    ).scalar_one()

    with pytest.raises(DBAPIError, match="append-only"):
        await db_session.execute(
            text(
                "UPDATE controlled_substance_log SET quantity_dispensed = 99 WHERE id = :i"
            ).bindparams(i=log_id)
        )
    await db_session.rollback()

    with pytest.raises(DBAPIError, match="append-only"):
        await db_session.execute(
            text("DELETE FROM controlled_substance_log WHERE id = :i").bindparams(i=log_id)
        )
    await db_session.rollback()


# ------------------------------ API layer ------------------------------


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


async def test_prescription_api_permissions_and_csrf(
    client: httpx.AsyncClient, db_session: AsyncSession, branch: Branch
) -> None:
    med_id, pkg_id, _bc = await _make_med(db_session, requires_prescription=True)
    body = {
        "branch_id": str(branch.id),
        "doctor_name": "د. منى",
        "prescription_date": dt.date.today().isoformat(),
        "items": [{"medication_id": med_id, "packaging_id": pkg_id, "quantity": "10"}],
    }

    # cashier lacks prescriptions.create/view entirely.
    cashier_csrf = await _login(client, await _seed_user(db_session, "cashier"))
    forbidden = await client.post(
        "/api/v1/prescriptions", headers={"X-CSRF-Token": cashier_csrf}, json=body
    )
    assert forbidden.status_code == 403 and forbidden.json()["error"]["code"] == "E-AUTH-002"
    view_forbidden = await client.get("/api/v1/prescriptions", params={"branch_id": str(branch.id)})
    assert view_forbidden.status_code == 403

    # pharmacist holds prescriptions.create/view; CSRF is mandatory.
    ph_csrf = await _login(client, await _seed_user(db_session, "pharmacist"))
    no_csrf = await client.post("/api/v1/prescriptions", json=body)
    assert no_csrf.status_code == 403 and no_csrf.json()["error"]["code"] == "E-AUTH-004"

    created = await client.post(
        "/api/v1/prescriptions", headers={"X-CSRF-Token": ph_csrf}, json=body
    )
    assert created.status_code == 200, created.text
    data = created.json()["data"]
    assert data["doctor_name"] == "د. منى" and len(data["items"]) == 1

    listed = await client.get("/api/v1/prescriptions", params={"branch_id": str(branch.id)})
    assert listed.status_code == 200
    assert any(p["id"] == data["id"] for p in listed.json()["data"])


async def test_controlled_substance_log_api_view_only(
    client: httpx.AsyncClient, db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_id, _pkg, barcode = await _make_med(db_session, controlled_substance=True)
    await _receive(db_session, actor, branch, med_id, qty="10")
    await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(quantity=Decimal(1), barcode=barcode)],
        cashier=actor,
    )

    # data_entry lacks controlled_substances.view.
    await _login(client, await _seed_user(db_session, "data_entry"))
    denied = await client.get(
        "/api/v1/controlled-substances/log", params={"branch_id": str(branch.id)}
    )
    assert denied.status_code == 403

    await _login(client, await _seed_user(db_session, "branch_manager"))
    ok = await client.get("/api/v1/controlled-substances/log", params={"branch_id": str(branch.id)})
    assert ok.status_code == 200, ok.text
    rows = ok.json()["data"]
    assert len(rows) == 1 and rows[0]["quantity_dispensed"] == "1.000"
    # Human-facing references (a compliance register of raw UUIDs is useless
    # to the pharmacist reviewing it) — joined in from Invoice/User/Batch.
    assert rows[0]["invoice_number"] and rows[0]["invoice_number"] != rows[0]["invoice_id"]
    assert rows[0]["batch_number"]
    assert rows[0]["dispensed_by_name"] == actor.full_name

    # No route exposes create/update/delete for the register — it is
    # exclusively a side effect of the sale flow (verified structurally: only
    # a GET is registered under /controlled-substances).
