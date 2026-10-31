"""Stocktake (الجرد) — sessions, batch-level counting, one-transaction approval.

Design contract (owner-approved plan, 2026-10-31):
  * Count per BATCH — medication_batches is the only quantity truth (rule 15).
    Recording a count locks the batch row (FOR UPDATE) and snapshots
    system_qty_at_count, so a sale racing the count can never corrupt the
    diff; approval re-locks and applies counted - snapshot.
  * Approval lands in ONE transaction: every applied diff goes through
    inventory_service._apply_batch_adjustment (movement_type='adjustment',
    reference_type='stocktake', reference_id=session id) + one
    controlled_substance_log(source='stocktake') row per controlled line.
    Batch rows are locked COLLECTIVELY in a fixed order
    (medication_id, expiry_date, id) compatible with the sale path's
    per-medication expiry order — adjust-vs-sale cannot deadlock; a residual
    deadlock (40P01) retries the whole approval exactly once, then surfaces
    E-STK-004.
  * A line whose live quantity + diff < 0 at approval is flagged
    needs_recount and SKIPPED — one stale line never fails the session.
  * Blind count (default from stocktake_settings.blind_default): the counter
    never sees system_qty_at_count/diff until review. The mask is applied by
    ONE serializer used by the lines list, the count response and the CSV
    export — the three surfaces can never disagree.
  * Permissions: create/count/submit = inventory.count (cashier counts NORMAL
    lines; controlled lines are pharmacist+ only, enforced here); approve and
    cancel = inventory.stocktake_approve (manager-only — router-enforced).
"""

import csv
import datetime as dt
import io
import uuid
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.audit import AuditAction
from pharmaos_api.errors import ApiError, ErrorCode
from pharmaos_api.models import (
    ControlledSubstanceLog,
    Medication,
    MedicationBatch,
    Role,
    StocktakeLine,
    StocktakeSession,
    StocktakeSettings,
    User,
)
from pharmaos_api.services import audit_service
from pharmaos_api.services.csv_safe import csv_safe
from pharmaos_api.services.inventory_service import _apply_batch_adjustment, get_batch

SESSION_TYPES = ("daily_quick", "weekly_cycle", "custom")
_STATUSES = ("draft", "counting", "review", "approved", "cancelled")
REASON_CODES = ("miscount", "breakage", "theft", "found", "unknown")
_DEFAULT_OVERDUE_DAYS = 7

# Numeric shelf walk (A2 before A10, NULLS LAST) — same shape as
# inventory_service.list_inventory's sort=location; `si` is the
# branch_inventory alias of the lines query.
_SHELF_ORDER = (
    "(si.shelf_location IS NULL), SUBSTRING(si.shelf_location, 1, 1), "
    "CAST(SUBSTRING(si.shelf_location, 2) AS INT), m.trade_name"
)


# --------------------------- settings + scope ---------------------------


async def get_settings(session: AsyncSession, branch_id: uuid.UUID) -> StocktakeSettings:
    """One row per branch, created on first touch (defaults: 7 days, blind)."""
    row = (
        await session.execute(
            select(StocktakeSettings).where(StocktakeSettings.branch_id == branch_id)
        )
    ).scalar_one_or_none()
    if row is None:
        row = StocktakeSettings(branch_id=branch_id)
        session.add(row)
        await session.flush()
    return row


