"""Notifications (P3-M7) — the DELIVERY side of the alerting story.

Ratified decisions shaping this module:
- D4 — alerts are STATE (what is wrong, migration 2700); notifications are
  DELIVERY (how a human is told). This service never evaluates rules.
- D5 — in_app + desktop are delivered IN Phase 3 (sent_at set on creation;
  the client consumes them); email is QUEUED behind a provider gateway
  (sent_at stays NULL until a configured provider actually sends —
  acceptance is "pending provider", the compliance-adapter pattern). SMS is
  deferred to Phase 4 and never claimed here.
- D7 — notifications are NOT audit events; nothing here touches the audit log.

i18n contract: title_key/body_key + params — the API never ships localized
strings; the client renders t(title_key) / t(body_key) with {token}
interpolation, identical to alerts.

Visibility: a row targets ONE user (user_id) or broadcasts to the branch's
notification audience (user_id IS NULL) — the schema has no user↔branch
membership yet, so per-user fan-out is not enumerable; broadcast rows share
read_at (honest MVP, documented on the migration).
"""

import dataclasses
import datetime as dt
import json
import uuid
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.errors import ApiError, ErrorCode
from pharmaos_api.models import Alert

_CHANNELS = frozenset({"in_app", "desktop", "email"})
_PRIORITIES = frozenset({"low", "medium", "high", "critical"})
_MAX_PAGE = 100

# Alert severity -> notification priority (ALERT_RULES vocabulary mapping).
_SEVERITY_PRIORITY = {"warning": "medium", "danger": "high", "critical": "critical"}
# Channels per severity: in_app always; desktop for danger+; email queues for
# critical only (a manager-must-know set — keeps the pending queue meaningful).
_SEVERITY_CHANNELS: dict[str, tuple[str, ...]] = {
    "warning": ("in_app",),
    "danger": ("in_app", "desktop"),
    "critical": ("in_app", "desktop", "email"),
}


@dataclasses.dataclass(frozen=True)
class EmailDelivery:
    """Result of one gateway send attempt."""

    sent: bool
    reason: str


class EmailProvider(Protocol):
    """Gateway port (compliance-adapter pattern): a configured provider sends
    and returns success; ANY misconfiguration/failure returns sent=False and
    the queue keeps the row pending — delivery is never claimed or lost."""

    def send(
        self, *, to_email: str, subject: str, body: str
    ) -> EmailDelivery:  # pragma: no cover - interface
        ...


class NoopEmailProvider:
    """The Phase-3 default: NO provider is configured, so the gateway is an
    honest no-op — nothing is delivered, nothing is lost, the queue waits."""

    def send(self, *, to_email: str, subject: str, body: str) -> EmailDelivery:
        return EmailDelivery(sent=False, reason="provider_unconfigured")


def get_email_provider() -> EmailProvider:
    """The swap point for a real SMTP provider (Phase 4+ / owner configuration).
    Tests monkeypatch this to simulate a configured provider."""
    return NoopEmailProvider()


# ---------------------------------------------------------------------------
# creation
# ---------------------------------------------------------------------------

_INSERT_SQL = text("""
    INSERT INTO notifications (branch_id, user_id, channel, priority,
                               title_key, body_key, params, sent_at,
                               related_alert_id)
    VALUES (:b, :u, :channel, :priority, :title_key, :body_key,
            CAST(:params AS jsonb), :sent_at, :alert)
    RETURNING id
""")


async def notify(
    session: AsyncSession,
    *,
    branch_id: uuid.UUID,
    channel: str,
    priority: str,
    title_key: str,
    body_key: str,
    params: dict[str, object] | None = None,
    user_id: uuid.UUID | None = None,
    related_alert_id: uuid.UUID | None = None,
    deliver_now: bool = True,
) -> uuid.UUID:
    """Create one notification row. in_app/desktop are delivered on creation
    (sent_at = NOW() — the client consumes them); email rows are QUEUED with
    sent_at NULL and only mark sent when the gateway actually delivers."""
    if channel not in _CHANNELS or priority not in _PRIORITIES:
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Unknown channel/priority.")
    sent_at = dt.datetime.now(dt.UTC) if channel in ("in_app", "desktop") and deliver_now else None
    nid = (
        await session.execute(
            _INSERT_SQL.bindparams(
                b=branch_id,
                u=user_id,
                channel=channel,
                priority=priority,
                title_key=title_key,
                body_key=body_key,
                params=json.dumps(params or {}),
                sent_at=sent_at,
                alert=related_alert_id,
            )
        )
    ).scalar_one()
    return uuid.UUID(str(nid))


