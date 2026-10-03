"""P2-M10 — ETA e-receipt outbox (adapter + local simulator).

A completed sale in an ETA-e-receipt branch enqueues a 'pending' e-receipt
inside the sale transaction (never blocks on network). The drain worker builds,
signs, and submits it via the local simulator, recording the UUID + QR and
auditing ereceipt.submitted. An offline backlog drains fully with no loss.
No real ETA acceptance is claimed — the adapter is the simulator (pending creds).
"""

import datetime as dt
import uuid
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.models import (
    Branch,
    EReceiptQueue,
    Medication,
    MedicationBarcode,
    MedicationPackaging,
    Role,
    User,
)
from pharmaos_api.security.passwords import hash_password
from pharmaos_api.services import inventory_service, sales_service
from pharmaos_api.services.compliance import ereceipt_service, eta_adapter
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


async def _make_med(db_session: AsyncSession, *, price: str = "50.00") -> tuple[str, str]:
    unit_id = (
        await db_session.execute(
            text(
                "INSERT INTO units (name_ar) VALUES ('علبة') "
                "ON CONFLICT (name_ar) DO UPDATE SET name_ar=EXCLUDED.name_ar RETURNING id"
            )
        )
    ).scalar_one()
    await db_session.commit()
    med = Medication(trade_name=f"E {uuid.uuid4().hex[:6]}", trade_name_ar="صنف", is_medicine=False)
    db_session.add(med)
    await db_session.flush()
    box = MedicationPackaging(
        medication_id=med.id,
        level=1,
        unit_id=unit_id,
        name_ar="علبة",
        selling_price=Decimal(price),
        is_default_sale=True,
    )
    db_session.add(box)
    await db_session.flush()
    bc = f"626{uuid.uuid4().int % 10**10:010d}"
    db_session.add(MedicationBarcode(medication_id=med.id, packaging_id=box.id, barcode=bc))
    await db_session.commit()
    return str(med.id), bc


async def _receive(db_session, actor, branch, med_id, *, qty="100"):  # type: ignore[no-untyped-def]
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


async def _sell(db_session, actor, branch, bc, qty="1"):  # type: ignore[no-untyped-def]
    return await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(quantity=Decimal(qty), barcode=bc)],
        cashier=actor,
    )


async def _queue_row(db_session: AsyncSession, invoice_id: uuid.UUID) -> EReceiptQueue | None:
    return (
        await db_session.execute(
            select(EReceiptQueue).where(EReceiptQueue.invoice_id == invoice_id)
        )
    ).scalar_one_or_none()


async def test_sale_enqueues_pending_ereceipt(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_id, bc = await _make_med(db_session)
    await _receive(db_session, actor, branch, med_id)
    invoice = await _sell(db_session, actor, branch, bc)

    row = await _queue_row(db_session, invoice.id)
    assert row is not None and row.status == "pending"
    assert row.eta_uuid is None and row.submission_attempts == 0


async def test_drain_builds_signs_submits_and_accepts(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_id, bc = await _make_med(db_session, price="114.00")  # non-medicine -> 14% VAT
    await _receive(db_session, actor, branch, med_id)
    invoice = await _sell(db_session, actor, branch, bc)

    result = await ereceipt_service.drain(db_session, branch_id=branch.id, actor=actor)
    assert result["processed"] >= 1 and result["accepted"] >= 1

    row = await _queue_row(db_session, invoice.id)
    assert row is not None
    assert row.status == "accepted"
    assert row.eta_uuid and row.qr_data and row.qr_data.endswith(row.eta_uuid)
    assert row.signed_payload and row.signed_payload.startswith("SIM-SEAL:")
    assert row.submission_attempts == 1
    # Payload carries the VAT snapshot from the invoice.
    assert row.payload is not None and row.payload["totals"]["tax"] == "14.00"

    audited = (
        await db_session.execute(
            text(
                "SELECT COUNT(*) FROM audit_logs WHERE action = 'ereceipt.submitted' "
                "AND entity_id = :i"
            ).bindparams(i=row.id)
        )
    ).scalar_one()
    assert audited == 1
    # The default adapter is the simulator (no real ETA acceptance claimed).
    assert eta_adapter.adapter_is_simulated() is True


async def test_offline_backlog_drains_fully(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    """24h-offline acceptance criterion: the queue accumulates, then drains with
    no loss when connectivity returns."""
    med_id, bc = await _make_med(db_session)
    await _receive(db_session, actor, branch, med_id)
    invoices = [await _sell(db_session, actor, branch, bc) for _ in range(3)]

    pending = (
        await db_session.execute(
            text(
                "SELECT COUNT(*) FROM ereceipt_queue WHERE branch_id = :b AND status = 'pending'"
            ).bindparams(b=branch.id)
        )
    ).scalar_one()
    assert pending == 3

    result = await ereceipt_service.drain(db_session, branch_id=branch.id, actor=actor)
    assert result["processed"] == 3 and result["accepted"] == 3

    for inv in invoices:
        row = await _queue_row(db_session, inv.id)
        assert row is not None and row.status == "accepted"


async def test_no_enqueue_when_branch_not_on_eta(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    """enqueue is a no-op when the branch's tax profile is not the ETA system."""
    med_id, _bc = await _make_med(db_session)
    await _receive(db_session, actor, branch, med_id)
    invoice = await _sell(db_session, actor, branch, _bc)
    # Directly exercise the guard: a None/other system must not enqueue.
    await ereceipt_service.enqueue_for_invoice(db_session, invoice=invoice, einvoice_system=None)
    await ereceipt_service.enqueue_for_invoice(db_session, invoice=invoice, einvoice_system="zatca")
    await db_session.flush()
    rows = (
        (
            await db_session.execute(
                select(EReceiptQueue).where(EReceiptQueue.invoice_id == invoice.id)
            )
        )
        .scalars()
        .all()
    )
    # Only the ONE row the ETA-branch sale already enqueued exists (no extras).
    assert len(rows) == 1


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


async def test_ereceipt_api_permissions_and_csrf(
    client: httpx.AsyncClient, db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    med_id, bc = await _make_med(db_session)
    await _receive(db_session, actor, branch, med_id)
    await _sell(db_session, actor, branch, bc)

    # cashier lacks compliance.ereceipt.
    await _login(client, await _seed_user(db_session, "cashier"))
    denied = await client.get("/api/v1/compliance/ereceipts", params={"branch_id": str(branch.id)})
    assert denied.status_code == 403

    bm_csrf = await _login(client, await _seed_user(db_session, "branch_manager"))
    listed = await client.get("/api/v1/compliance/ereceipts", params={"branch_id": str(branch.id)})
    assert listed.status_code == 200 and len(listed.json()["data"]) >= 1

    # CSRF required on drain.
    no_csrf = await client.post(
        "/api/v1/compliance/ereceipts/drain", json={"branch_id": str(branch.id)}
    )
    assert no_csrf.status_code == 403 and no_csrf.json()["error"]["code"] == "E-AUTH-004"

    drained = await client.post(
        "/api/v1/compliance/ereceipts/drain",
        headers={"X-CSRF-Token": bm_csrf},
        json={"branch_id": str(branch.id)},
    )
    assert drained.status_code == 200 and drained.json()["data"]["accepted"] >= 1