async def _daily_quick_batch_ids(
    session: AsyncSession, branch_id: uuid.UUID, *, movers_limit: int, value_limit: int
) -> tuple[list[uuid.UUID], dict[str, object]]:
    """Fastest movers (30d sales), highest on-hand value, controlled substances,
    cold-chain, and medications with past stocktake variances — union of batch ids."""
    movers = (await session.execute(text("""
                SELECT DISTINCT mb.id
                FROM invoice_items ii
                JOIN invoices i ON i.id = ii.invoice_id
                JOIN medication_batches mb ON mb.medication_id = ii.medication_id
                WHERE i.branch_id = :b AND NOT i.is_deleted
                  AND i.created_at >= NOW() - INTERVAL '30 days'
                  AND mb.branch_id = :b AND mb.status = 'active' AND NOT mb.is_deleted
                ORDER BY mb.id
                LIMIT :n
                """).bindparams(b=branch_id, n=movers_limit))).scalars().all()
    top_value = (await session.execute(text("""
                SELECT mb.id FROM medication_batches mb
                WHERE mb.branch_id = :b AND mb.status = 'active' AND NOT mb.is_deleted
                  AND mb.quantity > 0
                ORDER BY mb.quantity * COALESCE(mb.purchase_price, 0) DESC, mb.id
                LIMIT :n
                """).bindparams(b=branch_id, n=value_limit))).scalars().all()
    controlled = (await session.execute(text("""
                SELECT mb.id FROM medication_batches mb
                JOIN medications m ON m.id = mb.medication_id
                WHERE mb.branch_id = :b AND mb.status = 'active' AND NOT mb.is_deleted
                  AND mb.quantity > 0 AND m.controlled_substance
                ORDER BY mb.id
                """).bindparams(b=branch_id))).scalars().all()
    cold = (await session.execute(text("""
                SELECT mb.id FROM medication_batches mb
                JOIN medications m ON m.id = mb.medication_id
                WHERE mb.branch_id = :b AND mb.status = 'active' AND NOT mb.is_deleted
                  AND mb.quantity > 0 AND m.is_cold_chain
                ORDER BY mb.id
                """).bindparams(b=branch_id))).scalars().all()
    past_variance = (await session.execute(text("""
                SELECT DISTINCT mb.id
                FROM stocktake_lines sl
                JOIN stocktake_sessions ss ON ss.id = sl.session_id
                JOIN medication_batches mb ON mb.id = sl.batch_id
                WHERE ss.branch_id = :b AND ss.status = 'approved' AND NOT ss.is_deleted
                  AND sl.diff IS NOT NULL AND sl.diff <> 0
                  AND ss.approved_at >= NOW() - INTERVAL '90 days'
                  AND mb.status = 'active' AND NOT mb.is_deleted
                ORDER BY mb.id
                """).bindparams(b=branch_id))).scalars().all()
    ids = list(dict.fromkeys([*movers, *top_value, *controlled, *cold, *past_variance]))
    scope: dict[str, object] = {
        "movers_30d": len(movers),
        "top_value": len(top_value),
        "controlled": len(controlled),
        "cold_chain": len(cold),
        "past_variance_90d": len(past_variance),
        "movers_limit": movers_limit,
        "value_limit": value_limit,
    }
    return ids, scope


async def _weekly_batch_ids(
    session: AsyncSession, branch_id: uuid.UUID, zones: list[str]
) -> tuple[list[uuid.UUID], dict[str, object]]:
    """Every active batch whose branch_inventory shelf slot falls in one of the
    given zone prefixes (letters) — a week of rotations covers every shelf."""
    letters = [z.strip().upper()[:1] for z in zones if z.strip()]
    if not letters:
        raise ApiError(
            ErrorCode.STOCKTAKE_INVALID_STATE, 422, message="weekly_cycle needs at least one zone."
        )
    rows = (await session.execute(text("""
                SELECT mb.id
                FROM medication_batches mb
                JOIN medications m ON m.id = mb.medication_id
                JOIN branch_inventory si
                    ON si.branch_id = mb.branch_id AND si.medication_id = mb.medication_id
                WHERE mb.branch_id = :b AND mb.status = 'active' AND NOT mb.is_deleted
                  AND mb.quantity > 0
                  AND LEFT(COALESCE(si.shelf_location, ''), 1) = ANY(:zones)
                ORDER BY (si.shelf_location IS NULL), SUBSTRING(si.shelf_location, 1, 1),
                         CAST(SUBSTRING(si.shelf_location, 2) AS INT), m.trade_name
                """).bindparams(b=branch_id, zones=letters))).scalars().all()
    return list(rows), {"zones": letters, "matched": len(rows)}


