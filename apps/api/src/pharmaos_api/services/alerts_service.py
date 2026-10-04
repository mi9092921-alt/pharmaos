"""Smart alerts engine (P3-M6) — evaluates CLAUDE.md's ALERT_RULES into the
`alerts` table with idempotent, deduplicated generation.

Ratified decisions shaping this module:
- D4 — alerts (state) and notifications (delivery, P3-M7) are separate tables.
- D6 — generation is on-demand (POST /alerts/evaluate) + at boot + via CLI
  (the expiry-sweep pattern). No scheduler in Phase 3; evaluation must never
  block or break a primary flow.
- D7 — acknowledging/resolving is NOT an audit event (closed audit log gains
  no new actions).

Idempotency spine: every (rule, entity) finding maps to a branch-scoped
`dedup_key`; `uq_alerts_dedup_active` (partial unique WHERE status <>
'resolved') guarantees one live alert per key. Re-evaluation upserts
(refreshing last_seen/params) and never duplicates; a cleared condition
RESOLVES its alert; a resolved condition that returns opens a FRESH alert
(new first_seen). Acknowledged alerts stay acknowledged while the condition
persists.

Rule coverage vs CLAUDE.md ALERT_RULES (honesty rule: a rule never pretends
to fire from a source that does not exist):
  low_stock / out_of_stock / expiry_critical / expiry_warning / expired /
  high_discount / cash_discrepancy / ereceipt_backlog / tt_report_failed /
  inventory_drift  — evaluated here.
  sync_failed      — NOT yet evaluable: no sync-outbox table exists in the
                     schema (device→cloud sync is beyond Phase 3). The rule is
                     registered so its key/i18n surface is stable; the
                     evaluator wires in when the queue lands.
  backup_overdue   — evaluated from the filesystem: BACKUP_PATH (the CLI
                     backup command's directory, default ./backups). Unset
                     BACKUP_PATH = rule inert (nothing to measure).
"""

import dataclasses
import datetime as dt
import json
import logging
import os
import uuid
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.errors import ApiError, ErrorCode
from pharmaos_api.models import Alert
from pharmaos_api.services import backup_service, inventory_service, notification_service

logger = logging.getLogger(__name__)

_ALERT_RULES: dict[str, dict[str, str]] = {
    # CLAUDE.md ALERT_RULES — key -> severity + entity vocabulary.
    "low_stock": {"severity": "warning", "entity_type": "medication"},
    "out_of_stock": {"severity": "critical", "entity_type": "medication"},
    "expiry_critical": {"severity": "critical", "entity_type": "batch"},
    "expiry_warning": {"severity": "warning", "entity_type": "batch"},
    "expired": {"severity": "danger", "entity_type": "batch"},
    "high_discount": {"severity": "warning", "entity_type": "invoice"},
    "cash_discrepancy": {"severity": "critical", "entity_type": "cash_session"},
    "ereceipt_backlog": {"severity": "critical", "entity_type": "branch"},
    "tt_report_failed": {"severity": "critical", "entity_type": "branch"},
    "sync_failed": {"severity": "warning", "entity_type": "branch"},
    "backup_overdue": {"severity": "critical", "entity_type": "branch"},
    "inventory_drift": {"severity": "critical", "entity_type": "medication"},
}

_BACKLOG_HOURS = 24
_BACKUP_MAX_AGE_HOURS = 24
_TT_FAILED_RETRIES = 3


@dataclasses.dataclass(frozen=True)
class Finding:
    """One live condition discovered by a rule evaluator."""

    rule_key: str
    entity_type: str
    entity_id: uuid.UUID | None
    params: dict[str, object]
    dedup_key: str  # branch-scoped: the unique index adds branch_id

    @property
    def severity(self) -> str:
        return _ALERT_RULES[self.rule_key]["severity"]

    @property
    def message_key(self) -> str:
        return f"alerts.msg.{self.rule_key}"


# ---------------------------------------------------------------------------
# rule evaluators — each returns the live findings for ONE rule for ONE branch
# ---------------------------------------------------------------------------


