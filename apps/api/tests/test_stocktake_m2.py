"""Stocktake module (M2 of the stocktake/shortages round): session lifecycle,
batch-level counting, blind masking, one-transaction approval, controlled
register, permissions, and the stocktake_overdue alert.

Concurrency tests run REAL pooled connections (no mocks) per the owner's
contract: the approval-vs-attacker deadlock choreography, and a retry-path
test that injects a 40P01 OperationalError once to prove the rollback+rerun
wiring (the real deadlock itself is non-deterministic in which side PG
picks as victim, so both angles are covered).
"""

import asyncio
import datetime as dt
import uuid
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.models import (
    Branch,
    Medication,
    MedicationBatch,
    Role,
    StocktakeLine,
    StocktakeSession,
    User,
)
from pharmaos_api.security.passwords import hash_password

FUTURE = (dt.date.today() + dt.timedelta(days=365)).isoformat()
ST = "/api/v1/stocktake"


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


@pytest.fixture
async def admin(client: httpx.AsyncClient, db_session: AsyncSession):
    csrf = await _login(client, await _seed_user(db_session, "super_admin"))
    return client, csrf


@pytest.fixture
async def branch(db_session: AsyncSession) -> Branch:
    b = Branch(name=f"فرع {uuid.uuid4().hex[:6]}", country_code="EG", currency_code="EGP")
    db_session.add(b)
    await db_session.commit()
    return b


async def _actor(db_session: AsyncSession) -> User:
    username = await _seed_user(db_session, "super_admin")
    return (await db_session.execute(select(User).where(User.username == username))).scalar_one()


async def _receive_batch(
    db_session: AsyncSession,
    branch: Branch,
    med: Medication,
    qty: str,
    *,
    shelf: str | None = None,
    batch_number: str | None = None,
) -> MedicationBatch:
    """Receive through the real primitive (cache-consistent) and set the shelf."""
    from pharmaos_api.services import inventory_service

    actor = await _actor(db_session)
    batch = await inventory_service.receive_batch(
        db_session,
        actor=actor,
        branch_id=branch.id,
        medication_id=med.id,
        batch_number=batch_number or f"B-{uuid.uuid4().hex[:6]}",
        expiry_date=dt.date.today() + dt.timedelta(days=300),
        quantity=Decimal(qty),
        purchase_price=Decimal("2.00"),
    )
    if shelf:
        await db_session.execute(
            text(
                "UPDATE branch_inventory SET shelf_location = :s "
                "WHERE branch_id = :b AND medication_id = :m"
            ).bindparams(s=shelf, b=branch.id, m=med.id)
        )
    await db_session.commit()
    return batch


async def _make_med(db_session: AsyncSession, *, controlled: bool = False, name: str) -> Medication:
    med = Medication(
        trade_name=f"{name} {uuid.uuid4().hex[:6]}",
        trade_name_ar=f"دواء {name}",
        controlled_substance=controlled,
    )
    db_session.add(med)
    await db_session.commit()
    return med


async def _create_session(client, csrf, branch_id, *, st="daily_quick", blind=None, extra=None):
    body = {"branch_id": branch_id, "session_type": st, **(extra or {})}
    if blind is not None:
        body["blind"] = blind
    r = await client.post(f"{ST}/sessions", headers={"X-CSRF-Token": csrf}, json=body)
    assert r.status_code == 200, r.text
    return r.json()["data"]


async def _lines(client, session_id, branch_id, **params):
    r = await client.get(
        f"{ST}/sessions/{session_id}/lines",
        params={"branch_id": branch_id, **params},
    )
    assert r.status_code == 200, r.text
    return r.json()["data"]["items"]


async def _new_client():
    """Each user needs its OWN cookie jar — one shared client would keep only
    the last-logged-in session (CSRF is a double-submit against that cookie)."""
    import httpx

    from pharmaos_api.main import create_app

    transport = httpx.ASGITransport(app=create_app(license_scheduler=False))
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


async def _count(client, csrf, line_id, qty, **extra):
    return await client.post(
        f"{ST}/lines/{line_id}/count",
        headers={"X-CSRF-Token": csrf},
        json={"counted_qty": qty, **extra},
    )


# ------------------------------ lifecycle ------------------------------