async def create_session(
    session: AsyncSession,
    *,
    actor: User,
    branch_id: uuid.UUID,
    session_type: str,
    blind: bool | None = None,
    zones: list[str] | None = None,
    movers_limit: int = 30,
    value_limit: int = 30,
    notes: str | None = None,
) -> dict[str, object]:
    """Create a session in 'draft' with its line list already materialized;
    it enters 'counting' with the first recorded count (started_by/at set)."""
    if session_type not in SESSION_TYPES:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Unknown session type.")
    settings = await get_settings(session, branch_id)
    blind = settings.blind_default if blind is None else blind

    if session_type == "daily_quick":
        batch_ids, scope = await _daily_quick_batch_ids(
            session, branch_id, movers_limit=movers_limit, value_limit=value_limit
        )
    elif session_type == "weekly_cycle":
        batch_ids, scope = await _weekly_batch_ids(session, branch_id, zones or [])
    else:
        rows = (await session.execute(text("""
                    SELECT id FROM medication_batches
                    WHERE branch_id = :b AND status = 'active' AND NOT is_deleted
                      AND quantity > 0
                    ORDER BY id
                    """).bindparams(b=branch_id))).scalars().all()
        batch_ids, scope = list(rows), {"all_active": len(rows)}

    if not batch_ids:
        raise ApiError(
            ErrorCode.STOCKTAKE_INVALID_STATE, 422, message="Empty scope: nothing to count."
        )

    sess = StocktakeSession(
        branch_id=branch_id,
        session_type=session_type,
        scope=scope,
        status="draft",
        blind=blind,
        lines_expected=len(batch_ids),
        notes=(notes or None),
        created_by=actor.id,
    )
    session.add(sess)
    await session.flush()

    batch_rows = (await session.execute(text("""
                SELECT mb.id, mb.medication_id FROM medication_batches mb
                WHERE mb.id = ANY(:ids)
                """).bindparams(ids=batch_ids))).all()
    session.add_all(
        [
            StocktakeLine(
                session_id=sess.id,
                branch_id=branch_id,
                batch_id=bid,
                medication_id=mid,
                created_by=actor.id,
            )
            for bid, mid in batch_rows
        ]
    )
    await audit_service.record(
        session,
        AuditAction.STOCKTAKE_CREATED,
        actor=actor,
        branch_id=branch_id,
        entity_type="stocktake_session",
        entity_id=sess.id,
        metadata={"session_type": session_type, "blind": blind, "scope": scope},
    )
    await session.commit()
    return _session_dict(sess)


# ------------------------------ serializers ------------------------------


def _session_dict(sess: StocktakeSession) -> dict[str, object]:
    return {
        "id": str(sess.id),
        "branch_id": str(sess.branch_id),
        "session_type": sess.session_type,
        "scope": sess.scope,
        "status": sess.status,
        "blind": sess.blind,
        "started_by": str(sess.started_by) if sess.started_by else None,
        "started_at": sess.started_at.isoformat() if sess.started_at else None,
        "approved_by": str(sess.approved_by) if sess.approved_by else None,
        "approved_at": sess.approved_at.isoformat() if sess.approved_at else None,
        "lines_expected": sess.lines_expected,
        "lines_counted": sess.lines_counted,
        "notes": sess.notes,
    }


def _reveal_quantities(sess: StocktakeSession) -> bool:
    """Blind masking: system_qty_at_count/diff stay hidden until review —
    the ONE switch used by the lines list, count response and CSV export."""
    return (not sess.blind) or sess.status in ("review", "approved", "cancelled")


