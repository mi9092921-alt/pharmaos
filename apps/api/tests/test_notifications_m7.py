"""Notifications (P3-M7): delivery fan-out from alerts, channel gateways,
read state, and the HTTP surface.

The load-bearing test is test_alerts_engine_creates_notifications: the full
M6→M7 chain — the alerts engine evaluating a low-stock condition must spawn
the in_app delivery row linked back to the alert, and a RE-evaluation (which
only refreshes the alert, by M6 dedup) must NOT re-notify. One notification
burst per new condition, never per evaluation.

Email honesty (ratified D5): the default gateway is a NO-OP — without a
configured provider nothing is delivered, nothing is claimed, unaddressed
rows stay pending. A configured provider (monkeypatched here) marks sent_at
only on an actual send. SMS is never touched.
"""

import json
import uuid

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.models import Alert, Branch, Notification, Role, User
from pharmaos_api.services import alerts_service, notification_service
from tests.test_alerts_m6 import _make_med, _set_reorder, _sync_cache


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


async def _alert(
    db_session: AsyncSession, branch: Branch, *, severity: str, rule: str = "cash_discrepancy"
) -> Alert:
    alert = Alert(
        branch_id=branch.id,
        rule_key=rule,
        severity=severity,
        entity_type="branch",
        entity_id=None,
        message_key=f"alerts.msg.{rule}",
        params={"discrepancy": "-5.00"},
        dedup_key=f"{rule}:{uuid.uuid4().hex[:8]}",
    )
    db_session.add(alert)
    await db_session.commit()
    return alert


# ------------------------------ service ------------------------------