async def test_lifecycle_count_submit_approve(admin, branch, db_session: AsyncSession):
    """draft -> counting (first count) -> review -> approved; the applied diff
    writes one 'stocktake' movement, updates the cache, keeps drift clean."""
    client, csrf = admin
    med = await _make_med(db_session, name="Lifecycle")
    batch = await _receive_batch(db_session, branch, med, "50", shelf="A1")
    bid = str(branch.id)

    sess = await _create_session(client, csrf, bid)
    assert sess["status"] == "draft"
    assert sess["lines_expected"] >= 1

    lines = await _lines(client, sess["id"], bid)
    mine = next(ln for ln in lines if ln["batch_id"] == str(batch.id))

    # blind by default: no system quantity before review
    assert mine["system_qty_at_count"] is None and mine["diff"] is None

    counted = await _count(client, csrf, mine["id"], "45", reason_code="miscount")
    assert counted.status_code == 200, counted.text
    body = counted.json()["data"]
    assert body["counted_qty"] == "45.000"
    assert body["system_qty_at_count"] is None, "count response stays blind until review"

    detail_r = await client.get(f"{ST}/sessions/{sess['id']}", params={"branch_id": bid})
    assert detail_r.json()["data"]["status"] == "counting"

    sub = await client.post(
        f"{ST}/sessions/{sess['id']}/submit",
        params={"branch_id": bid},
        headers={"X-CSRF-Token": csrf},
    )
    assert sub.status_code == 200, sub.text

    # review reveals the snapshot and the diff
    lines = await _lines(client, sess["id"], bid)
    mine = next(ln for ln in lines if ln["batch_id"] == str(batch.id))
    assert mine["system_qty_at_count"] == "50.000" and mine["diff"] == "-5.000"

    appr = await client.post(
        f"{ST}/sessions/{sess['id']}/approve",
        params={"branch_id": bid},
        headers={"X-CSRF-Token": csrf},
        json={"reason": "اعتماد الجرد اليومي"},
    )
    assert appr.status_code == 200, appr.text
    data = appr.json()["data"]
    assert data["status"] == "approved" and data["applied"] == 1

    fresh = (
        await db_session.execute(select(MedicationBatch).where(MedicationBatch.id == batch.id))
    ).scalar_one()
    await db_session.refresh(fresh)
    assert fresh.quantity == 45

    mv = (await db_session.execute(text("""
                SELECT quantity_delta, reference_type, reference_id FROM stock_movements
                WHERE batch_id = :b AND movement_type = 'adjustment'
                  AND reference_type = 'stocktake'
                """).bindparams(b=batch.id))).all()
    assert len(mv) == 1 and str(mv[0][2]) == sess["id"]

    drift = await client.get("/api/v1/inventory/drift", params={"branch_id": bid})
    assert drift.json()["data"]["ok"] is True

    audit = (await db_session.execute(text("""
                SELECT COUNT(*) FROM audit_logs
                WHERE action = 'stocktake.approved' AND entity_id = :e
                """).bindparams(e=uuid.UUID(sess["id"])))).scalar_one()
    assert audit == 1


async def test_zero_diff_skips_and_export_csv_masks_blind(admin, branch, db_session: AsyncSession):
    client, csrf = admin
    med = await _make_med(db_session, name="ZeroDiff")
    batch = await _receive_batch(db_session, branch, med, "10")
    bid = str(branch.id)
    sess = await _create_session(client, csrf, bid, blind=False)
    lines = await _lines(client, sess["id"], bid)
    mine = next(ln for ln in lines if ln["batch_id"] == str(batch.id))
    # blind=False: the CURRENT batch quantity is visible before counting
    # (the snapshot itself only exists once a count is recorded)
    assert mine["system_qty_current"] == "10.000"
    assert mine["system_qty_at_count"] is None

    same = await _count(client, csrf, mine["id"], "10")
    assert same.status_code == 200

    # CSV while counting stays masked (blind switch is per-session status too)
    csv_r = await client.get(f"{ST}/sessions/{sess['id']}/export.csv", params={"branch_id": bid})
    assert csv_r.status_code == 200
    assert "10.000" in csv_r.text, "blind=False reveals in CSV as well"

    await client.post(
        f"{ST}/sessions/{sess['id']}/submit",
        params={"branch_id": bid},
        headers={"X-CSRF-Token": csrf},
    )
    appr = await client.post(
        f"{ST}/sessions/{sess['id']}/approve",
        params={"branch_id": bid},
        headers={"X-CSRF-Token": csrf},
        json={"reason": "لا فروق"},
    )
    data = appr.json()["data"]
    assert data["zero_diff_skipped"] == 1 and data["applied"] == 0