def _line_dict(
    line: StocktakeLine,
    *,
    info: dict[str, object],
    sess: StocktakeSession,
) -> dict[str, object]:
    reveal = _reveal_quantities(sess)
    return {
        "id": str(line.id),
        "session_id": str(line.session_id),
        "batch_id": str(line.batch_id),
        "medication_id": str(line.medication_id),
        "trade_name": info.get("trade_name"),
        "trade_name_ar": info.get("trade_name_ar"),
        "batch_number": info.get("batch_number"),
        "expiry_date": info.get("expiry_date"),
        "shelf_location": info.get("shelf_location"),
        "controlled_substance": info.get("controlled_substance", False),
        "system_qty_at_count": (
            str(line.system_qty_at_count)
            if reveal and line.system_qty_at_count is not None
            else None
        ),
        # live batch quantity at read time — drives the review screen's
        # stale-line highlight; hidden for blind sessions until review too
        "system_qty_current": (
            str(info.get("current_qty")) if reveal and info.get("current_qty") is not None else None
        ),
        "counted_qty": str(line.counted_qty) if line.counted_qty is not None else None,
        "diff": str(line.diff) if reveal and line.diff is not None else None,
        "reason_code": line.reason_code,
        "note": line.note,
        "counted_by": str(line.counted_by) if line.counted_by else None,
        "counted_at": line.counted_at.isoformat() if line.counted_at else None,
        "flag": line.flag,
    }


# ------------------------------ session reads ------------------------------