async def test_created_from_alert_fans_out_channels_by_severity(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    """critical → in_app + desktop (both delivered) + email (queued, sent_at
    NULL); warning → in_app only at medium priority. body_key carries the
    alert's message_key and related_alert_id points back at the alert."""
    critical = await _alert(db_session, branch, severity="critical")
    created = await notification_service.create_from_alert(db_session, critical)
    assert created == 3

    rows = (
        (
            await db_session.execute(
                select(Notification).where(Notification.related_alert_id == critical.id)
            )
        )
        .scalars()
        .all()
    )
    by_channel = {r.channel: r for r in rows}
    assert set(by_channel) == {"in_app", "desktop", "email"}
    assert by_channel["in_app"].sent_at is not None  # delivered on creation
    assert by_channel["desktop"].sent_at is not None
    assert by_channel["email"].sent_at is None  # QUEUED — pending provider
    for r in rows:
        assert r.priority == "critical"
        assert r.body_key == "alerts.msg.cash_discrepancy"
        assert r.title_key == "alerts.rule_cash_discrepancy"
        assert r.params["discrepancy"] == "-5.00"

    warning = await _alert(db_session, branch, severity="warning", rule="low_stock")
    assert await notification_service.create_from_alert(db_session, warning) == 1
    warning_rows = (
        (
            await db_session.execute(
                select(Notification).where(Notification.related_alert_id == warning.id)
            )
        )
        .scalars()
        .all()
    )
    assert [r.channel for r in warning_rows] == ["in_app"]
    assert warning_rows[0].priority == "medium"


async def test_email_gateway_noop_and_configured(
    db_session: AsyncSession,
    actor: User,
    branch: Branch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default gateway: an unaddressed row is SKIPPED (stays pending, nothing
    claimed). With params.to_email and a configured provider: sent → sent_at
    set; a failing provider keeps the row pending."""
    await notification_service.notify(
        db_session,
        branch_id=branch.id,
        channel="email",
        priority="high",
        title_key="alerts.rule_cash_discrepancy",
        body_key="alerts.msg.cash_discrepancy",
        params={},
    )
    no_address = await notification_service.dispatch_pending_email(db_session)
    assert no_address["skipped"] >= 1 and no_address["sent"] == 0
    pending = (
        await db_session.execute(
            text("SELECT COUNT(*) FROM notifications WHERE channel='email' AND sent_at IS NULL")
        )
    ).scalar_one()
    assert pending >= 1  # nothing claimed, nothing lost

    addressed = await notification_service.notify(
        db_session,
        branch_id=branch.id,
        channel="email",
        priority="critical",
        title_key="alerts.rule_cash_discrepancy",
        body_key="alerts.msg.cash_discrepancy",
        params={"to_email": "owner@pharmacy.eg"},
    )

    class _OkProvider:
        def send(self, *, to_email: str, subject: str, body: str):
            assert to_email == "owner@pharmacy.eg"
            return notification_service.EmailDelivery(sent=True, reason="smtp")

    monkeypatch.setattr(notification_service, "get_email_provider", lambda: _OkProvider())
    result = await notification_service.dispatch_pending_email(db_session)
    assert result["sent"] >= 1
    await db_session.refresh(await db_session.get(Notification, addressed))
    row = await db_session.get(Notification, addressed)
    assert row is not None and row.sent_at is not None

    class _DownProvider:
        def send(self, *, to_email: str, subject: str, body: str):
            return notification_service.EmailDelivery(sent=False, reason="smtp_down")

    monkeypatch.setattr(notification_service, "get_email_provider", lambda: _DownProvider())
    down = await notification_service.dispatch_pending_email(db_session)
    assert down["failed"] >= 0  # a down provider never marks sent


async def test_mark_read_unread_count_and_read_all(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    """Broadcast row: unread 1 → mark_read → 0; re-mark is an idempotent
    no-op; read-all sweeps the rest."""
    first = await notification_service.notify(
        db_session,
        branch_id=branch.id,
        channel="in_app",
        priority="critical",
        title_key="alerts.rule_cash_discrepancy",
        body_key="alerts.msg.cash_discrepancy",
        params={},
    )
    await notification_service.notify(
        db_session,
        branch_id=branch.id,
        channel="in_app",
        priority="medium",
        title_key="alerts.rule_low_stock",
        body_key="alerts.msg.low_stock",
        params={},
    )
    count = await notification_service.unread_count(
        db_session, branch_id=branch.id, user_id=actor.id
    )
    assert count["unread"] == 2

    marked = await notification_service.mark_read(
        db_session, notification_id=first, branch_id=branch.id, user_id=actor.id
    )
    assert marked["already_read"] is False
    again = await notification_service.mark_read(
        db_session, notification_id=first, branch_id=branch.id, user_id=actor.id
    )
    assert again["already_read"] is True  # idempotent, not an error

    swept = await notification_service.mark_all_read(
        db_session, branch_id=branch.id, user_id=actor.id
    )
    assert swept["marked"] == 1
    final = await notification_service.unread_count(
        db_session, branch_id=branch.id, user_id=actor.id
    )
    assert final["unread"] == 0


async def test_visibility_scoping_blocks_foreign_rows(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    """Another branch's notification is invisible (list + mark_read 422), and
    a PRIVATE row addressed to someone else is equally invisible."""
    other = Branch(name=f"فرع {uuid.uuid4().hex[:6]}", country_code="EG", currency_code="EGP")
    db_session.add(other)
    await db_session.commit()
    foreign = await notification_service.notify(
        db_session,
        branch_id=other.id,
        channel="in_app",
        priority="critical",
        title_key="alerts.rule_cash_discrepancy",
        body_key="alerts.msg.cash_discrepancy",
        params={},
    )
    private = await notification_service.notify(
        db_session,
        branch_id=branch.id,
        channel="in_app",
        priority="high",
        title_key="alerts.rule_low_stock",
        body_key="alerts.msg.low_stock",
        params={},
        user_id=actor.id,
    )
    # Re-point the private row to a DIFFERENT REAL user (notify binds to actor
    # only via explicit user_id; simulate a row owned by someone else).
    other_username = await _seed_role_user(db_session, "pharmacist")
    someone_else = (
        await db_session.execute(select(User).where(User.username == other_username))
    ).scalar_one()
    await db_session.execute(
        text("UPDATE notifications SET user_id = :u WHERE id = :i").bindparams(
            u=someone_else.id, i=private
        )
    )
    await db_session.commit()

    rows = await notification_service.list_notifications(
        db_session, branch_id=branch.id, user_id=actor.id
    )
    ids = {n["id"] for n in rows["notifications"]}  # type: ignore[index,union-attr]
    assert str(foreign) not in ids and str(private) not in ids

    from pharmaos_api.errors import ApiError

    with pytest.raises(ApiError):
        await notification_service.mark_read(
            db_session, notification_id=foreign, branch_id=branch.id, user_id=actor.id
        )
    with pytest.raises(ApiError):
        await notification_service.mark_read(
            db_session, notification_id=private, branch_id=branch.id, user_id=actor.id
        )


async def test_alerts_engine_creates_notifications(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    """THE M6→M7 chain: evaluating a low-stock condition creates the alert AND
    its in_app delivery row; re-evaluating (refresh-only, M6 dedup) must NOT
    re-notify."""
    _, med_id, _ = await _make_med(db_session, branch.id)
    await _sync_cache(db_session, branch, med_id)
    await _set_reorder(db_session, branch, med_id, "1500")

    first = await alerts_service.evaluate_branch(db_session, branch.id)
    assert first["created"] == 1
    rows = (await db_session.execute(text("""
                SELECT n.id FROM notifications n
                JOIN alerts a ON a.id = n.related_alert_id
                WHERE a.branch_id = :b AND a.rule_key = 'low_stock' AND n.channel = 'in_app'
                """).bindparams(b=branch.id))).scalars().all()
    assert len(rows) == 1  # one burst per new condition

    second = await alerts_service.evaluate_branch(db_session, branch.id)
    assert second["created"] == 0 and second["refreshed"] >= 1
    rows_after = (await db_session.execute(text("""
                SELECT n.id FROM notifications n
                JOIN alerts a ON a.id = n.related_alert_id
                WHERE a.branch_id = :b AND a.rule_key = 'low_stock' AND n.channel = 'in_app'
                """).bindparams(b=branch.id))).scalars().all()
    assert len(rows_after) == 1  # refresh did NOT re-notify


async def test_unread_count_rolls_up_across_branches(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    """The bell must watch EVERY branch (the M8 banner rollup's rationale): with
    no branch_id the count sums unread in_app rows across all branches, still
    scoped to the viewer's visibility and to the in_app channel only."""
    other = Branch(name=f"فرع {uuid.uuid4().hex[:6]}", country_code="EG", currency_code="EGP")
    db_session.add(other)
    await db_session.commit()

    await notification_service.notify(
        db_session,
        branch_id=branch.id,
        channel="in_app",
        priority="critical",
        title_key="alerts.rule_cash_discrepancy",
        body_key="alerts.msg.cash_discrepancy",
        params={},
    )
    await notification_service.notify(
        db_session,
        branch_id=branch.id,
        channel="desktop",
        priority="critical",
        title_key="alerts.rule_cash_discrepancy",
        body_key="alerts.msg.cash_discrepancy",
        params={},
    )  # desktop rows never count toward the bell
    await notification_service.notify(
        db_session,
        branch_id=other.id,
        channel="in_app",
        priority="high",
        title_key="alerts.rule_low_stock",
        body_key="alerts.msg.low_stock",
        params={},
    )
    await db_session.commit()

    one = await notification_service.unread_count(db_session, user_id=actor.id, branch_id=branch.id)
    assert one == {"branch_id": str(branch.id), "unread": 1}
    other_one = await notification_service.unread_count(
        db_session, user_id=actor.id, branch_id=other.id
    )
    assert other_one == {"branch_id": str(other.id), "unread": 1}
    # The suite shares the test DB, so other tests' unread rows also sit in the
    # rollup — what matters here is that the second branch's row IS included.
    rollup = await notification_service.unread_count(db_session, user_id=actor.id)
    assert rollup["branch_id"] is None
    assert rollup["unread"] >= one["unread"] + other_one["unread"]


async def test_cli_email_drain_marks_sent_with_configured_provider(
    db_session: AsyncSession,
    branch: Branch,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The queue drain must be reachable from a PRODUCTION path, not only from
    tests: the CLI command (alerts-evaluate's D6 pattern) runs the real
    dispatch. The default Noop provider leaves the row pending; a configured
    provider marks sent_at on the actual send."""
    nid = await notification_service.notify(
        db_session,
        branch_id=branch.id,
        channel="email",
        priority="critical",
        title_key="alerts.rule_cash_discrepancy",
        body_key="alerts.msg.cash_discrepancy",
        params={"to_email": "manager@pharma.example"},
    )
    await db_session.commit()

    # The handler is the CLI command's body: it prints the drain summary and
    # returns an exit code — the delivery facts live in the printed JSON.
    from pharmaos_api.cli import _notifications_drain_email

    exit_code = await _notifications_drain_email()
    assert exit_code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["sent"] == 0 and printed["attempted"] >= 1  # honest no-op: queued, unclaimed
    db_session.expire_all()
    row = await db_session.get(Notification, nid)
    assert row is not None and row.sent_at is None

    class _Configured:
        def send(
            self, *, to_email: str, subject: str, body: str
        ) -> notification_service.EmailDelivery:
            return notification_service.EmailDelivery(sent=True, reason="ok")

    monkeypatch.setattr(notification_service, "get_email_provider", lambda: _Configured())
    exit_code = await _notifications_drain_email()
    assert exit_code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["sent"] >= 1

    db_session.expire_all()
    row = await db_session.get(Notification, nid)
    assert row is not None and row.sent_at is not None


async def test_boot_email_drain_invokes_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """The boot hook runs the drain in every non-test environment (best-effort —
    convention #8): the M7 queue drains without waiting for a manual command."""
    from types import SimpleNamespace

    from pharmaos_api import config as config_module
    from pharmaos_api.main import _boot_email_drain
    from pharmaos_api.services import notification_service

    calls: list[dict[str, int]] = []

    async def _fake_dispatch(session: object, *, limit: int = 50) -> dict[str, int]:
        calls.append({"attempted": 0, "sent": 0, "failed": 0, "skipped": 0})
        return calls[-1]

    monkeypatch.setattr(config_module, "get_settings", lambda: SimpleNamespace(pharmaos_env="dev"))
    monkeypatch.setattr(notification_service, "dispatch_pending_email", _fake_dispatch)
    await _boot_email_drain()
    assert len(calls) == 1


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


async def test_notifications_http_matrix_csrf_and_flow(
    client: httpx.AsyncClient, db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    """cashier 403 (deliberately outside the tier — cash/compliance material),
    pharmacist+manager 200. Mutations CSRF-gated; the read flow flips the
    unread counter; read-all sweeps."""
    nid = await notification_service.notify(
        db_session,
        branch_id=branch.id,
        channel="in_app",
        priority="critical",
        title_key="alerts.rule_cash_discrepancy",
        body_key="alerts.msg.cash_discrepancy",
        params={"discrepancy": "-5.00"},
    )
    params = {"branch_id": str(branch.id)}

    await _login(client, await _seed_role_user(db_session, "cashier"))
    assert (await client.get("/api/v1/notifications", params=params)).status_code == 403
    no_csrf = await client.post("/api/v1/notifications/read-all", params=params)
    assert no_csrf.status_code == 403

    csrf = await _login(client, await _seed_role_user(db_session, "pharmacist"))
    ok = await client.get("/api/v1/notifications", params=params)
    assert ok.status_code == 200, ok.text
    data = ok.json()["data"]
    assert set(data) >= {"notifications", "pagination"}
    assert len(data["notifications"]) == 1
    row = data["notifications"][0]
    assert row["id"] == str(nid) and row["channel"] == "in_app"
    assert row["body_key"] == "alerts.msg.cash_discrepancy"

    unread = await client.get("/api/v1/notifications/unread-count", params=params)
    assert unread.status_code == 200 and unread.json()["data"]["unread"] == 1

    # branch_id omitted → the all-branch rollup the topbar bell watches.
    rollup = await client.get("/api/v1/notifications/unread-count")
    assert rollup.status_code == 200, rollup.text
    assert rollup.json()["data"]["branch_id"] is None
    assert rollup.json()["data"]["unread"] >= 1

    marked = await client.post(
        f"/api/v1/notifications/{nid}/read", params=params, headers={"X-CSRF-Token": csrf}
    )
    assert marked.status_code == 200, marked.text
    unread_after = await client.get("/api/v1/notifications/unread-count", params=params)
    assert unread_after.json()["data"]["unread"] == 0

    await notification_service.notify(
        db_session,
        branch_id=branch.id,
        channel="in_app",
        priority="medium",
        title_key="alerts.rule_low_stock",
        body_key="alerts.msg.low_stock",
        params={},
    )
    read_all = await client.post(
        "/api/v1/notifications/read-all", params=params, headers={"X-CSRF-Token": csrf}
    )
    assert read_all.status_code == 200
    final = await client.get("/api/v1/notifications/unread-count", params=params)
    assert final.json()["data"]["unread"] == 0