_SEVERITY_TITLE_KEY = {sev: f"alerts.rule_generic_{sev}" for sev in _SEVERITY_PRIORITY}


async def create_from_alert(session: AsyncSession, alert: Alert) -> int:
    """Fan one newly-created alert out into delivery rows (the alerts engine
    calls this for every CREATED alert, never for refreshes — dedup upstream
    means one notification burst per new condition).

    Channels per severity (D5): in_app always; desktop for danger+; email is
    QUEUED for critical only. Returns the number of rows created."""
    channels = _SEVERITY_CHANNELS[alert.severity]
    created = 0
    for channel in channels:
        await notify(
            session,
            branch_id=alert.branch_id,
            channel=channel,
            priority=_SEVERITY_PRIORITY[alert.severity],
            title_key=alert.message_key.replace("alerts.msg.", "alerts.rule_"),
            body_key=alert.message_key,
            params=dict(alert.params),
            related_alert_id=alert.id,
            deliver_now=True,
        )
        created += 1
    return created


async def dispatch_pending_email(session: AsyncSession, *, limit: int = 50) -> dict[str, int]:
    """Drain queued email notifications through the gateway. Without a
    configured provider this is an honest NO-OP (rows stay pending — the
    acceptance criterion "pending provider"); a configured provider marks
    sent_at only on an actual send. Failures keep rows pending (retryable).

    Scans the queue with a KEYSET cursor instead of a plain LIMIT: unaddressed
    rows stay pending forever, so a flat page of them would otherwise block
    the head of the queue and starve every newer addressable row."""
    provider = get_email_provider()
    sent = 0
    failed = 0
    skipped = 0
    attempted = 0
    cursor: tuple[dt.datetime, uuid.UUID] | None = None
    # Bound the work per drain: enough pages to walk a realistic device backlog
    # (skipped rows are re-scanned on every drain by design — they stay pending).
    for _page in range(40):
        bind: dict[str, object] = {"lim": _MAX_PAGE}
        cond = ""
        if cursor is not None:
            cond = "AND (created_at, id) > (:cursor_ts, :cursor_id) "
            bind["cursor_ts"] = cursor[0]
            bind["cursor_id"] = cursor[1]
        rows = (
            (
                await session.execute(
                    text(
                        "".join(
                            [
                                "SELECT id, created_at, ",
                                "       params->>'to_email' AS to_email, title_key, params ",
                                "FROM notifications ",
                                "WHERE channel = 'email' AND sent_at IS NULL AND NOT is_deleted ",
                                cond,
                                "ORDER BY created_at, id LIMIT :lim",
                            ]
                        )
                    ).bindparams(**bind)
                )
            )
            .mappings()
            .all()
        )
        if not rows:
            break
        for r in rows:
            cursor = (r["created_at"], r["id"])
            # Delivery needs an ADDRESSABLE recipient. Phase 3 carries no user
            # directory (no emails on users), so unaddressed rows stay
            # pending — nothing is claimed, nothing is lost (the "pending
            # provider" acceptance).
            to_email = r["to_email"]
            if not to_email:
                skipped += 1
                continue
            if attempted >= limit:
                continue  # budget spent; later rows wait for the next drain
            attempted += 1
            delivery = provider.send(
                to_email=to_email, subject=r["title_key"], body=json.dumps(r["params"])
            )
            if delivery.sent:
                await session.execute(
                    text("UPDATE notifications SET sent_at = NOW() WHERE id = :i").bindparams(
                        i=r["id"]
                    )
                )
                sent += 1
            else:
                failed += 1
        if attempted >= limit:
            break
    await session.commit()
    return {"attempted": attempted, "sent": sent, "failed": failed, "skipped": skipped}


# ---------------------------------------------------------------------------
# read models — list / unread count / mark read
# ---------------------------------------------------------------------------