async def _eval_low_stock(session: AsyncSession, branch_id: uuid.UUID) -> list[Finding]:
    """cached_quantity <= reorder_point (and > 0 — zero is out_of_stock's job)."""
    rows = (await session.execute(text("""
                SELECT bi.medication_id, bi.cached_quantity, bi.reorder_point,
                       COALESCE(m.trade_name_ar, m.trade_name) AS name
                FROM branch_inventory bi
                JOIN medications m ON m.id = bi.medication_id
                WHERE bi.branch_id = :b AND NOT bi.is_deleted AND NOT m.is_deleted
                  AND bi.reorder_point IS NOT NULL
                  AND bi.cached_quantity > 0
                  AND bi.cached_quantity <= bi.reorder_point
                """).bindparams(b=branch_id))).all()
    return [
        Finding(
            rule_key="low_stock",
            entity_type="medication",
            entity_id=r[0],
            params={
                "medication_id": str(r[0]),
                "name": r[3],
                "quantity": str(r[1]),
                "reorder_point": str(r[2]),
            },
            dedup_key=f"low_stock:{r[0]}",
        )
        for r in rows
    ]


async def _eval_out_of_stock(session: AsyncSession, branch_id: uuid.UUID) -> list[Finding]:
    """cached_quantity == 0 for any tracked medication."""
    rows = (await session.execute(text("""
                SELECT bi.medication_id,
                       COALESCE(m.trade_name_ar, m.trade_name) AS name
                FROM branch_inventory bi
                JOIN medications m ON m.id = bi.medication_id
                WHERE bi.branch_id = :b AND NOT bi.is_deleted AND NOT m.is_deleted
                  AND bi.cached_quantity = 0
                """).bindparams(b=branch_id))).all()
    return [
        Finding(
            rule_key="out_of_stock",
            entity_type="medication",
            entity_id=r[0],
            params={"medication_id": str(r[0]), "name": r[1]},
            dedup_key=f"out_of_stock:{r[0]}",
        )
        for r in rows
    ]


async def _eval_expiry(
    session: AsyncSession, branch_id: uuid.UUID, *, rule_key: str
) -> list[Finding]:
    """Expiry horizon per ACTIVE batch with quantity (0-30d critical, 31-90d
    warning — bucket semantics mirror expiry_alerts so a batch appears in
    exactly one severity)."""
    if rule_key == "expiry_critical":
        min_days, max_days = 0, 30
    else:
        min_days, max_days = 31, 90
    rows = (await session.execute(text("""
                SELECT b.id, b.quantity, b.expiry_date,
                       COALESCE(m.trade_name_ar, m.trade_name) AS name
                FROM medication_batches b
                JOIN medications m ON m.id = b.medication_id
                WHERE b.branch_id = :b AND NOT b.is_deleted
                  AND NOT m.is_deleted
                  AND b.status = 'active' AND b.quantity > 0
                  AND b.expiry_date >= CURRENT_DATE + :min_days
                  AND b.expiry_date <= CURRENT_DATE + :max_days
                ORDER BY b.expiry_date
                """).bindparams(b=branch_id, min_days=min_days, max_days=max_days))).all()
    return [
        Finding(
            rule_key=rule_key,
            entity_type="batch",
            entity_id=r[0],
            params={
                "batch_id": str(r[0]),
                "name": r[3],
                "quantity": str(r[1]),
                "expiry_date": r[2].isoformat(),
                "window": str(max_days),
            },
            dedup_key=f"{rule_key}:{r[0]}",
        )
        for r in rows
    ]


async def _eval_expired(session: AsyncSession, branch_id: uuid.UUID) -> list[Finding]:
    """Batches the expiry sweep has already marked 'expired' (the ALERT_RULES
    action — quarantine — belongs to the sweep; the alert surfaces the state)."""
    rows = (await session.execute(text("""
                SELECT b.id, b.quantity, b.expiry_date,
                       COALESCE(m.trade_name_ar, m.trade_name) AS name
                FROM medication_batches b
                JOIN medications m ON m.id = b.medication_id
                WHERE b.branch_id = :b AND NOT b.is_deleted
                  AND NOT m.is_deleted AND b.status = 'expired'
                """).bindparams(b=branch_id))).all()
    return [
        Finding(
            rule_key="expired",
            entity_type="batch",
            entity_id=r[0],
            params={
                "batch_id": str(r[0]),
                "name": r[3],
                "quantity": str(r[1]),
                "expiry_date": r[2].isoformat(),
            },
            dedup_key=f"expired:{r[0]}",
        )
        for r in rows
    ]