async def test_csv_export_escapes_formula_injection(admin, branch, db_session: AsyncSession):
    client, csrf = admin
    med = Medication(
        trade_name=f"=1+1cmd|{uuid.uuid4().hex[:6]}",  # formula-leading trade name
        trade_name_ar="@sum|خطر",  # @-leading Arabic name
    )
    db_session.add(med)
    await db_session.commit()
    await _receive_batch(db_session, branch, med, "5")
    bid = str(branch.id)
    sess = await _create_session(client, csrf, bid)
    csv_r = await client.get(f"{ST}/sessions/{sess['id']}/export.csv", params={"branch_id": bid})
    assert csv_r.status_code == 200
    # every dangerous leading character is quote-prefixed (csv_safe)
    for line in csv_r.text.splitlines():
        for cell in line.split(","):
            assert not cell.startswith(("=", "+", "-", "@")), f"unescaped cell: {cell!r}"


# ------------------------------ needs_recount ------------------------------


async def _sell(db_session: AsyncSession, branch, med, batch, delta: str) -> None:
    """Move a batch as a sale would (quantity + cache + movement) so the
    live-vs-snapshot staleness check has something real to catch."""
    from pharmaos_api.services.inventory_service import apply_cache_delta

    actor = await _actor(db_session)
    batch.quantity = batch.quantity + Decimal(delta)
    batch.updated_by = actor.id
    await apply_cache_delta(db_session, branch.id, med.id, Decimal(delta))
    await db_session.commit()


async def test_sale_after_count_rejects_approval_flags_survive(
    admin, branch, db_session: AsyncSession
):
    """A sale between count and approval moves the live batch against the diff:
    approval now REFUSES with E-STK-005 (owner decision 1) — the session stays
    `review`, NOTHING settles (no partial movements), and the needs_recount
    flags SURVIVE the rejection (committed separately, not rolled back)."""
    client, csrf = admin
    med_a = await _make_med(db_session, name="StaleA")
    med_b = await _make_med(db_session, name="StaleB")
    batch_a = await _receive_batch(db_session, branch, med_a, "50")
    batch_b = await _receive_batch(db_session, branch, med_b, "30")
    bid = str(branch.id)

    sess = await _create_session(client, csrf, bid)
    lines = await _lines(client, sess["id"], bid)
    line_a = next(ln for ln in lines if ln["batch_id"] == str(batch_a.id))
    line_b = next(ln for ln in lines if ln["batch_id"] != str(batch_a.id))

    r1 = await _count(client, csrf, line_a["id"], "45")  # diff -5 (snapshot 50)
    r2 = await _count(client, csrf, line_b["id"], "35")  # diff +5
    assert r1.status_code == r2.status_code == 200

    await client.post(
        f"{ST}/sessions/{sess['id']}/submit",
        params={"branch_id": bid},
        headers={"X-CSRF-Token": csrf},
    )

    # SELL batch_a down to 2 after the count: 2 + (-5) < 0 → stale
    await _sell(db_session, branch, med_a, batch_a, "-48")

    appr = await client.post(
        f"{ST}/sessions/{sess['id']}/approve",
        params={"branch_id": bid},
        headers={"X-CSRF-Token": csrf},
        json={"reason": "اعتماد مع سطر قديم"},
    )
    assert appr.status_code == 422, appr.text
    err = appr.json()["error"]
    assert err["code"] == "E-STK-005"
    assert [ln["line_id"] for ln in err["details"]["flagged_lines"]] == [line_a["id"]]

    # the session did NOT approve, and NOTHING moved (all-or-nothing)
    session_row = (
        await db_session.execute(
            select(StocktakeSession).where(StocktakeSession.id == uuid.UUID(sess["id"]))
        )
    ).scalar_one()
    await db_session.refresh(session_row)
    assert session_row.status == "review", "the refusal keeps the session open"
    flagged = (
        await db_session.execute(
            select(StocktakeLine).where(StocktakeLine.id == uuid.UUID(line_a["id"]))
        )
    ).scalar_one()
    await db_session.refresh(flagged)
    assert flagged.flag == "needs_recount", "flags survive the rejected approval"
    assert flagged.adjustment_movement_id is None
    healthy = (
        await db_session.execute(
            select(StocktakeLine).where(StocktakeLine.id == uuid.UUID(line_b["id"]))
        )
    ).scalar_one()
    await db_session.refresh(healthy)
    assert healthy.adjustment_movement_id is None, "no partial settlement"
    await db_session.refresh(batch_b)
    assert batch_b.quantity == Decimal("30"), "healthy batch untouched by the refusal"

    drift = await client.get("/api/v1/inventory/drift", params={"branch_id": bid})
    assert drift.json()["data"]["ok"] is True


