"""EDA Track & Trace outbox service (P2-M11).

Events are enqueued 'pending' INSIDE the receive/sale transaction (no network,
no commit) — capture and dispensing never block on the national system. The
drain worker reports each event via the EDA port/adapter (local simulator by
default) and audits tt_event.reported. A pre-launch backlog can be imported so
there is no data gap when PharmaOS goes live.
"""

import datetime as dt
import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.audit import AuditAction
from pharmaos_api.models import PackSerial, TtEvent, User
from pharmaos_api.services import audit_service
from pharmaos_api.services.compliance import eda_tt_adapter

MAX_PAGE_SIZE = 100
_NEEDS_WORK = ("pending", "failed")


def _enqueue(
    session: AsyncSession,
    *,
    actor: User,
    event_type: str,
    pack: PackSerial,
    invoice_id: uuid.UUID | None = None,
) -> None:
    session.add(
        TtEvent(
            branch_id=pack.branch_id,
            event_type=event_type,
            pack_serial_id=pack.id,
            gtin=pack.gtin,
            serial_number=pack.serial_number,
            batch_number=None,
            invoice_id=invoice_id,
            status="pending",
            created_by=actor.id,
            updated_by=actor.id,
        )
    )


def enqueue_receive(session: AsyncSession, *, actor: User, packs: Sequence[PackSerial]) -> None:
    """Enqueue 'receive' events for freshly-captured pack serials (no commit)."""
    for pack in packs:
        _enqueue(session, actor=actor, event_type="receive", pack=pack)


def enqueue_dispense(
    session: AsyncSession, *, actor: User, packs: Sequence[PackSerial], invoice_id: uuid.UUID
) -> None:
    """Enqueue 'dispense' events for dispensed pack serials (no commit)."""
    for pack in packs:
        _enqueue(session, actor=actor, event_type="dispense", pack=pack, invoice_id=invoice_id)


def _build_payload(event: TtEvent) -> dict[str, object]:
    return {
        "event_type": event.event_type,
        "gtin": event.gtin,
        "serial_number": event.serial_number,
        "batch_number": event.batch_number,
        "expiry_date": event.expiry_date.isoformat() if event.expiry_date else None,
        "invoice_id": str(event.invoice_id) if event.invoice_id else None,
        "occurred_at": event.created_at.isoformat(),
    }


async def process_one(
    session: AsyncSession, *, event: TtEvent, actor: User | None = None
) -> TtEvent:
    """Report ONE event via the adapter; commits the row. Failure marks it
    'failed' and audits independently so the failure persists."""
    if event.status == "reported":
        return event
    adapter = eda_tt_adapter.get_adapter()
    event.report_attempts = event.report_attempts + 1
    payload = _build_payload(event)
    try:
        result = adapter.report(payload=payload)
        event.payload = payload
        event.status = "reported" if result.reported else "failed"
        event.reported_at = dt.datetime.now(dt.UTC)
        event.last_error = None
        # Keep the pack's denormalized report flag in step, when linked.
        if event.pack_serial_id is not None:
            pack = await session.get(PackSerial, event.pack_serial_id)
            if pack is not None:
                pack.tt_report_status = "reported"
        await audit_service.record(
            session,
            AuditAction.TT_EVENT_REPORTED,
            actor=actor,
            branch_id=event.branch_id,
            entity_type="tt_event",
            entity_id=event.id,
            metadata={
                "event_type": event.event_type,
                "serial_number": event.serial_number,
                "report_id": result.report_id,
                "simulated": adapter.is_simulator,
            },
        )
        await session.commit()
        await session.refresh(event)
        return event
    except eda_tt_adapter.EdaTtAdapterError as exc:
        event.status = "failed"
        event.last_error = str(exc)
        await session.commit()
        await audit_service.record_independent(
            AuditAction.TT_EVENT_REPORTED,
            actor=actor,
            branch_id=event.branch_id,
            entity_type="tt_event",
            entity_id=event.id,
            metadata={"event_type": event.event_type, "error": str(exc), "status": "failed"},
        )
        await session.refresh(event)
        return event


async def drain(
    session: AsyncSession, *, branch_id: uuid.UUID, actor: User | None = None, limit: int = 50
) -> dict[str, int]:
    events = (
        (
            await session.execute(
                select(TtEvent)
                .where(
                    TtEvent.branch_id == branch_id,
                    TtEvent.status.in_(_NEEDS_WORK),
                    TtEvent.is_deleted.is_(False),
                )
                .order_by(TtEvent.created_at)
                .limit(min(max(limit, 1), MAX_PAGE_SIZE))
            )
        )
        .scalars()
        .all()
    )
    reported = failed = 0
    for event in events:
        processed = await process_one(session, event=event, actor=actor)
        if processed.status == "reported":
            reported += 1
        else:
            failed += 1
    return {"processed": len(events), "reported": reported, "failed": failed}


async def import_events(
    session: AsyncSession,
    *,
    actor: User,
    branch_id: uuid.UUID,
    rows: list[dict[str, object]],
) -> int:
    """Backfill pre-launch serial records as 'import' events (pending) so there
    is no data gap at go-live. pack_serial_id is left null (the pack may not
    exist locally). Commits."""
    count = 0
    for r in rows:
        gtin = str(r.get("gtin", "")).strip()
        serial = str(r.get("serial_number", "")).strip()
        if not gtin or not serial:
            continue
        expiry_raw = r.get("expiry_date")
        expiry = dt.date.fromisoformat(str(expiry_raw)) if expiry_raw else None
        batch = r.get("batch_number")
        session.add(
            TtEvent(
                branch_id=branch_id,
                event_type="import",
                pack_serial_id=None,
                gtin=gtin,
                serial_number=serial,
                batch_number=str(batch).strip() if batch else None,
                expiry_date=expiry,
                status="pending",
                created_by=actor.id,
                updated_by=actor.id,
            )
        )
        count += 1
    await session.commit()
    return count


def _out(event: TtEvent) -> dict[str, object]:
    return {
        "id": str(event.id),
        "event_type": event.event_type,
        "gtin": event.gtin,
        "serial_number": event.serial_number,
        "invoice_id": str(event.invoice_id) if event.invoice_id else None,
        "status": event.status,
        "report_attempts": event.report_attempts,
        "last_error": event.last_error,
        "reported_at": event.reported_at.isoformat() if event.reported_at else None,
        "created_at": event.created_at.isoformat(),
    }


async def list_events(
    session: AsyncSession,
    *,
    branch_id: uuid.UUID,
    status: str | None = None,
    skip: int = 0,
    limit: int = 50,
) -> tuple[list[dict[str, object]], int]:
    capped = min(max(limit, 1), MAX_PAGE_SIZE)
    conditions = [TtEvent.branch_id == branch_id, TtEvent.is_deleted.is_(False)]
    if status is not None:
        conditions.append(TtEvent.status == status)
    total = (await session.execute(select(func.count(TtEvent.id)).where(*conditions))).scalar_one()
    rows = (
        (
            await session.execute(
                select(TtEvent)
                .where(*conditions)
                .order_by(TtEvent.created_at.desc())
                .offset(max(skip, 0))
                .limit(capped)
            )
        )
        .scalars()
        .all()
    )
    return [_out(e) for e in rows], int(total)