async def _eval_high_discount(session: AsyncSession, branch_id: uuid.UUID) -> list[Finding]:
    """Invoices in the last 24h whose discount rate exceeded the branch's
    max_discount_percent (pre-discount gross = total + discount_amount).
    A limit of 0 means the branch configured no ceiling -> the rule is inert
    (loyalty redeem-as-discount is normal behavior, not a policy breach)."""
    limit_row = (await session.execute(text("""
                SELECT max_discount_percent FROM settings
                WHERE branch_id = :b AND NOT is_deleted
                """).bindparams(b=branch_id))).first()
    if limit_row is None or not limit_row[0]:
        return []
    max_pct = limit_row[0]
    rows = (await session.execute(text("""
                SELECT i.id, i.invoice_number, i.discount_amount,
                       (i.total + i.discount_amount) AS pre_discount
                FROM invoices i
                WHERE i.branch_id = :b AND NOT i.is_deleted AND i.status = 'completed'
                  AND i.discount_amount > 0
                  AND i.created_at >= NOW() - (:h || ' hours')::interval
                  AND i.discount_amount * 100
                      / NULLIF(i.total + i.discount_amount, 0) > :max_pct
                """).bindparams(b=branch_id, h=_BACKLOG_HOURS, max_pct=max_pct))).all()
    return [
        Finding(
            rule_key="high_discount",
            entity_type="invoice",
            entity_id=r[0],
            params={
                "invoice_id": str(r[0]),
                "invoice_number": r[1],
                "discount": str(r[2]),
                "max_percent": str(max_pct),
            },
            dedup_key=f"high_discount:{r[0]}",
        )
        for r in rows
    ]


async def _eval_cash_discrepancy(session: AsyncSession, branch_id: uuid.UUID) -> list[Finding]:
    """Closed drawers in the last 24h whose counted cash != expected."""
    rows = (await session.execute(text("""
                SELECT cs.id, cs.discrepancy, cs.closed_at
                FROM cash_sessions cs
                WHERE cs.branch_id = :b AND NOT cs.is_deleted AND cs.status = 'closed'
                  AND cs.discrepancy IS NOT NULL AND cs.discrepancy <> 0
                  AND cs.closed_at >= NOW() - (:h || ' hours')::interval
                """).bindparams(b=branch_id, h=_BACKLOG_HOURS))).all()
    return [
        Finding(
            rule_key="cash_discrepancy",
            entity_type="cash_session",
            entity_id=r[0],
            params={"session_id": str(r[0]), "discrepancy": str(r[1])},
            dedup_key=f"cash_discrepancy:{r[0]}",
        )
        for r in rows
    ]


async def _eval_ereceipt_backlog(session: AsyncSession, branch_id: uuid.UUID) -> list[Finding]:
    """ETA receipts stuck 'pending' for more than 24h (one branch-level alert)."""
    count = (await session.execute(text("""
                SELECT COUNT(*) FROM ereceipt_queue
                WHERE branch_id = :b AND NOT is_deleted
                  AND status = 'pending'
                  AND created_at < NOW() - (:h || ' hours')::interval
                """).bindparams(b=branch_id, h=_BACKLOG_HOURS))).scalar_one()
    if not count:
        return []
    return [
        Finding(
            rule_key="ereceipt_backlog",
            entity_type="branch",
            entity_id=None,
            params={"count": str(count), "hours": str(_BACKLOG_HOURS)},
            dedup_key="ereceipt_backlog",
        )
    ]


async def _eval_tt_report_failed(session: AsyncSession, branch_id: uuid.UUID) -> list[Finding]:
    """EDA events failed after more than 3 report attempts (branch aggregate —
    an unconfigured adapter must not storm per-event alerts)."""
    count = (await session.execute(text("""
                SELECT COUNT(*) FROM tt_events
                WHERE branch_id = :b AND NOT is_deleted
                  AND status = 'failed' AND report_attempts > :retries
                """).bindparams(b=branch_id, retries=_TT_FAILED_RETRIES))).scalar_one()
    if not count:
        return []
    return [
        Finding(
            rule_key="tt_report_failed",
            entity_type="branch",
            entity_id=None,
            params={"count": str(count), "retries": str(_TT_FAILED_RETRIES)},
            dedup_key="tt_report_failed",
        )
    ]