async def test_recount_flagged_line_then_approval_settles(admin, branch, db_session: AsyncSession):
    """The full recovery flow the refusal demands: recount the flagged line
    (fresh snapshot under lock), then approve — the real variance settles and
    the flag clears (the previously-missing settlement test)."""
    client, csrf = admin
    med_a = await _make_med(db_session, name="RecountA")
    batch_a = await _receive_batch(db_session, branch, med_a, "50")
    bid = str(branch.id)

    sess = await _create_session(client, csrf, bid)
    lines = await _lines(client, sess["id"], bid)
    line_a = next(ln for ln in lines if ln["batch_id"] == str(batch_a.id))

    await _count(client, csrf, line_a["id"], "45")  # diff -5 (snapshot 50)
    await client.post(
        f"{ST}/sessions/{sess['id']}/submit",
        params={"branch_id": bid},
        headers={"X-CSRF-Token": csrf},
    )
    await _sell(db_session, branch, med_a, batch_a, "-48")  # live 2: 2 + (-5) < 0

    appr1 = await client.post(
        f"{ST}/sessions/{sess['id']}/approve",
        params={"branch_id": bid},
        headers={"X-CSRF-Token": csrf},
        json={"reason": "سطر قديم"},
    )
    assert appr1.status_code == 422 and appr1.json()["error"]["code"] == "E-STK-005"

    # recount AFTER the refusal — record_count takes a fresh snapshot (2)
    r = await _count(client, csrf, line_a["id"], "1")  # true variance: 1 missing
    assert r.status_code == 200, r.text

    appr2 = await client.post(
        f"{ST}/sessions/{sess['id']}/approve",
        params={"branch_id": bid},
        headers={"X-CSRF-Token": csrf},
        json={"reason": "تسوية بعد إعادة العد"},
    )
    assert appr2.status_code == 200, appr2.text
    data = appr2.json()["data"]
    assert data["applied"] == 1

    await db_session.refresh(batch_a)
    assert batch_a.quantity == Decimal("1"), "the recounted diff settled for real"
    flagged = (
        await db_session.execute(
            select(StocktakeLine).where(StocktakeLine.id == uuid.UUID(line_a["id"]))
        )
    ).scalar_one()
    await db_session.refresh(flagged)
    assert flagged.flag is None and flagged.adjustment_movement_id is not None

    drift = await client.get("/api/v1/inventory/drift", params={"branch_id": bid})
    assert drift.json()["data"]["ok"] is True


# ------------------------------ controlled ------------------------------


async def test_controlled_line_cashier_rejected_pharmacist_ok_and_register_row(
    client: httpx.AsyncClient, branch, db_session: AsyncSession
):
    """Cashier cannot count a controlled line (pharmacist+ only); a pharmacist
    count + manager approval writes controlled_substance_log(source='stocktake')."""
    admin_client = client
    admin_csrf = await _login(admin_client, await _seed_user(db_session, "super_admin"))
    async with await _new_client() as cashier_client:
        cashier_csrf = await _login(cashier_client, await _seed_user(db_session, "cashier"))
        async with await _new_client() as pharmacist_client:
            pharmacist_csrf = await _login(
                pharmacist_client, await _seed_user(db_session, "pharmacist")
            )

            med = await _make_med(db_session, controlled=True, name="Controlled")
            await _receive_batch(db_session, branch, med, "20")
            bid = str(branch.id)

            sess = await _create_session(admin_client, admin_csrf, bid)
            lines = await _lines(admin_client, sess["id"], bid)
            line = next(ln for ln in lines if ln["controlled_substance"])

            as_cashier = await _count(cashier_client, cashier_csrf, line["id"], "20")
            assert as_cashier.status_code == 403
            assert as_cashier.json()["error"]["code"] == "E-AUTH-002"

            as_pharmacist = await _count(pharmacist_client, pharmacist_csrf, line["id"], "18")
            assert as_pharmacist.status_code == 200, as_pharmacist.text

            await admin_client.post(
                f"{ST}/sessions/{sess['id']}/submit",
                params={"branch_id": bid},
                headers={"X-CSRF-Token": admin_csrf},
            )
            appr = await admin_client.post(
                f"{ST}/sessions/{sess['id']}/approve",
                params={"branch_id": bid},
                headers={"X-CSRF-Token": admin_csrf},
                json={"reason": "فرق مخدرات بسبب موثق"},
            )
    assert appr.status_code == 200, appr.text
    assert appr.json()["data"]["controlled_rows"] == 1

    reg = (await db_session.execute(text("""
                SELECT source, quantity_delta, stocktake_session_id, invoice_id
                FROM controlled_substance_log WHERE branch_id = :b
                """).bindparams(b=branch.id))).all()
    assert len(reg) == 1
    assert reg[0][0] == "stocktake" and str(reg[0][1]) in ("-2.000", "-2")
    assert str(reg[0][2]) == sess["id"] and reg[0][3] is None