async def _load_session(
    session: AsyncSession, branch_id: uuid.UUID, session_id: uuid.UUID
) -> StocktakeSession:
    sess = (
        await session.execute(
            select(StocktakeSession).where(
                StocktakeSession.id == session_id,
                StocktakeSession.branch_id == branch_id,
                StocktakeSession.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if sess is None:
        raise ApiError(
            ErrorCode.STOCKTAKE_INVALID_STATE, 404, message="Stocktake session not found."
        )
    return sess


async def get_session_detail(
    session: AsyncSession, *, branch_id: uuid.UUID, session_id: uuid.UUID
) -> dict[str, object]:
    sess = await _load_session(session, branch_id, session_id)
    d = _session_dict(sess)
    d["lines_needing_recount"] = (await session.execute(text("""
                SELECT COUNT(*) FROM stocktake_lines
                WHERE session_id = :s AND flag = 'needs_recount'
                """).bindparams(s=session_id))).scalar_one()
    return d


async def list_sessions(
    session: AsyncSession,
    *,
    branch_id: uuid.UUID,
    status: str | None = None,
    skip: int = 0,
    limit: int = 50,
) -> tuple[list[dict[str, object]], int]:
    where = "branch_id = :b AND NOT is_deleted"
    params: dict[str, object] = {"b": branch_id}
    if status:
        if status not in _STATUSES:
            raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Unknown status.")
        where += " AND status = :st"
        params["st"] = status
    total = (
        await session.execute(
            text(
                f"SELECT COUNT(*) FROM stocktake_sessions WHERE {where}"  # noqa: S608 (fragments are constant)
            ).bindparams(**params)
        )
    ).scalar_one()
    rows = (
        (
            await session.execute(
                text(f"""
                SELECT id FROM stocktake_sessions WHERE {where}
                ORDER BY created_at DESC
                OFFSET :skip LIMIT :lim
                """).bindparams(  # noqa: S608 (fragments are constant; values are bound)
                    **params, skip=max(skip, 0), lim=min(max(limit, 1), 200)
                )
            )
        )
        .scalars()
        .all()
    )
    out = []
    for sid in rows:
        sess = await session.get(StocktakeSession, sid)
        if sess is None:  # deleted between the two queries — skip defensively
            continue
        out.append(_session_dict(sess))
    return out, total


async def list_lines(
    session: AsyncSession,
    *,
    branch_id: uuid.UUID,
    session_id: uuid.UUID,
    skip: int = 0,
    limit: int = 50,
) -> tuple[list[dict[str, object]], int]:
    """Lines ordered by the numeric shelf walk (reuse of the stocktake sort
    from the stock list), paginated; blind-masked until review."""
    sess = await _load_session(session, branch_id, session_id)
    base = """
        FROM stocktake_lines sl
        JOIN medications m ON m.id = sl.medication_id
        JOIN medication_batches mb ON mb.id = sl.batch_id
        LEFT JOIN branch_inventory si
            ON si.branch_id = sl.branch_id AND si.medication_id = sl.medication_id
        WHERE sl.session_id = :s AND NOT sl.is_deleted
        """
    total = (
        await session.execute(text(f"SELECT COUNT(*) {base}").bindparams(s=session_id))
    ).scalar_one()
    rows = (
        await session.execute(text(f"""
                SELECT sl.id, m.trade_name, m.trade_name_ar, m.controlled_substance,
                       mb.batch_number, mb.expiry_date, si.shelf_location, mb.quantity
                {base}
                ORDER BY {_SHELF_ORDER}
                OFFSET :skip LIMIT :lim
                """).bindparams(s=session_id, skip=max(skip, 0), lim=min(max(limit, 1), 200)))
    ).all()
    info_by_line = {
        r[0]: {
            "trade_name": r[1],
            "trade_name_ar": r[2],
            "controlled_substance": r[3],
            "batch_number": r[4],
            "expiry_date": r[5].isoformat() if r[5] else None,
            "shelf_location": r[6],
            "current_qty": r[7],
        }
        for r in rows
    }
    lines = (
        (
            await session.execute(
                text(f"""
                SELECT sl.id FROM stocktake_lines sl
                JOIN medications m ON m.id = sl.medication_id
                LEFT JOIN branch_inventory si
                    ON si.branch_id = sl.branch_id AND si.medication_id = sl.medication_id
                WHERE sl.session_id = :s AND NOT sl.is_deleted
                ORDER BY {_SHELF_ORDER}
                OFFSET :skip LIMIT :lim
                """).bindparams(  # noqa: S608 (fragments are constant; values are bound)
                    s=session_id, skip=max(skip, 0), lim=min(max(limit, 1), 200)
                )
            )
        )
        .scalars()
        .all()
    )
    orm_lines = {line.id: line for line in await _lines_by_ids(session, list(lines))}
    out = [
        _line_dict(orm_lines[lid], info=info_by_line[lid], sess=sess)
        for lid in lines
        if lid in orm_lines
    ]
    return out, total


async def _lines_by_ids(session: AsyncSession, ids: list[uuid.UUID]) -> list[StocktakeLine]:
    if not ids:
        return []
    return list(
        (await session.execute(select(StocktakeLine).where(StocktakeLine.id.in_(ids)))).scalars()
    )


# ------------------------------ mutations ------------------------------


async def _actor_role_code(session: AsyncSession, actor: User) -> str:
    return (await session.execute(select(Role.code).where(Role.id == actor.role_id))).scalar_one()


async def _is_controlled(session: AsyncSession, medication_id: uuid.UUID) -> bool:
    return bool(
        (
            await session.execute(
                select(Medication.controlled_substance).where(Medication.id == medication_id)
            )
        ).scalar_one()
    )


async def record_count(
    session: AsyncSession,
    *,
    actor: User,
    line_id: uuid.UUID,
    counted_qty: Decimal,
    reason_code: str | None = None,
    note: str | None = None,
) -> dict[str, object]:
    """Record (or re-record) one batch count. The batch is read UNDER LOCK and
    its live quantity snapshotted, so sales racing the count cannot corrupt
    the diff. Recounting is allowed while counting (and in review, to settle
    stale lines) — it re-snapshots against the fresh quantity."""
    if counted_qty < 0:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Counted quantity must be >= 0.")
    if reason_code is not None and reason_code not in REASON_CODES:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Unknown reason code.")

    line = (
        await session.execute(select(StocktakeLine).where(StocktakeLine.id == line_id))
    ).scalar_one_or_none()
    if line is None or line.is_deleted:
        raise ApiError(ErrorCode.STOCKTAKE_INVALID_STATE, 404, message="Stocktake line not found.")
    sess = await _load_session(session, line.branch_id, line.session_id)
    if sess.status in ("approved", "cancelled"):
        raise ApiError(ErrorCode.STOCKTAKE_INVALID_STATE, 422, message=f"Session is {sess.status}.")

    if await _is_controlled(session, line.medication_id):
        role = await _actor_role_code(session, actor)
        if role not in ("super_admin", "branch_manager", "pharmacist"):
            raise ApiError(
                ErrorCode.PERMISSION_DENIED,
                403,
                message="Controlled-substance lines are counted by a pharmacist only.",
            )

    # Lock the batch (same ordering as the sale path: batch row first, the
    # cache is untouched here) and snapshot its live quantity.
    batch = await get_batch(session, line.batch_id, for_update=True, branch_id=line.branch_id)
    line.system_qty_at_count = batch.quantity
    line.counted_qty = counted_qty
    line.diff = counted_qty - batch.quantity
    line.reason_code = reason_code
    line.note = note
    line.counted_by = actor.id
    line.counted_at = dt.datetime.now(dt.UTC)
    line.flag = None
    line.updated_by = actor.id

    if sess.status == "draft":
        sess.status = "counting"
        sess.started_by = actor.id
        sess.started_at = dt.datetime.now(dt.UTC)
    # raw SQL does not autoflush — push the pending line update down first
    await session.flush()
    sess.lines_counted = (await session.execute(text("""
                SELECT COUNT(*) FROM stocktake_lines
                WHERE session_id = :s AND counted_qty IS NOT NULL AND NOT is_deleted
                """).bindparams(s=sess.id))).scalar_one()

    await audit_service.record(
        session,
        AuditAction.STOCKTAKE_COUNT_RECORDED,
        actor=actor,
        branch_id=sess.branch_id,
        entity_type="stocktake_line",
        entity_id=line.id,
        metadata={
            "session_id": str(sess.id),
            "counted_qty": str(counted_qty),
            "system_qty_at_count": str(line.system_qty_at_count),
            "reason_code": reason_code,
        },
    )
    await session.commit()
    await session.refresh(line)
    return _line_dict(line, info=await _line_info(session, line), sess=sess)


async def _line_info(session: AsyncSession, line: StocktakeLine) -> dict[str, object]:
    r = (await session.execute(text("""
                SELECT m.trade_name, m.trade_name_ar, m.controlled_substance,
                       mb.batch_number, mb.expiry_date, si.shelf_location
                FROM medications m
                JOIN medication_batches mb ON mb.id = :b
                LEFT JOIN branch_inventory si
                    ON si.branch_id = :br AND si.medication_id = m.id
                WHERE m.id = :m
                """).bindparams(b=line.batch_id, br=line.branch_id, m=line.medication_id))).one()
    return {
        "trade_name": r[0],
        "trade_name_ar": r[1],
        "controlled_substance": r[2],
        "batch_number": r[3],
        "expiry_date": r[4].isoformat() if r[4] else None,
        "shelf_location": r[5],
    }


async def submit(
    session: AsyncSession, *, actor: User, branch_id: uuid.UUID, session_id: uuid.UUID
) -> dict[str, object]:
    """counting (or draft, if everything was recounted) -> review."""
    sess = await _load_session(session, branch_id, session_id)
    if sess.status not in ("draft", "counting"):
        raise ApiError(
            ErrorCode.STOCKTAKE_INVALID_STATE, 422, message=f"Cannot submit from {sess.status}."
        )
    if sess.lines_counted == 0:
        raise ApiError(ErrorCode.STOCKTAKE_INVALID_STATE, 422, message="Nothing counted yet.")
    sess.status = "review"
    sess.updated_by = actor.id
    await audit_service.record(
        session,
        AuditAction.STOCKTAKE_SUBMITTED,
        actor=actor,
        branch_id=sess.branch_id,
        entity_type="stocktake_session",
        entity_id=sess.id,
        metadata={"lines_counted": sess.lines_counted, "lines_expected": sess.lines_expected},
    )
    await session.commit()
    return _session_dict(sess)


async def approve(
    session: AsyncSession, *, actor: User, branch_id: uuid.UUID, session_id: uuid.UUID, reason: str
) -> dict[str, object]:
    """Apply every counted diff in ONE transaction (deadlock-safe), then
    approve. Lines whose live quantity moved against the diff (sale after
    count) are flagged needs_recount and skipped — approval never fails
    wholesale. Controlled diffs additionally write the append-only
    controlled_substance_log(source='stocktake')."""
    if not reason.strip():
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Reason is required.")

    from sqlalchemy.exc import OperationalError

    last_error: Exception | None = None
    for _attempt in (1, 2):  # exactly one retry on a residual deadlock (owner point 1)
        # EVERY attempt runs on its OWN disposable session: approval is a
        # self-contained unit of work that commits itself, and a fresh session
        # per attempt means a deadlocked attempt can never poison the next one
        # (a rolled-back transaction on this SQLAlchemy/asyncpg combination
        # leaves the same session unable to drive further IO — MissingGreenlet).
        from pharmaos_api.db import get_session_factory

        async with get_session_factory()() as attempt_session:
            try:
                return await _approve_once(
                    attempt_session,
                    actor=actor,
                    branch_id=branch_id,
                    session_id=session_id,
                    reason=reason,
                )
            except OperationalError as e:  # asyncpg 40P01 surfaces through SQLAlchemy
                pgcode = getattr(getattr(e, "orig", None), "sqlstate", None) or getattr(
                    getattr(e, "orig", None), "pgcode", None
                )
                if pgcode != "40P01":
                    raise
                last_error = e  # the context manager rolls the attempt back
    raise ApiError(
        ErrorCode.STOCKTAKE_APPROVAL_RACE,
        409,
        message="Stocktake approval hit concurrent updates twice; retry.",
    ) from last_error


async def _approve_once(
    session: AsyncSession, *, actor: User, branch_id: uuid.UUID, session_id: uuid.UUID, reason: str
) -> dict[str, object]:
    sess = await _load_session(session, branch_id, session_id)
    if sess.status != "review":
        raise ApiError(
            ErrorCode.STOCKTAKE_INVALID_STATE, 422, message=f"Cannot approve from {sess.status}."
        )
    lines = list(
        (
            await session.execute(
                select(StocktakeLine).where(
                    StocktakeLine.session_id == sess.id,
                    StocktakeLine.counted_qty.isnot(None),
                    StocktakeLine.is_deleted.is_(False),
                )
            )
        ).scalars()
    )

    # ONE collective lock in a fixed order compatible with the sale path —
    # every counted line's batch, ordered (medication_id, expiry_date, id).
    batch_ids = [line.batch_id for line in lines]
    locked_batches: dict[uuid.UUID, MedicationBatch] = {}
    if batch_ids:
        rows = (
            await session.execute(
                select(MedicationBatch)
                .where(MedicationBatch.id.in_(batch_ids))
                .order_by(
                    MedicationBatch.medication_id, MedicationBatch.expiry_date, MedicationBatch.id
                )
                .with_for_update()
            )
        ).scalars()
        locked_batches = {b.id: b for b in rows}

    controlled_rows: list[ControlledSubstanceLog] = []
    applied = needs_recount = skipped_zero = 0
    for line in lines:
        batch = locked_batches.get(line.batch_id)
        if batch is None:  # batch deleted mid-session — cannot settle
            line.flag = "needs_recount"
            needs_recount += 1
            continue
        if line.system_qty_at_count is None or line.counted_qty is None:
            line.flag = "needs_recount"  # defensive: cannot settle without a snapshot
            needs_recount += 1
            continue
        delta = line.counted_qty - line.system_qty_at_count
        if delta == 0:
            skipped_zero += 1
            continue
        if batch.quantity + delta < 0:
            # A sale moved the batch against the diff after the count — the
            # line is stale: flag it for a recount, never fail the session.
            line.flag = "needs_recount"
            line.updated_by = actor.id
            needs_recount += 1
            continue
        movement = await _apply_batch_adjustment(
            session,
            actor=actor,
            batch=batch,
            quantity_delta=delta,
            reason=f"{reason.strip()} [جرد]",
            reference_type="stocktake",
            reference_id=sess.id,
        )
        line.adjustment_movement_id = movement.id
        line.flag = None
        line.updated_by = actor.id
        applied += 1
        if await _is_controlled(session, line.medication_id):
            controlled_rows.append(
                ControlledSubstanceLog(
                    branch_id=sess.branch_id,
                    medication_id=line.medication_id,
                    batch_id=line.batch_id,
                    invoice_id=None,
                    invoice_item_id=None,
                    prescription_id=None,
                    quantity_dispensed=Decimal(0),
                    quantity_delta=delta,
                    source="stocktake",
                    stocktake_session_id=sess.id,
                    dispensed_by=actor.id,
                )
            )
    if controlled_rows:
        session.add_all(controlled_rows)

    sess.status = "approved"
    sess.approved_by = actor.id
    sess.approved_at = dt.datetime.now(dt.UTC)
    sess.updated_by = actor.id
    await audit_service.record(
        session,
        AuditAction.STOCKTAKE_APPROVED,
        actor=actor,
        branch_id=sess.branch_id,
        entity_type="stocktake_session",
        entity_id=sess.id,
        metadata={
            "applied": applied,
            "needs_recount": needs_recount,
            "zero_diff_skipped": skipped_zero,
            "controlled_rows": len(controlled_rows),
            "reason": reason.strip(),
        },
    )
    await session.commit()
    await session.refresh(sess)
    return {
        **_session_dict(sess),
        "applied": applied,
        "needs_recount": needs_recount,
        "zero_diff_skipped": skipped_zero,
        "controlled_rows": len(controlled_rows),
    }


async def cancel(
    session: AsyncSession, *, actor: User, branch_id: uuid.UUID, session_id: uuid.UUID, reason: str
) -> dict[str, object]:
    sess = await _load_session(session, branch_id, session_id)
    if sess.status in ("approved", "cancelled"):
        raise ApiError(
            ErrorCode.STOCKTAKE_INVALID_STATE, 422, message=f"Cannot cancel from {sess.status}."
        )
    sess.status = "cancelled"
    sess.updated_by = actor.id
    await audit_service.record(
        session,
        AuditAction.STOCKTAKE_CANCELLED,
        actor=actor,
        branch_id=sess.branch_id,
        entity_type="stocktake_session",
        entity_id=sess.id,
        metadata={"reason": reason.strip() or "manual"},
    )
    await session.commit()
    return _session_dict(sess)


# ------------------------------ export ------------------------------


async def export_csv(
    session: AsyncSession, *, branch_id: uuid.UUID, session_id: uuid.UUID
) -> tuple[str, str]:
    """Lines CSV (shelf-ordered). Blind-masked until review — same serializer
    switch as the API surfaces. Returns (filename, csv_text)."""
    sess = await _load_session(session, branch_id, session_id)
    lines, _total = await list_lines(
        session, branch_id=branch_id, session_id=session_id, skip=0, limit=100000
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "trade_name",
            "batch_number",
            "expiry_date",
            "shelf_location",
            "system_qty_at_count",
            "counted_qty",
            "diff",
            "reason_code",
            "note",
            "flag",
        ]
    )
    for line in lines:
        writer.writerow(
            [
                csv_safe(str(line.get("trade_name_ar") or line.get("trade_name") or "")),
                csv_safe(str(line.get("batch_number") or "")),
                csv_safe(str(line.get("expiry_date") or "")),
                csv_safe(str(line.get("shelf_location") or "")),
                csv_safe(str(line.get("system_qty_at_count") or "")),
                csv_safe(str(line.get("counted_qty") or "")),
                csv_safe(str(line.get("diff") or "")),
                csv_safe(str(line.get("reason_code") or "")),
                csv_safe(str(line.get("note") or "")),
                csv_safe(str(line.get("flag") or "")),
            ]
        )
    filename = f"stocktake-{sess.session_type}-{session_id}.csv"
    return filename, buf.getvalue()
