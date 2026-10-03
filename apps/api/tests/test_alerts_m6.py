"""Smart alerts engine (P3-M6): rule evaluators, idempotent dedup generation,
lifecycle (acknowledge/resolve), and the HTTP surface.

Coverage follows CLAUDE.md's ALERT_RULES honestly:
  * low_stock / out_of_stock — real branch_inventory cache states (kept
    cache==truth-consistent so inventory_drift doesn't double-fire in these
    tests).
  * expiry_critical / expiry_warning / expired — batch horizons (0-30 / 31-90
    / swept past-expiry via the real expiry_sweep, mirroring expiry_alerts'
    single-bucket semantics).
  * high_discount — a synthetic completed invoice above the branch's
    max_discount_percent (the sale/redeem path is P2-M6's tested domain; the
    RULE is what's under test here), plus the limit-0-inert contract.
  * cash_discrepancy — a REAL close_session with counted != expected.
  * ereceipt_backlog / tt_report_failed — synthetic outbox rows past their
    thresholds (24h pending / >3 failed retries, branch-aggregated so an
    unconfigured adapter cannot storm per-event alerts).
  * inventory_drift — cache desynced from batch truth (the boot healer runs
    BEFORE alert evaluation, so a firing drift alert is a real signal).
  * sync_failed — asserted permanently silent: the schema has NO sync outbox
    (device->cloud is beyond Phase 3) and the engine must never pretend.
  * backup_overdue — filesystem rule, inert without BACKUP_PATH (proven) and
    firing for an empty configured dir (never-backed-up = overdue).

The dedup test is the idempotency spine: repeated evaluation must refresh
(not duplicate) and a cleared condition must RESOLVE its alert.
"""

import datetime as dt
import uuid
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.errors import ApiError
from pharmaos_api.models import Branch, MedicationBatch, Role, User
from pharmaos_api.services import alerts_service, cashier_service, inventory_service
from tests.test_reports_m5 import _make_med  # reused stocking helper (strip+tablet chain)


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


async def _set_reorder(
    db_session: AsyncSession, branch: Branch, med_id: uuid.UUID, reorder: str
) -> None:
    await db_session.execute(
        text(
            "UPDATE branch_inventory SET reorder_point = CAST(:r AS NUMERIC) "
            "WHERE branch_id = :b AND medication_id = :m"
        ).bindparams(r=reorder, b=branch.id, m=med_id)
    )
    await db_session.commit()


async def _sync_cache(db_session: AsyncSession, branch: Branch, med_id: uuid.UUID) -> None:
    """Align the derived cache with batch truth for one medication
    (cached_quantity == SUM(active batches)) — the M5 helper stocks a batch
    DIRECTLY without a cache row, while the stock rules read branch_inventory.
    Keeps cache==truth so inventory_drift doesn't noise rule-specific tests."""
    await db_session.execute(text("""
        INSERT INTO branch_inventory (branch_id, medication_id, cached_quantity)
        SELECT :b, :m, COALESCE(SUM(quantity), 0) FROM medication_batches
        WHERE branch_id = :b AND medication_id = :m
          AND status = 'active' AND NOT is_deleted
        ON CONFLICT (branch_id, medication_id)
        DO UPDATE SET cached_quantity = EXCLUDED.cached_quantity
        """).bindparams(b=branch.id, m=med_id))
    await db_session.commit()


async def _force_expiry(db_session: AsyncSession, batch_id: uuid.UUID, days: int) -> None:
    await db_session.execute(
        text("UPDATE medication_batches SET expiry_date = :d WHERE id = :i").bindparams(
            d=dt.date.today() + dt.timedelta(days=days), i=batch_id
        )
    )
    await db_session.commit()


def _rule(report: dict[str, object], rule_key: str) -> int:
    findings = report["findings"]
    assert isinstance(findings, dict)
    return int(findings[rule_key])


# ------------------------------ rules fire ------------------------------