async def test_cashier_counts_normal_line_but_cannot_approve_or_cancel(
    client: httpx.AsyncClient, branch, db_session: AsyncSession
):
    admin_client = client
    admin_csrf = await _login(admin_client, await _seed_user(db_session, "super_admin"))
    async with await _new_client() as cashier_client:
        cashier_csrf = await _login(cashier_client, await _seed_user(db_session, "cashier"))
        med = await _make_med(db_session, name="CashierOk")
        await _receive_batch(db_session, branch, med, "10")
        bid = str(branch.id)

        sess = await _create_session(cashier_client, cashier_csrf, bid)  # create = inventory.count
        lines = await _lines(cashier_client, sess["id"], bid)
        ok = await _count(cashier_client, cashier_csrf, lines[0]["id"], "10")
        assert ok.status_code == 200

        sub = await cashier_client.post(
            f"{ST}/sessions/{sess['id']}/submit",
            params={"branch_id": bid},
            headers={"X-CSRF-Token": cashier_csrf},
        )
        assert sub.status_code == 200

        denied = await cashier_client.post(
            f"{ST}/sessions/{sess['id']}/approve",
            params={"branch_id": bid},
            headers={"X-CSRF-Token": cashier_csrf},
            json={"reason": "محاولة كاشير"},
        )
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "E-AUTH-002"

        cancelled = await admin_client.post(
            f"{ST}/sessions/{sess['id']}/cancel",
            params={"branch_id": bid},
            headers={"X-CSRF-Token": admin_csrf},
            json={"reason": "إلغاء تجريبي"},
        )
        assert cancelled.status_code == 200


async def test_count_needs_csrf_and_rejects_bad_input(admin, branch, db_session: AsyncSession):
    client, csrf = admin
    med = await _make_med(db_session, name="Guard")
    await _receive_batch(db_session, branch, med, "10")
    bid = str(branch.id)
    sess = await _create_session(client, csrf, bid)
    lines = await _lines(client, sess["id"], bid)

    no_csrf = await client.post(f"{ST}/lines/{lines[0]['id']}/count", json={"counted_qty": "1"})
    assert no_csrf.status_code == 403 and no_csrf.json()["error"]["code"] == "E-AUTH-004"

    negative = await _count(client, csrf, lines[0]["id"], "-3")
    assert negative.status_code == 422

    over_precision = await _count(client, csrf, lines[0]["id"], "1.2345")
    assert over_precision.status_code == 422

    bad_reason = await _count(client, csrf, lines[0]["id"], "5", reason_code="aliens")
    assert bad_reason.status_code == 422


# ------------------------------ deadlock (real) ------------------------------