async def list_notifications(
    session: AsyncSession,
    *,
    branch_id: uuid.UUID,
    user_id: uuid.UUID,
    unread_only: bool = False,
    skip: int = 0,
    limit: int = 50,
) -> dict[str, object]:
    """The viewer's notifications for a branch, newest first (page <= 100).

    Visibility: rows for THIS branch addressed to me OR broadcast (user_id
    NULL) — inlined in each query (no SQL string interpolation)."""
    limit = min(max(limit, 1), _MAX_PAGE)
    params: dict[str, object] = {
        "b": branch_id,
        "u": user_id,
        "skip": skip,
        "lim": limit,
    }
    unread = "AND n.read_at IS NULL" if unread_only else ""
    rows = (
        (
            await session.execute(
                text(
                    "".join(
                        [
                            "SELECT n.id, n.channel, n.priority, n.title_key, n.body_key,",
                            "       n.params, n.read_at, n.sent_at, n.related_alert_id,",
                            "       n.created_at ",
                            "FROM notifications n ",
                            "WHERE n.branch_id = :b AND NOT n.is_deleted ",
                            "  AND (n.user_id = :u OR n.user_id IS NULL) ",
                            "  " + unread + " ",
                            "ORDER BY n.created_at DESC ",
                            "OFFSET :skip LIMIT :lim",
                        ]
                    )
                ).bindparams(**params)
            )
        )
        .mappings()
        .all()
    )
    total = (
        await session.execute(
            text(
                "".join(
                    [
                        "SELECT COUNT(*) FROM notifications n ",
                        "WHERE n.branch_id = :b AND NOT n.is_deleted ",
                        "  AND (n.user_id = :u OR n.user_id IS NULL) ",
                        "  " + unread,
                    ]
                )
            ).bindparams(b=branch_id, u=user_id)
        )
    ).scalar_one()
    items = []
    for r in rows:
        item = dict(r)
        item["id"] = str(r["id"])
        item["related_alert_id"] = str(r["related_alert_id"]) if r["related_alert_id"] else None
        items.append(item)
    return {
        "branch_id": str(branch_id),
        "notifications": items,
        "pagination": {"skip": skip, "limit": limit, "total": int(total)},
    }


async def unread_count(
    session: AsyncSession, *, branch_id: uuid.UUID, user_id: uuid.UUID
) -> dict[str, object]:
    """The bell's data source: unread in_app rows visible to the viewer."""
    count = (await session.execute(text("""
                SELECT COUNT(*) FROM notifications n
                WHERE n.branch_id = :b AND NOT n.is_deleted
                  AND (n.user_id = :u OR n.user_id IS NULL)
                  AND n.read_at IS NULL AND n.channel = 'in_app'
                """).bindparams(b=branch_id, u=user_id))).scalar_one()
    return {"branch_id": str(branch_id), "unread": int(count)}


async def _visible_row(
    session: AsyncSession, notification_id: uuid.UUID, branch_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    row = (await session.execute(text("""
                SELECT 1 FROM notifications n
                WHERE n.id = :i AND n.branch_id = :b AND NOT n.is_deleted
                  AND (n.user_id = :u OR n.user_id IS NULL)
                """).bindparams(i=notification_id, b=branch_id, u=user_id))).first()
    return row is not None


async def mark_read(
    session: AsyncSession,
    *,
    notification_id: uuid.UUID,
    branch_id: uuid.UUID,
    user_id: uuid.UUID,
) -> dict[str, object]:
    """Mark one VISIBLE notification read (self-service — the recipient or a
    broadcast viewer; marking someone ELSE's private row is 404-grade 422)."""
    if not await _visible_row(session, notification_id, branch_id, user_id):
        raise ApiError(ErrorCode.VALIDATION_FAILED, 422, message="Notification not visible.")
    row = (await session.execute(text("""
                UPDATE notifications SET read_at = NOW()
                WHERE id = :i AND read_at IS NULL
                RETURNING id
                """).bindparams(i=notification_id))).first()
    await session.commit()
    if row is None:
        # Already read — idempotent no-op (not an error).
        return {"id": str(notification_id), "already_read": True}
    return {"id": str(row[0]), "already_read": False}


async def mark_all_read(
    session: AsyncSession, *, branch_id: uuid.UUID, user_id: uuid.UUID
) -> dict[str, object]:
    """Mark every VISIBLE unread notification read for this viewer."""
    result = (await session.execute(text("""
                UPDATE notifications n SET read_at = NOW()
                WHERE n.branch_id = :b AND NOT n.is_deleted
                  AND (n.user_id = :u OR n.user_id IS NULL)
                  AND n.read_at IS NULL
                RETURNING id
                """).bindparams(b=branch_id, u=user_id))).scalars().all()
    await session.commit()
    return {"branch_id": str(branch_id), "marked": len(result)}