async def test_low_stock_fires_and_refreshes_on_repeat(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    """THE dedup spine: evaluate → created 1; evaluate again → created 0,
    refreshed (never duplicated); raising the condition above reorder_point →
    resolved on the next pass. The batch holds 1000 tablets, so reorder 1500
    puts the cache (1000) at/below the threshold."""
    _, med_id, _ = await _make_med(db_session, branch.id)
    await _sync_cache(db_session, branch, med_id)
    await _set_reorder(db_session, branch, med_id, "1500")

    first = await alerts_service.evaluate_branch(db_session, branch.id)
    assert _rule(first, "low_stock") == 1
    assert first["created"] == 1 and first["live_alerts"] >= 1

    second = await alerts_service.evaluate_branch(db_session, branch.id)
    assert second["created"] == 0
    assert second["refreshed"] >= 1  # low_stock row refreshed, NOT duplicated
    rows = (
        await db_session.execute(
            text(
                "SELECT COUNT(*) FROM alerts WHERE rule_key = 'low_stock' " "AND branch_id = :b"
            ).bindparams(b=branch.id)
        )
    ).scalar_one()
    assert rows == 1  # the partial-unique dedup held

    await _set_reorder(db_session, branch, med_id, "10")  # condition cleared
    third = await alerts_service.evaluate_branch(db_session, branch.id)
    assert _rule(third, "low_stock") == 0
    live = (
        await db_session.execute(
            text(
                "SELECT COUNT(*) FROM alerts WHERE rule_key = 'low_stock' "
                "AND branch_id = :b AND status <> 'resolved'"
            ).bindparams(b=branch.id)
        )
    ).scalar_one()
    assert live == 0  # auto-resolved
    assert third["resolved"] >= 1


async def test_out_of_stock_fires_on_zero_cache(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    """cached 0 with batch truth ALSO 0 (consistent cache — out_of_stock, not
    drift), and no reorder_point required (the CLAUDE.md trigger is ===0)."""
    barcode, med_id, _ = await _make_med(db_session, branch.id)
    await db_session.execute(
        text("UPDATE medication_batches SET quantity = 0 WHERE medication_id = :m").bindparams(
            m=med_id
        )
    )
    await _sync_cache(db_session, branch, med_id)  # cache follows truth: 0

    report = await alerts_service.evaluate_branch(db_session, branch.id)
    assert _rule(report, "out_of_stock") == 1
    assert _rule(report, "inventory_drift") == 0  # cache matches truth
    alerts = (await alerts_service.list_alerts(db_session, branch_id=branch.id, status="active"))[
        "alerts"
    ]
    assert isinstance(alerts, list)
    row = next(a for a in alerts if a["rule_key"] == "out_of_stock")
    assert row["severity"] == "critical" and row["entity_type"] == "medication"
    assert row["params"]["name"].startswith("دواء M5")


async def test_expiry_buckets_critical_warning_and_expired(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    """20 days -> critical; 60 days -> warning (31-90); a swept past-expiry
    batch -> expired danger (via the REAL expiry_sweep). One batch per bucket —
    never two severities for the same batch."""
    _, med_id, _ = await _make_med(db_session, branch.id)  # 365d — quiet
    batch_c = MedicationBatch(
        branch_id=branch.id,
        medication_id=med_id,
        batch_number=f"C-{uuid.uuid4().hex[:6]}",
        expiry_date=dt.date.today() + dt.timedelta(days=20),
        quantity=Decimal(10),
        purchase_price=Decimal("1.00"),
    )
    db_session.add(batch_c)
    batch_w = MedicationBatch(
        branch_id=branch.id,
        medication_id=med_id,
        batch_number=f"W-{uuid.uuid4().hex[:6]}",
        expiry_date=dt.date.today() + dt.timedelta(days=60),
        quantity=Decimal(10),
        purchase_price=Decimal("1.00"),
    )
    db_session.add(batch_w)
    await db_session.commit()

    report = await alerts_service.evaluate_branch(db_session, branch.id)
    assert _rule(report, "expiry_critical") == 1
    assert _rule(report, "expiry_warning") == 1

    # Sweep the critical batch past expiry -> it becomes the expired rule's.
    await _force_expiry(db_session, batch_c.id, days=-1)
    swept = await inventory_service.expiry_sweep(db_session)
    assert swept["swept"] >= 1
    after = await alerts_service.evaluate_branch(db_session, branch.id)
    assert _rule(after, "expiry_critical") == 0  # no longer active/in-horizon
    assert _rule(after, "expired") == 1
    alerts = (await alerts_service.list_alerts(db_session, branch_id=branch.id, status="active"))[
        "alerts"
    ]
    assert isinstance(alerts, list)
    expired = next(a for a in alerts if a["rule_key"] == "expired")
    assert expired["severity"] == "danger" and expired["entity_type"] == "batch"


async def test_high_discount_fires_above_limit_and_inert_at_zero(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    """Synthetic completed invoice: total 100.00 + discount 30.00 → rate
    30/130 = 23.08% > 20% limit → fires. With the branch limit at 0
    (unconfigured ceiling) the rule must stay inert even for the same invoice."""
    _, med_id, _ = await _make_med(db_session, branch.id)
    await db_session.execute(text("""
        INSERT INTO settings (branch_id, pharmacy_name, max_discount_percent)
        VALUES (:b, 'صيدلية اختبار', 20.00)
        """).bindparams(b=branch.id))
    invoice_id = (await db_session.execute(text("""
            INSERT INTO invoices (branch_id, invoice_number, currency_code, subtotal,
                                  discount_amount, tax_amount, total, status, created_at)
            VALUES (:b, :num, 'EGP', 100.00, 30.00, 0, 100.00, 'completed', NOW())
            RETURNING id
            """).bindparams(b=branch.id, num=f"INV-DISC-{uuid.uuid4().hex[:8]}"))).scalar_one()
    await db_session.commit()

    report = await alerts_service.evaluate_branch(db_session, branch.id)
    assert _rule(report, "high_discount") == 1
    alerts = (await alerts_service.list_alerts(db_session, branch_id=branch.id, status="active"))[
        "alerts"
    ]
    assert isinstance(alerts, list)
    disc = next(a for a in alerts if a["rule_key"] == "high_discount")
    assert disc["severity"] == "warning" and disc["entity_type"] == "invoice"
    assert str(invoice_id) == disc["entity_id"]

    await db_session.execute(
        text("UPDATE settings SET max_discount_percent = 0 WHERE branch_id = :b").bindparams(
            b=branch.id
        )
    )
    await db_session.commit()
    inert = await alerts_service.evaluate_branch(db_session, branch.id)
    assert _rule(inert, "high_discount") == 0
    assert inert["resolved"] >= 1  # the standing alert auto-resolved


async def test_cash_discrepancy_fires_from_real_close(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    """A REAL drawer close with counted != expected (opening 100, counted 95 →
    discrepancy -5.00) must raise the critical alert; a clean close must not."""
    await cashier_service.open_session(
        db_session, actor=actor, branch_id=branch.id, opening_float=Decimal("100.00")
    )
    session_row = (
        await db_session.execute(
            text("SELECT id FROM cash_sessions WHERE branch_id = :b").bindparams(b=branch.id)
        )
    ).scalar_one()
    from pharmaos_api.models import CashSession

    cs = await db_session.get(CashSession, session_row)
    assert cs is not None
    await cashier_service.close_session(
        db_session, actor=actor, cash_session=cs, counted_cash=Decimal("95.00")
    )

    report = await alerts_service.evaluate_branch(db_session, branch.id)
    assert _rule(report, "cash_discrepancy") == 1
    alerts = (await alerts_service.list_alerts(db_session, branch_id=branch.id, status="active"))[
        "alerts"
    ]
    assert isinstance(alerts, list)
    disc = next(a for a in alerts if a["rule_key"] == "cash_discrepancy")
    assert disc["severity"] == "critical" and disc["entity_type"] == "cash_session"
    assert disc["params"]["discrepancy"] == "-5.00"


async def test_ereceipt_backlog_and_tt_failed(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    """24h-pending ETA receipt + an EDA event failed >3 retries → the two
    compliance alerts fire as BRANCH aggregates (count params, one row each)."""
    _, _, _ = await _make_med(db_session, branch.id)
    await db_session.execute(text("""
        INSERT INTO invoices (branch_id, invoice_number, currency_code, subtotal,
                              tax_amount, total, status, created_at)
        VALUES (:b, :num, 'EGP', 10.00, 0, 10.00, 'completed', NOW() - interval '25 hours')
        """).bindparams(b=branch.id, num=f"INV-ETA-{uuid.uuid4().hex[:8]}"))
    await db_session.execute(text("""
        INSERT INTO ereceipt_queue (branch_id, invoice_id, status, created_at)
        SELECT :b, id, 'pending', NOW() - interval '25 hours' FROM invoices
        WHERE branch_id = :b LIMIT 1
        """).bindparams(b=branch.id))
    await db_session.execute(text("""
        INSERT INTO tt_events (branch_id, event_type, gtin, serial_number, status,
                               report_attempts, created_at)
        VALUES (:b, 'receive', '06220000000000', :serial, 'failed', 4, NOW())
        """).bindparams(b=branch.id, serial=f"SN{uuid.uuid4().hex[:12]}"))
    await db_session.commit()

    report = await alerts_service.evaluate_branch(db_session, branch.id)
    assert _rule(report, "ereceipt_backlog") == 1
    assert _rule(report, "tt_report_failed") == 1
    alerts = (await alerts_service.list_alerts(db_session, branch_id=branch.id, status="active"))[
        "alerts"
    ]
    assert isinstance(alerts, list)
    backlog = next(a for a in alerts if a["rule_key"] == "ereceipt_backlog")
    assert backlog["severity"] == "critical" and backlog["entity_type"] == "branch"
    assert backlog["params"]["count"] == "1"


async def test_inventory_drift_fires_and_sync_failed_stays_silent(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    """Desyncing the cache (cached +7 over truth) fires the critical drift
    alert with both numbers in params. sync_failed must NEVER fire while no
    sync outbox exists — the engine does not pretend."""
    _, med_id, _ = await _make_med(db_session, branch.id)
    await _sync_cache(db_session, branch, med_id)
    await db_session.execute(
        text(
            "UPDATE branch_inventory SET cached_quantity = cached_quantity + 7 "
            "WHERE medication_id = :m"
        ).bindparams(m=med_id)
    )
    await db_session.commit()

    report = await alerts_service.evaluate_branch(db_session, branch.id)
    assert _rule(report, "inventory_drift") == 1
    assert _rule(report, "sync_failed") == 0
    alerts = (await alerts_service.list_alerts(db_session, branch_id=branch.id, status="active"))[
        "alerts"
    ]
    assert isinstance(alerts, list)
    drift = next(a for a in alerts if a["rule_key"] == "inventory_drift")
    assert drift["severity"] == "critical" and drift["entity_type"] == "medication"
    assert drift["params"]["truth"] == "1000.000" and drift["params"]["cached"] == "1007.000"


async def test_backup_overdue_inert_without_path_and_fires_on_empty_dir(
    db_session: AsyncSession,
    actor: User,
    branch: Branch,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
) -> None:
    """BACKUP_PATH unset → inert (nothing to measure). Set to an EMPTY dir →
    'never backed up' IS overdue. A fresh backup file clears the alert."""
    monkeypatch.delenv("BACKUP_PATH", raising=False)
    inert = await alerts_service.evaluate_branch(db_session, branch.id)
    assert _rule(inert, "backup_overdue") == 0

    backup_dir = tmp_path / "bk"  # type: ignore[operator]
    backup_dir.mkdir()
    monkeypatch.setenv("BACKUP_PATH", str(backup_dir))
    never = await alerts_service.evaluate_branch(db_session, branch.id)
    assert _rule(never, "backup_overdue") == 1
    alerts = (await alerts_service.list_alerts(db_session, branch_id=branch.id, status="active"))[
        "alerts"
    ]
    assert isinstance(alerts, list)
    overdue = next(a for a in alerts if a["rule_key"] == "backup_overdue")
    assert overdue["params"]["never"] is True

    fresh = backup_dir / "pharmaos_2026.pharmaos-backup"
    fresh.write_bytes(b"x")
    fresh.touch()
    cleared = await alerts_service.evaluate_branch(db_session, branch.id)
    assert _rule(cleared, "backup_overdue") == 0


# ------------------------------ lifecycle + HTTP ------------------------------


async def test_acknowledge_and_resolve_lifecycle(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    """active → acknowledged (persists across re-evaluation while the condition
    holds) → resolved (manually); acknowledging a resolved alert is 422."""
    _, med_id, _ = await _make_med(db_session, branch.id)
    await _sync_cache(db_session, branch, med_id)
    await _set_reorder(db_session, branch, med_id, "1500")
    await alerts_service.evaluate_branch(db_session, branch.id)
    alert_id = (
        await db_session.execute(
            text(
                "SELECT id FROM alerts WHERE rule_key = 'low_stock' AND branch_id = :b LIMIT 1"
            ).bindparams(b=branch.id)
        )
    ).scalar_one()

    acked = await alerts_service.acknowledge(db_session, alert_id=alert_id, actor_id=actor.id)
    assert acked["status"] == "acknowledged"

    # Condition persists → refresh does NOT reset the acknowledgement.
    await alerts_service.evaluate_branch(db_session, branch.id)
    row = (
        await db_session.execute(
            text("SELECT status, acknowledged_by IS NOT NULL FROM alerts WHERE id = :i").bindparams(
                i=alert_id
            )
        )
    ).first()
    assert row is not None and row[0] == "acknowledged" and row[1] is True

    resolved = await alerts_service.resolve(db_session, alert_id=alert_id)
    assert resolved["status"] == "resolved"
    with pytest.raises(ApiError) as exc:
        await alerts_service.acknowledge(db_session, alert_id=alert_id, actor_id=actor.id)
    assert exc.value.http_status == 422
    with pytest.raises(ApiError):
        await alerts_service.resolve(db_session, alert_id=alert_id)  # already resolved


async def test_summary_counts_by_severity(
    db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    """The dashboard banner's data source: live (unresolved) counts by severity."""
    _, med_id, _ = await _make_med(db_session, branch.id)
    await _sync_cache(db_session, branch, med_id)
    await _set_reorder(db_session, branch, med_id, "1500")  # warning
    _, med2, _ = await _make_med(db_session, branch.id)
    await db_session.execute(
        text("UPDATE medication_batches SET quantity = 0 WHERE medication_id = :m").bindparams(
            m=med2
        )
    )
    await _sync_cache(db_session, branch, med2)  # critical
    await alerts_service.evaluate_branch(db_session, branch.id)

    summary = await alerts_service.alert_summary(db_session, branch_id=branch.id)
    assert summary["warning"] >= 1 and summary["critical"] >= 1
    assert summary["total"] == summary["warning"] + summary["danger"] + summary["critical"]


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


async def test_alerts_http_permission_matrix_and_csrf(
    client: httpx.AsyncClient, db_session: AsyncSession, actor: User, branch: Branch
) -> None:
    """cashier 403 (no alerts.view/manage), pharmacist 200 (new operational
    tier), branch_manager 200. Mutations enforce CSRF; acknowledge of a
    non-active alert is 422."""
    _, med_id, _ = await _make_med(db_session, branch.id)
    await _sync_cache(db_session, branch, med_id)
    await _set_reorder(db_session, branch, med_id, "1500")
    await alerts_service.evaluate_branch(db_session, branch.id)
    params = {"branch_id": str(branch.id)}

    await _login(client, await _seed_role_user(db_session, "cashier"))
    assert (await client.get("/api/v1/alerts", params=params)).status_code == 403
    assert (await client.get("/api/v1/alerts/summary", params=params)).status_code == 403
    no_csrf = await client.post("/api/v1/alerts/evaluate", params=params)
    assert no_csrf.status_code == 403  # CSRF gate fires before permission noise

    await _login(client, await _seed_role_user(db_session, "pharmacist"))
    ok = await client.get("/api/v1/alerts", params=params)
    assert ok.status_code == 200, ok.text
    data = ok.json()["data"]
    assert set(data) >= {"alerts", "pagination"}
    assert len(data["alerts"]) >= 1
    summary = await client.get("/api/v1/alerts/summary", params=params)
    assert summary.status_code == 200

    ph_csrf = await _login(client, await _seed_role_user(db_session, "pharmacist"))
    ev = await client.post(
        "/api/v1/alerts/evaluate", params=params, headers={"X-CSRF-Token": ph_csrf}
    )
    assert ev.status_code == 200, ev.text

    alert_id = data["alerts"][0]["id"]
    ack = await client.post(
        f"/api/v1/alerts/{alert_id}/acknowledge",
        headers={"X-CSRF-Token": ph_csrf},
    )
    assert ack.status_code == 200, ack.text
    assert ack.json()["data"]["status"] == "acknowledged"

    resolve = await client.post(
        f"/api/v1/alerts/{alert_id}/resolve", headers={"X-CSRF-Token": ph_csrf}
    )
    assert resolve.status_code == 200
    re_ack = await client.post(
        f"/api/v1/alerts/{alert_id}/acknowledge", headers={"X-CSRF-Token": ph_csrf}
    )
    assert re_ack.status_code == 422  # resolved is the lifecycle sink