async def test_approval_vs_opposite_order_locks_no_hang(admin, branch, db_session: AsyncSession):
    """Two REAL connections: an attacker transaction locks the batches in the
    OPPOSITE of the approval's collective order, then asks for the batch the
    approval holds — a genuine 40P01 cycle on PostgreSQL. Whichever side PG
    picks as victim, the approval must complete (via its single retry when it
    is the victim) and the ledger must stay consistent."""
    from sqlalchemy.ext.asyncio import AsyncSession as SessionAlias

    from pharmaos_api.db import get_engine
    from pharmaos_api.services import stocktake_service

    client, csrf = admin
    m1 = await _make_med(db_session, name="DL-A")
    m2 = await _make_med(db_session, name="DL-B")
    b1 = await _receive_batch(db_session, branch, m1, "50")
    b2 = await _receive_batch(db_session, branch, m2, "50")
    bid = str(branch.id)

    # approval order is (medication_id, expiry, id) — force m2 first in that
    # order by giving m2 a SMALLER uuid than m1 via explicit re-insert order:
    # simpler: order batches for the attacker as the REVERSE of the ids list
    # the approval will use; compute it from the service ordering.
    ordered = sorted([b1.id, b2.id], key=lambda i: (m1.id if i == b1.id else m2.id, i))
    first_locked, second_locked = ordered[0], ordered[1]

    sess = await _create_session(client, csrf, bid)
    lines = await _lines(client, sess["id"], bid)
    for ln in lines:
        r = await _count(client, csrf, ln["id"], "40")
        assert r.status_code == 200
    await client.post(
        f"{ST}/sessions/{sess['id']}/submit",
        params={"branch_id": bid},
        headers={"X-CSRF-Token": csrf},
    )

    attacker = SessionAlias(get_engine())
    approval_task: asyncio.Task | None = None
    try:
        # DETERMINISTIC victim selection: PG aborts whichever waiter's
        # deadlock_timeout fires first. Give the attacker a 100ms detector so
        # IT is always the victim — under load the default 1s could otherwise
        # fire on the approval first, twice, exhausting the single retry.
        await attacker.execute(text("SET deadlock_timeout = '100ms'"))
        # attacker takes the SECOND batch the approval will want, in its own
        # uncommitted transaction (simulating a concurrent writer mid-flight)
        await attacker.execute(
            text("SELECT id FROM medication_batches WHERE id = :i FOR UPDATE").bindparams(
                i=second_locked
            )
        )
        await attacker.execute(
            text("UPDATE medication_batches SET updated_at = NOW() WHERE id = :i").bindparams(
                i=second_locked
            )
        )

        async def attacker_flow() -> None:
            import contextlib

            """A real concurrent writer: if it WINS the lock race it finishes
            its work and releases (commit); if it dies as the deadlock victim
            its locks vanish with the abort — either way the approval's retry
            must complete."""
            try:
                await attacker.execute(
                    text("SELECT id FROM medication_batches WHERE id = :i FOR UPDATE").bindparams(
                        i=first_locked
                    )
                )
                await attacker.execute(
                    text(
                        "UPDATE medication_batches SET updated_at = NOW() WHERE id = :i"
                    ).bindparams(i=first_locked)
                )
                await attacker.commit()  # release — the writer finished its work
            except Exception as e:  # noqa: BLE001 — victim of the 40P01 cycle
                assert "deadlock" in str(e).lower(), f"unexpected attacker error: {e}"
                with contextlib.suppress(Exception):
                    await attacker.rollback()  # aborted tx cleanup must not re-raise

        async def run_approve() -> dict[str, object]:
            return await stocktake_service.approve(
                db_session,
                actor=await _actor(db_session),
                branch_id=branch.id,
                session_id=uuid.UUID(sess["id"]),
                reason="اعتماد مع تنافس أقفال",
            )

        approval_task = asyncio.create_task(run_approve())
        await asyncio.sleep(0.3)  # approval takes its collective lock, blocks on second
        grab_task = asyncio.create_task(attacker_flow())
        await asyncio.wait_for(grab_task, timeout=10)
        result = await asyncio.wait_for(approval_task, timeout=15)
        assert result["status"] == "approved"
    finally:
        if approval_task is not None and not approval_task.done():
            approval_task.cancel()
        await attacker.close()

    fresh = (
        await db_session.execute(
            select(StocktakeSession).where(StocktakeSession.id == uuid.UUID(sess["id"]))
        )
    ).scalar_one()
    await db_session.refresh(fresh)
    assert fresh.status == "approved"


