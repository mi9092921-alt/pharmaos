"""Audit Log (P1-M1): DB-level append-only immutability, the writer service,
action validation, and same-transaction wiring of invoice.created."""

import datetime as dt
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.audit import AuditAction
from pharmaos_api.models import (
    AuditLog,
    Branch,
    Medication,
    MedicationBarcode,
    MedicationBatch,
    MedicationPackaging,
    User,
)
from pharmaos_api.services import audit_service, sales_service
from pharmaos_api.services.sales_service import SaleLine


@pytest.fixture
async def actor(db_session: AsyncSession, seeded_user: dict) -> User:
    return (
        await db_session.execute(select(User).where(User.username == seeded_user["username"]))
    ).scalar_one()


async def test_record_writes_entry(db_session: AsyncSession, actor: User) -> None:
    marker = uuid.uuid4()
    await audit_service.record(
        db_session,
        AuditAction.USER_CREATED,
        actor=actor,
        entity_type="user",
        entity_id=marker,
        metadata={"note": "test"},
    )
    await db_session.commit()

    row = (
        await db_session.execute(select(AuditLog).where(AuditLog.entity_id == marker))
    ).scalar_one()
    assert row.action == "user.created"
    assert row.actor_user_id == actor.id
    assert row.actor_username == actor.username  # snapshot stored
    assert row.metadata_ == {"note": "test"}
    assert row.created_at is not None


async def test_unknown_action_rejected(db_session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="unknown audit action"):
        await audit_service.record(db_session, "totally.made_up")


async def test_append_only_update_blocked_at_db(db_session: AsyncSession, actor: User) -> None:
    await audit_service.record(db_session, AuditAction.BACKUP_CREATED, actor=actor)
    await db_session.commit()

    from pharmaos_api.db import get_session_factory

    # UPDATE must be refused by the DB trigger (role-independent protection).
    async with get_session_factory()() as s:
        with pytest.raises(Exception, match="append-only"):
            await s.execute(text("UPDATE audit_logs SET action = 'x' WHERE action IS NOT NULL"))
            await s.commit()

    # DELETE must be refused too.
    async with get_session_factory()() as s:
        with pytest.raises(Exception, match="append-only"):
            await s.execute(text("DELETE FROM audit_logs"))
            await s.commit()


async def test_app_user_role_revoked(db_session: AsyncSession) -> None:
    """The documented app_user role exists and lacks UPDATE/DELETE on audit_logs."""
    exists = (
        await db_session.execute(text("SELECT 1 FROM pg_roles WHERE rolname = 'app_user'"))
    ).scalar_one_or_none()
    assert exists == 1
    priv = (
        await db_session.execute(
            text(
                "SELECT has_table_privilege('app_user', 'audit_logs', 'UPDATE') "
                "OR has_table_privilege('app_user', 'audit_logs', 'DELETE')"
            )
        )
    ).scalar_one()
    assert priv is False


async def _seed_saleable(db_session: AsyncSession) -> tuple[uuid.UUID, str]:
    branch = Branch(name=f"فرع {uuid.uuid4().hex[:6]}", country_code="EG", currency_code="EGP")
    db_session.add(branch)
    unit_id = (
        await db_session.execute(
            text(
                "INSERT INTO units (name_ar) VALUES ('قرص') "
                "ON CONFLICT (name_ar) DO UPDATE SET name_ar=EXCLUDED.name_ar RETURNING id"
            )
        )
    ).scalar_one()
    med = Medication(trade_name=f"Med {uuid.uuid4().hex[:6]}", trade_name_ar="دواء")
    db_session.add(med)
    await db_session.flush()
    pkg = MedicationPackaging(
        medication_id=med.id,
        level=3,
        unit_id=unit_id,
        name_ar="قرص",
        qty_in_parent=Decimal(1),
        selling_price=Decimal("5.00"),
        is_default_sale=True,
    )
    db_session.add(pkg)
    await db_session.flush()
    barcode = f"629{uuid.uuid4().int % 10**10:010d}"
    db_session.add(MedicationBarcode(medication_id=med.id, packaging_id=pkg.id, barcode=barcode))
    db_session.add(
        MedicationBatch(
            branch_id=branch.id,
            medication_id=med.id,
            batch_number=f"AUD-{uuid.uuid4().hex[:6]}",
            expiry_date=dt.date.today() + dt.timedelta(days=200),
            quantity=Decimal(50),
            purchase_price=Decimal("1.00"),
        )
    )
    await db_session.commit()
    return branch.id, barcode


async def test_sale_writes_invoice_created_audit(db_session: AsyncSession, actor: User) -> None:
    branch_id, barcode = await _seed_saleable(db_session)
    invoice = await sales_service.create_sale(
        db_session,
        branch_id=branch_id,
        lines=[SaleLine(barcode=barcode, quantity=Decimal(2))],
        cashier=actor,
    )
    entry = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.entity_type == "invoice", AuditLog.entity_id == invoice.id
            )
        )
    ).scalar_one()
    assert entry.action == "invoice.created"
    assert entry.actor_user_id == actor.id
    assert entry.branch_id == branch_id
    assert entry.metadata_["invoice_number"] == invoice.invoice_number
    assert entry.metadata_["total"] == str(invoice.total)


async def test_failed_sale_writes_no_audit(db_session: AsyncSession, actor: User) -> None:
    branch_id, barcode = await _seed_saleable(db_session)
    before = (
        await db_session.execute(
            text("SELECT COUNT(*) FROM audit_logs WHERE action = 'invoice.created'")
        )
    ).scalar_one()
    from pharmaos_api.errors import ApiError

    with pytest.raises(ApiError):
        # 999 tablets > 50 in stock -> E-STK-001 before any invoice/audit is written.
        await sales_service.create_sale(
            db_session,
            branch_id=branch_id,
            lines=[SaleLine(barcode=barcode, quantity=Decimal(999))],
            cashier=actor,
        )
    await db_session.rollback()
    after = (
        await db_session.execute(
            text("SELECT COUNT(*) FROM audit_logs WHERE action = 'invoice.created'")
        )
    ).scalar_one()
    assert after == before  # atomicity: no audit for a sale that never happened