async def _eval_sync_failed(session: AsyncSession, branch_id: uuid.UUID) -> list[Finding]:
    """NOT YET EVALUABLE — the schema has no sync outbox (device->cloud sync is
    beyond Phase 3); registered in _ALERT_RULES so the key/i18n surface is
    stable. Wire the trigger here when the queue table lands. Never pretend."""
    return []


def _newest_backup_file() -> tuple[float, str] | None:
    """Sync filesystem scan (tiny local IO, safe inside the async evaluator):
    the newest encrypted backup file in the configured dir, or None."""
    raw = os.environ.get("BACKUP_PATH")
    if not raw:
        return None
    backup_dir = Path(raw)
    newest: tuple[float, str] | None = None
    try:
        for candidate in backup_dir.glob("*" + backup_service.BACKUP_SUFFIX):
            if candidate.is_file():
                mtime = candidate.stat().st_mtime
                if newest is None or mtime > newest[0]:
                    newest = (mtime, candidate.name)
    except OSError:
        return None
    return newest


async def _eval_backup_overdue(session: AsyncSession, branch_id: uuid.UUID) -> list[Finding]:
    """Last encrypted backup older than 24h, measured on the FILESYSTEM — the
    same source the CLI backup command writes to (BACKUP_PATH, default
    ./backups). BACKUP_PATH unset -> the rule is inert (nothing to measure)."""
    if os.environ.get("BACKUP_PATH") is None:
        return []
    newest = _newest_backup_file()
    if newest is not None:
        age_hours = (dt.datetime.now(dt.UTC).timestamp() - newest[0]) / 3600
        if age_hours <= _BACKUP_MAX_AGE_HOURS:
            return []
    return [
        Finding(
            rule_key="backup_overdue",
            entity_type="branch",
            entity_id=None,
            params={
                "hours": str(_BACKUP_MAX_AGE_HOURS),
                "last_backup": newest[1] if newest else None,
                "never": newest is None,
            },
            dedup_key="backup_overdue",
        )
    ]


async def _eval_inventory_drift(session: AsyncSession, branch_id: uuid.UUID) -> list[Finding]:
    """cached_quantity != SUM(active batches) — reuses the Phase-1 drift_check
    read model (boot_heal runs BEFORE alert evaluation, so a firing drift alert
    means the healer itself could not restore the invariant)."""
    drifted = await inventory_service.drift_check(session, branch_id)
    return [
        Finding(
            rule_key="inventory_drift",
            entity_type="medication",
            entity_id=uuid.UUID(row["medication_id"]),
            params={
                "medication_id": row["medication_id"],
                "cached": row["cached"],
                "truth": row["truth"],
            },
            dedup_key=f"inventory_drift:{row['medication_id']}",
        )
        for row in drifted
    ]


_EVALUATORS = {
    "low_stock": _eval_low_stock,
    "out_of_stock": _eval_out_of_stock,
    "expiry_critical": lambda s, b: _eval_expiry(s, b, rule_key="expiry_critical"),
    "expiry_warning": lambda s, b: _eval_expiry(s, b, rule_key="expiry_warning"),
    "expired": _eval_expired,
    "high_discount": _eval_high_discount,
    "cash_discrepancy": _eval_cash_discrepancy,
    "ereceipt_backlog": _eval_ereceipt_backlog,
    "tt_report_failed": _eval_tt_report_failed,
    "sync_failed": _eval_sync_failed,
    "backup_overdue": _eval_backup_overdue,
    "inventory_drift": _eval_inventory_drift,
}

# ---------------------------------------------------------------------------
# generation — idempotent upsert + stale resolution
# ---------------------------------------------------------------------------

_UPSERT_SQL = text("""
    INSERT INTO alerts (branch_id, rule_key, severity, entity_type, entity_id,
                        message_key, params, dedup_key)
    VALUES (:b, :rule, :severity, :entity_type, :entity_id,
            :message_key, CAST(:params AS jsonb), :dedup_key)
    ON CONFLICT (branch_id, dedup_key) WHERE status <> 'resolved'
    DO UPDATE SET last_seen = NOW(),
                  severity = EXCLUDED.severity,
                  message_key = EXCLUDED.message_key,
                  params = EXCLUDED.params
    RETURNING id, (xmax = 0) AS inserted
""")