async def test_approval_retries_once_on_deadlock(admin, branch, db_session, monkeypatch):
    """Retry wiring: first approval attempt raises a 40P01 OperationalError
    (as asyncpg surfaces through SQLAlchemy), the wrapper rolls back and the
    second attempt completes — exactly one retry, then E-STK-004."""
    from pharmaos_api.services import stocktake_service

    client, csrf = admin
    med = await _make_med(db_session, name="Retry")
    await _receive_batch(db_session, branch, med, "50")
    bid = str(branch.id)
    sess = await _create_session(client, csrf, bid)
    lines = await _lines(client, sess["id"], bid)
    await _count(client, csrf, lines[0]["id"], "45")
    await client.post(
        f"{ST}/sessions/{sess['id']}/submit",
        params={"branch_id": bid},
        headers={"X-CSRF-Token": csrf},
    )

    import asyncpg
    from sqlalchemy.exc import OperationalError

    calls = {"n": 0}
    real_once = stocktake_service._approve_once

    async def flaky_once(session, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            from sqlalchemy import text as _text

            await session.execute(_text("SELECT 1"))
            err = asyncpg.DeadlockDetectedError("deadlock detected")
            err.sqlstate = "40P01"  # what the server sets on a real 40P01
            raise OperationalError("SELECT ... FOR UPDATE", {}, err)
        return await real_once(session, *args, **kwargs)

    monkeypatch.setattr(stocktake_service, "_approve_once", flaky_once)
    # the wrapper runs each attempt on its OWN session: the poisoned first
    # attempt is discarded and the second one re-runs clean
    appr = await client.post(
        f"{ST}/sessions/{sess['id']}/approve",
        params={"branch_id": bid},
        headers={"X-CSRF-Token": csrf},
        json={"reason": "إعادة المحاولة بعد تعارض"},
    )
    assert appr.status_code == 200, appr.text
    assert calls["n"] == 2 and appr.json()["data"]["status"] == "approved"


async def test_approval_double_deadlock_surfaces_estk004(admin, branch, db_session, monkeypatch):
    from pharmaos_api.services import stocktake_service

    client, csrf = admin
    med = await _make_med(db_session, name="Retry2")
    await _receive_batch(db_session, branch, med, "50")
    bid = str(branch.id)
    sess = await _create_session(client, csrf, bid)
    lines = await _lines(client, sess["id"], bid)
    await _count(client, csrf, lines[0]["id"], "45")
    await client.post(
        f"{ST}/sessions/{sess['id']}/submit",
        params={"branch_id": bid},
        headers={"X-CSRF-Token": csrf},
    )

    import asyncpg
    from sqlalchemy.exc import OperationalError

    async def always_dead(*args, **kwargs):
        err = asyncpg.DeadlockDetectedError("deadlock detected")
        err.sqlstate = "40P01"  # what the server sets on a real 40P01
        raise OperationalError("SELECT ... FOR UPDATE", {}, err)

    monkeypatch.setattr(stocktake_service, "_approve_once", always_dead)
    # ApiError is raised INSIDE the app — over HTTP it surfaces as the
    # error envelope with the E-STK-004 code
    resp = await client.post(
        f"{ST}/sessions/{sess['id']}/approve",
        params={"branch_id": bid},
        headers={"X-CSRF-Token": csrf},
        json={"reason": "تعارض متكرر"},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "E-STK-004"


# ------------------------------ overdue alert ------------------------------


async def test_stocktake_overdue_grace_then_fires(admin, branch, db_session: AsyncSession):
    from pharmaos_api.services import alerts_service, stocktake_service

    actor = await _actor(db_session)
    med = await _make_med(db_session, name="Overdue")
    await _receive_batch(db_session, branch, med, "10")

    # a FRESH branch is inside the grace window: no alert
    report = await alerts_service.evaluate_branch(db_session, branch.id)
    findings = report["findings"]
    assert isinstance(findings, dict)
    assert findings.get("stocktake_overdue", 0) == 0

    # an approved session 8 days old (default window 7) → overdue fires
    sess = await stocktake_service.create_session(
        db_session, actor=actor, branch_id=branch.id, session_type="custom"
    )
    raw = (
        await db_session.execute(
            select(StocktakeSession).where(StocktakeSession.id == uuid.UUID(sess["id"]))
        )
    ).scalar_one()
    raw.status = "approved"
    raw.approved_by = actor.id
    raw.approved_at = dt.datetime.now(dt.UTC) - dt.timedelta(days=8)
    await db_session.commit()

    report = await alerts_service.evaluate_branch(db_session, branch.id)
    findings = report["findings"]
    assert isinstance(findings, dict)
    assert findings["stocktake_overdue"] == 1

    # a fresh approval clears it again
    raw.approved_at = dt.datetime.now(dt.UTC)
    await db_session.commit()
    report = await alerts_service.evaluate_branch(db_session, branch.id)
    findings = report["findings"]
    assert isinstance(findings, dict)
    assert findings.get("stocktake_overdue", 0) == 0
