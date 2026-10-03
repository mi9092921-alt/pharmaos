"""P2-M11 — EDA Track & Trace outbox (adapter + local simulator).

Capturing 2D pack serials on receive enqueues 'receive' events; dispensing them
enqueues 'dispense' events — both inside the receive/sale transaction (never
block on the national system). The drain worker reports events via the local
simulator and audits tt_event.reported. An offline backlog drains with no loss;
pre-launch records can be imported. No real EDA acceptance is claimed (pending).
"""

import datetime as dt
import uuid
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.models import (
    Branch,
    Medication,
    MedicationPackaging,
    Role,
    TtEvent,
    User,
)
from pharmaos_api.security.passwords import hash_password
from pharmaos_api.services import inventory_service, sales_service
from pharmaos_api.services.compliance import eda_tt_adapter, tt_service
from pharmaos_api.services.sales_service import SaleLine

FUTURE = dt.date.today() + dt.timedelta(days=400)


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


def _gtin() -> str:
    return f"{uuid.uuid4().int % 10**14:014d}"


async def _make_med(db_session: AsyncSession, *, gtin: str) -> tuple[str, str]:
    unit_id = (
        await db_session.execute(
            text(
                "INSERT INTO units (name_ar) VALUES ('علبة') "
                "ON CONFLICT (name_ar) DO UPDATE SET name_ar=EXCLUDED.name_ar RETURNING id"
            )
        )
    ).scalar_one()
    await db_session.commit()
    med = Medication(trade_name=f"T {uuid.uuid4().hex[:6]}", trade_name_ar="صنف", gtin=gtin)
    db_session.add(med)
    await db_session.flush()
    box = MedicationPackaging(
        medication_id=med.id,
        level=1,
        unit_id=unit_id,
        name_ar="علبة",
        selling_price=Decimal("50.00"),
        is_default_sale=True,
    )
    db_session.add(box)
    await db_session.flush()
    bc = f"625{uuid.uuid4().int % 10**10:010d}"
    from pharmaos_api.models import MedicationBarcode

    db_session.add(MedicationBarcode(medication_id=med.id, packaging_id=box.id, barcode=bc))
    await db_session.commit()
    return str(med.id), bc


async def _receive(db_session, actor, branch, med_id, *, gtin, serials):  # type: ignore[no-untyped-def]
    await inventory_service.receive_stock(
        db_session,
        actor=actor,
        branch_id=branch.id,
        medication_id=uuid.UUID(med_id),
        batch_number=f"B-{uuid.uuid4().hex[:6]}",
        expiry_date=FUTURE,
        quantity=Decimal(len(serials) or 10),
        purchase_price=Decimal("1.00"),
        gtin=gtin,
        serials=serials,
    )


async def _count(db_session, branch, event_type, status=None):  # type: ignore[no-untyped-def]
    conds = [TtEvent.branch_id == branch.id, TtEvent.event_type == event_type]
    if status is not None:
        conds.append(TtEvent.status == status)
    return (await db_session.execute(select(func.count(TtEvent.id)).where(*conds))).scalar_one()


async def test_receive_enqueues_tt_events(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    g = _gtin()
    med_id, _bc = await _make_med(db_session, gtin=g)
    u = uuid.uuid4().hex[:8]
    await _receive(db_session, actor, branch, med_id, gtin=g, serials=[f"A-{u}", f"B-{u}"])

    assert await _count(db_session, branch, "receive", "pending") == 2


async def test_dispense_enqueues_tt_event(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    g = _gtin()
    med_id, bc = await _make_med(db_session, gtin=g)
    serial = f"D-{uuid.uuid4().hex[:8]}"
    await _receive(db_session, actor, branch, med_id, gtin=g, serials=[serial])

    await sales_service.create_sale(
        db_session,
        branch_id=branch.id,
        lines=[SaleLine(quantity=Decimal(1), barcode=bc)],
        cashier=actor,
        serials=[serial],
    )
    assert await _count(db_session, branch, "dispense", "pending") == 1


async def test_drain_reports_all_events(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    g = _gtin()
    med_id, _bc = await _make_med(db_session, gtin=g)
    u = uuid.uuid4().hex[:8]
    await _receive(
        db_session, actor, branch, med_id, gtin=g, serials=[f"R1-{u}", f"R2-{u}", f"R3-{u}"]
    )

    result = await tt_service.drain(db_session, branch_id=branch.id, actor=actor)
    assert result["processed"] == 3 and result["reported"] == 3
    assert await _count(db_session, branch, "receive", "reported") == 3
    assert await _count(db_session, branch, "receive", "pending") == 0

    audited = (
        await db_session.execute(
            text(
                "SELECT COUNT(*) FROM audit_logs WHERE action = 'tt_event.reported' "
                "AND branch_id = :b"
            ).bindparams(b=branch.id)
        )
    ).scalar_one()
    assert audited == 3
    assert eda_tt_adapter.adapter_is_simulated() is True


async def test_import_backfills_pending_events(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    count = await tt_service.import_events(
        db_session,
        actor=actor,
        branch_id=branch.id,
        rows=[
            {"gtin": _gtin(), "serial_number": "IMP-1", "batch_number": "L1", "expiry_date": None},
            {"gtin": _gtin(), "serial_number": "IMP-2"},
            {"gtin": "", "serial_number": "SKIP"},  # invalid — skipped
        ],
    )
    assert count == 2
    assert await _count(db_session, branch, "import", "pending") == 2

    result = await tt_service.drain(db_session, branch_id=branch.id, actor=actor)
    assert result["reported"] == 2


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


async def test_tt_api_permissions_and_csrf(
    client: httpx.AsyncClient, db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    g = _gtin()
    med_id, _bc = await _make_med(db_session, gtin=g)
    await _receive(db_session, actor, branch, med_id, gtin=g, serials=[f"X-{uuid.uuid4().hex[:8]}"])

    # data_entry lacks compliance.tt_report.
    await _login(client, await _seed_user(db_session, "data_entry"))
    denied = await client.get("/api/v1/compliance/tt-events", params={"branch_id": str(branch.id)})
    assert denied.status_code == 403

    # pharmacist HAS compliance.tt_report.
    ph_csrf = await _login(client, await _seed_user(db_session, "pharmacist"))
    listed = await client.get("/api/v1/compliance/tt-events", params={"branch_id": str(branch.id)})
    assert listed.status_code == 200 and len(listed.json()["data"]) >= 1

    no_csrf = await client.post(
        "/api/v1/compliance/tt-events/drain", json={"branch_id": str(branch.id)}
    )
    assert no_csrf.status_code == 403 and no_csrf.json()["error"]["code"] == "E-AUTH-004"

    drained = await client.post(
        "/api/v1/compliance/tt-events/drain",
        headers={"X-CSRF-Token": ph_csrf},
        json={"branch_id": str(branch.id)},
    )
    assert drained.status_code == 200 and drained.json()["data"]["reported"] >= 1