_RESOLVE_SQL = text("""
    UPDATE alerts SET status = 'resolved'
    WHERE branch_id = :b AND rule_key = :rule AND NOT is_deleted
      AND status <> 'resolved'
      AND NOT (dedup_key = ANY(CAST(:keys AS text[])))
    RETURNING id
""")


async def _generate(
    session: AsyncSession, branch_id: uuid.UUID, rule_key: str
) -> tuple[int, int, int, int]:
    """Run one rule's evaluator, upsert its findings, resolve stale alerts,
    and notify on NEWLY CREATED alerts (P3-M7 — one notification burst per new
    condition; refreshes never re-notify). Returns
    (created, refreshed, live_findings, resolved)."""
    findings = await _EVALUATORS[rule_key](session, branch_id)
    created = 0
    refreshed = 0
    keys: list[str] = []
    new_alerts: list[Alert] = []
    for f in findings:
        row = (
            await session.execute(
                _UPSERT_SQL.bindparams(
                    b=branch_id,
                    rule=rule_key,
                    severity=f.severity,
                    entity_type=f.entity_type,
                    entity_id=f.entity_id,
                    message_key=f.message_key,
                    params=json.dumps(f.params),
                    dedup_key=f.dedup_key,
                )
            )
        ).first()
        if row is None:  # pragma: no cover — the upsert always returns its row
            raise RuntimeError("alert upsert returned no row")
        if row.inserted:
            created += 1
            alert = await session.get(Alert, row.id)
            if alert is not None:
                new_alerts.append(alert)
        else:
            refreshed += 1
        keys.append(f.dedup_key)
    result = await session.execute(_RESOLVE_SQL.bindparams(b=branch_id, rule=rule_key, keys=keys))
    resolved = len(result.scalars().all())
    for alert in new_alerts:
        # Delivery fan-out (in_app/desktop/email per severity) — best-effort by
        # convention #8: a notification failure must never break alert
        # generation. The SAVEPOINT keeps the alert upserts alive if the
        # fan-out raises (a bare rollback would poison the whole transaction).
        try:
            async with session.begin_nested():
                await notification_service.create_from_alert(session, alert)
        except Exception:  # noqa: BLE001 — delivery is never load-bearing
            logger.exception("notification fan-out failed for alert %s", alert.id)
    await session.commit()
    return created, refreshed, len(findings), resolved


async def evaluate_branch(session: AsyncSession, branch_id: uuid.UUID) -> dict[str, object]:
    """Evaluate EVERY rule for one branch, idempotently. Safe to call as often
    as wanted (boot / cron / manual) — repeats never duplicate alerts."""
    by_rule: dict[str, int] = {}
    created_total = 0
    refreshed_total = 0
    resolved_total = 0
    for rule_key in _ALERT_RULES:
        created, refreshed, findings, resolved = await _generate(session, branch_id, rule_key)
        created_total += created
        refreshed_total += refreshed
        resolved_total += resolved
        by_rule[rule_key] = findings
    live = (await session.execute(text("""
                SELECT COUNT(*) FROM alerts
                WHERE branch_id = :b AND NOT is_deleted AND status <> 'resolved'
                """).bindparams(b=branch_id))).scalar_one()
    return {
        "branch_id": str(branch_id),
        "created": created_total,
        "refreshed": refreshed_total,
        "resolved": resolved_total,
        "findings": by_rule,
        "live_alerts": int(live),
    }


async def evaluate_all(session: AsyncSession) -> dict[str, object]:
    """Evaluate every ACTIVE branch (boot / CLI entry point)."""
    branches = (
        (await session.execute(text("SELECT id FROM branches WHERE NOT is_deleted")))
        .scalars()
        .all()
    )
    results = [await evaluate_branch(session, b) for b in branches]
    return {"branches": len(results), "results": results}


# ---------------------------------------------------------------------------
# lifecycle — list / acknowledge / resolve
# ---------------------------------------------------------------------------


async def list_alerts(
    session: AsyncSession,
    *,
    branch_id: uuid.UUID,
    status: str = "active",
    skip: int = 0,
    limit: int = 50,
) -> dict[str, object]:
    """Branch alert list, newest condition first (page <= 100)."""
    if status not in {"active", "acknowledged", "resolved", "all"}:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Unknown status filter.")
    limit = min(max(limit, 1), 100)
    params: dict[str, object] = {"b": branch_id, "skip": skip, "lim": limit, "st": status}
    rows = (await session.execute(text("""
                SELECT id, rule_key, severity, entity_type, entity_id,
                       message_key, params, status, first_seen, last_seen,
                       acknowledged_by, acknowledged_at
                FROM alerts
                WHERE branch_id = :b AND NOT is_deleted
                  AND (:st = 'all' OR status = :st)
                ORDER BY CASE severity
                             WHEN 'critical' THEN 0
                             WHEN 'danger' THEN 1
                             ELSE 2 END,
                         last_seen DESC
                OFFSET :skip LIMIT :lim
                """).bindparams(**params))).mappings().all()
    total = (await session.execute(text("""
                SELECT COUNT(*) FROM alerts
                WHERE branch_id = :b AND NOT is_deleted
                  AND (:st = 'all' OR status = :st)
                """).bindparams(b=branch_id, st=status))).scalar_one()
    items = []
    for r in rows:
        item = dict(r)
        item["id"] = str(r["id"])
        item["entity_id"] = str(r["entity_id"]) if r["entity_id"] else None
        item["acknowledged_by"] = str(r["acknowledged_by"]) if r["acknowledged_by"] else None
        items.append(item)
    return {
        "branch_id": str(branch_id),
        "alerts": items,
        "pagination": {"skip": skip, "limit": limit, "total": int(total)},
    }


async def alert_summary(
    session: AsyncSession, *, branch_id: uuid.UUID | None = None
) -> dict[str, object]:
    """Live-alert counts by severity — the dashboard banner's data source.

    ``branch_id=None`` rolls up across ALL branches (P3-M8 polish): the banner
    must never hide a second branch's emergency behind a first-branch-only
    query. With a branch_id the response stays exactly the M6 shape."""
    if branch_id is None:
        rows = (await session.execute(text("""
                    SELECT severity, COUNT(*) FROM alerts
                    WHERE NOT is_deleted AND status <> 'resolved'
                    GROUP BY severity
                    """))).all()
    else:
        rows = (await session.execute(text("""
                    SELECT severity, COUNT(*) FROM alerts
                    WHERE branch_id = :b AND NOT is_deleted AND status <> 'resolved'
                    GROUP BY severity
                    """).bindparams(b=branch_id))).all()
    by_severity = {r[0]: int(r[1]) for r in rows}
    return {
        "branch_id": str(branch_id) if branch_id is not None else None,
        "warning": by_severity.get("warning", 0),
        "danger": by_severity.get("danger", 0),
        "critical": by_severity.get("critical", 0),
        "total": sum(by_severity.values()),
    }


async def acknowledge(
    session: AsyncSession, *, alert_id: uuid.UUID, actor_id: uuid.UUID
) -> dict[str, object]:
    """A human saw it: active -> acknowledged (idempotent no-op if already
    acknowledged/resolved)."""
    row = (await session.execute(text("""
                UPDATE alerts SET status = 'acknowledged',
                       acknowledged_by = :actor, acknowledged_at = NOW()
                WHERE id = :i AND NOT is_deleted AND status = 'active'
                RETURNING id, status
                """).bindparams(i=alert_id, actor=actor_id))).first()
    if row is None:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Alert is not active.")
    await session.commit()
    return {"id": str(row[0]), "status": row[1]}


async def resolve(session: AsyncSession, *, alert_id: uuid.UUID) -> dict[str, object]:
    """Handled: active|acknowledged -> resolved (the lifecycle sink)."""
    row = (await session.execute(text("""
                UPDATE alerts SET status = 'resolved'
                WHERE id = :i AND NOT is_deleted AND status <> 'resolved'
                RETURNING id, status
                """).bindparams(i=alert_id))).first()
    if row is None:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Alert is not resolvable.")
    await session.commit()
    return {"id": str(row[0]), "status": row[1]}
