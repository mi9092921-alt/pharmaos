"""P3-M8 hardening: the automated acceptance gates.

1) OWASP gate — deterministic, self-updating as routers/services change:
   * A03 Injection: no SQL is ever built by interpolating runtime VALUES.
     The scan flags any `text(...)` argument that is string-concatenated,
     %-formatted, `.format()`ed, or an f-string interpolating anything beyond
     the two documented-safe shapes: a plain local/module name that is NOT a
     function parameter (e.g. `where_sql`, `_LOW_THRESHOLD` — constant SQL
     fragments composed of literals; values themselves always travel via
     `.bindparams()`, the repo's `# noqa: S608` convention) and a constant-
     separator `.join()` over such a fragment list. A future
     `text(f"... {name} ...")` with `name` a handler/service parameter fails
     this test.
   * A01/A08: every mutation endpoint (POST/PUT/PATCH/DELETE) enforces CSRF.
     The only documented exceptions are the two PRE-AUTH auth endpoints
     (login/refresh — there is no authenticated session to abuse yet; login
     is additionally rate-limited 5/min/IP).
   * A05: security headers on success AND error responses; routing-level
     rejections (404/405) speak the unified error envelope (E-GEN-001) so the
     client's `t('errors.<code>')` path never meets a bare {"detail": ...};
     an unhandled exception returns the generic E-SYS-001 envelope and never
     the exception text (forbidden rule 6); no Swagger/ReDoc/OpenAPI surface
     is mounted.
   * A09: audit_logs immutability trigger exists at the DB level (append-only
     is enforced by PostgreSQL, not convention).
2) Performance acceptance guard — the Phase-3 criterion "daily report < 3s"
   holds end-to-end through the service layer at synthetic pilot scale
   (22k invoices / 44k items across a year, with a dense "today"), asserted
   with ~10–70x measured headroom so a shared/throttled CI runner cannot flake
   it (the wall-clock assert is safe exactly because the measured reality is
   tens of milliseconds; the STRUCTURAL guards in test_query_plans.py remain
   the primary regression fence).
3) Alerts-summary rollup (P3-M8 polish, closing the M6 deferral):
   GET /alerts/summary without branch_id aggregates EVERY branch's live
   alerts — the dashboard banner must not hide a second branch's emergency
   behind a first-branch-only query. Scoped summaries keep the M6 shape.
"""

import ast
import datetime as dt
import pathlib
import time
import uuid

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

import pharmaos_api
from pharmaos_api.main import create_app
from pharmaos_api.models import Branch, Role, User
from pharmaos_api.security.passwords import hash_password
from pharmaos_api.services import alerts_service, reporting_service

# ============================== OWASP gate ==============================


def _package_py_files() -> list[pathlib.Path]:
    pkg = pathlib.Path(pharmaos_api.__file__).resolve().parent
    return sorted(p for p in pkg.rglob("*.py") if "__pycache__" not in p.parts)


def _function_params(func: ast.AST) -> set[str]:
    names: set[str] = set()
    if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        a = func.args
        for arg in (*a.posonlyargs, *a.args, *a.kwonlyargs):
            names.add(arg.arg)
        if a.vararg:
            names.add(a.vararg.arg)
        if a.kwarg:
            names.add(a.kwarg.arg)
    return names


def _scan_sql_interpolation(
    node: ast.AST, params: set[str], path: str, violations: list[str]
) -> None:
    """Recurse with precise function-parameter scope: a Name interpolated into
    SQL is only suspect when it is a parameter of an enclosing function (i.e.
    caller-controlled). Module constants and literal-composed fragment lists
    (the documented `# noqa: S608` convention) pass; parameters fail."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        own = _function_params(node)
        for child in ast.iter_child_nodes(node):
            _scan_sql_interpolation(child, params | own, path, violations)
        return
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "text":
        for arg in node.args:
            if isinstance(arg, ast.BinOp):
                violations.append(f"{path}:{node.lineno} SQL via string concatenation")
                continue
            if (
                isinstance(arg, ast.Call)
                and isinstance(arg.func, ast.Attribute)
                and arg.func.attr == "format"
            ):
                violations.append(f"{path}:{node.lineno} SQL via .format()")
                continue
            if not isinstance(arg, ast.JoinedStr):
                continue
            for part in arg.values:
                if not isinstance(part, ast.FormattedValue):
                    continue
                expr = part.value
                ok = False
                if isinstance(expr, ast.Name):
                    ok = expr.id not in params
                elif (
                    isinstance(expr, ast.Call)
                    and isinstance(expr.func, ast.Attribute)
                    and expr.func.attr == "join"
                    and isinstance(expr.func.value, ast.Constant)
                    and expr.args
                    and isinstance(expr.args[0], ast.Name)
                    and expr.args[0].id not in params
                ):
                    ok = True  # " AND ".join(where) — constant fragment list
                if not ok:
                    violations.append(
                        f"{path}:{node.lineno} interpolates a runtime value "
                        f"into SQL text: {ast.dump(expr)[:80]}"
                    )
        return
    for child in ast.iter_child_nodes(node):
        _scan_sql_interpolation(child, params, path, violations)


def test_no_sql_is_built_by_interpolating_runtime_values() -> None:
    """A03 — every `text()` argument is a literal or the documented constant-
    fragment composition; runtime values reach SQL only as bound params."""
    violations: list[str] = []
    for path in _package_py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        _scan_sql_interpolation(tree, set(), path.name, violations)

    assert not violations, "SQL built from runtime values: " + "; ".join(violations)


def test_every_mutation_endpoint_enforces_csrf() -> None:
    """A01/A08 — no mutation endpoint ships without CSRF, including future
    ones: the scan reads the routers as written, not a hand-kept list."""
    routers_dir = pathlib.Path(pharmaos_api.__file__).resolve().parent / "routers"
    # Pre-auth endpoints: no session cookie exists yet for CSRF to protect;
    # login is additionally rate-limited (5/min/IP).
    exempt = {("auth", "/login"), ("auth", "/refresh")}
    missing: list[str] = []

    for path in sorted(routers_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not (
                    isinstance(dec, ast.Call)
                    and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr in {"post", "put", "patch", "delete"}
                ):
                    continue
                route = (
                    dec.args[0].value
                    if dec.args and isinstance(dec.args[0], ast.Constant)
                    else "<dynamic>"
                )
                if (path.stem, route) in exempt:
                    continue
                calls_csrf = any(
                    isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Name)
                    and n.func.id == "enforce_csrf"
                    for n in ast.walk(node)
                )
                if not calls_csrf:
                    missing.append(f"{path.stem}: {dec.func.attr.upper()} {route} ({node.name})")

    assert not missing, "mutation endpoints without enforce_csrf: " + "; ".join(missing)


async def test_security_headers_unified_envelope_and_no_internals_leak() -> None:
    """A05 — headers on success AND failure, unified envelope on routing
    rejections, generic 500 (no exception text), no OpenAPI surface."""
    app = create_app()

    @app.get("/api/v1/_m8_boom")
    async def boom() -> None:
        raise RuntimeError("boom-secret-internals-xyz")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        ok = await c.get("/api/v1/health")
        assert ok.status_code == 200
        assert ok.headers.get("x-content-type-options") == "nosniff"
        assert ok.headers.get("x-frame-options") == "DENY"
        assert "default-src 'self'" in (ok.headers.get("content-security-policy") or "")

        missing = await c.get("/api/v1/definitely-not-a-route")
        assert missing.status_code == 404
        body = missing.json()
        assert body["success"] is False
        assert body["error"]["code"] == "E-GEN-001"
        assert missing.headers.get("x-frame-options") == "DENY"

        wrong_method = await c.post("/api/v1/health")
        assert wrong_method.status_code == 405
        assert wrong_method.json()["error"]["code"] == "E-GEN-001"

        crash = await c.get("/api/v1/_m8_boom")
        assert crash.status_code == 500
        assert crash.json()["error"]["code"] == "E-SYS-001"
        assert "boom-secret-internals-xyz" not in crash.text
        assert "RuntimeError" not in crash.text

        schema = await c.get("/openapi.json")
        assert schema.status_code == 404  # openapi_url=None — no self-description


async def test_audit_log_is_append_only_at_the_db_level(
    db_session: AsyncSession,
) -> None:
    """A09 — the immutability trigger must exist on the live schema (P0 built
    it; this gate fails if a future migration ever drops it)."""
    triggers = (
        (
            await db_session.execute(
                text(
                    "SELECT tgname FROM pg_trigger "
                    "WHERE tgrelid = 'audit_logs'::regclass AND NOT tgisinternal"
                )
            )
        )
        .scalars()
        .all()
    )
    assert "trg_audit_immutable" in triggers, triggers


# ================== Performance acceptance guard (< 3s) ==================


async def test_daily_reports_meet_the_3s_budget_at_pilot_scale(
    db_session: AsyncSession,
) -> None:
    """The Phase-3 acceptance criterion, measured end-to-end through the
    service layer on synthetic pilot scale: 22k invoices / 44k lines over a
    year with a dense today, plus returns and expenses so the P&L chain is
    fully exercised. All rows roll back — nothing touches the shared DB."""
    branch = Branch(name=f"أداء {uuid.uuid4().hex[:6]}", country_code="EG", currency_code="EGP")
    db_session.add(branch)
    await db_session.commit()  # only the branch commits (harmless); data rolls back

    med_id, pack_id, batch_id = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    await db_session.execute(
        text(
            "INSERT INTO medications (id, trade_name, trade_name_ar) "
            "VALUES (:m, 'PerfMed', 'دواء الأداء')"
        ),
        {"m": med_id},
    )
    unit_id = (
        await db_session.execute(
            text(
                "INSERT INTO units (name_ar) VALUES ('شريط') "
                "ON CONFLICT (name_ar) DO UPDATE SET name_ar = EXCLUDED.name_ar RETURNING id"
            )
        )
    ).scalar_one()
    await db_session.execute(
        text(
            "INSERT INTO medication_packaging (id, medication_id, level, unit_id, name_ar, "
            "qty_in_parent, selling_price, is_default_sale) "
            "VALUES (:p, :m, 2, :u, 'شريط', 10, 30.00, TRUE)"
        ),
        {"p": pack_id, "m": med_id, "u": unit_id},
    )
    await db_session.execute(
        text(
            "INSERT INTO medication_batches (id, branch_id, medication_id, batch_number, "
            "expiry_date, quantity, purchase_price, status) "
            "VALUES (:b, :br, :m, 'PERF-BATCH', CURRENT_DATE + 365, 1000000, 2.00, 'active')"
        ),
        {"b": batch_id, "br": branch.id, "m": med_id},
    )

    b = str(branch.id)
    # 20k invoices across the year + 2k dense TODAY rows (local midnight via
    # date_trunc in the SESSION timezone — the M7 local-day convention).
    await db_session.execute(
        text(
            "INSERT INTO invoices (branch_id, invoice_number, status, currency_code, subtotal, "
            "tax_amount, total, payment_method, created_at) "
            "SELECT :b, 'PERF-' || g, 'completed', 'EGP', 30.00, 0, 30.00, "
            "(ARRAY['cash','card'])[1 + g % 2], "
            "date_trunc('day', NOW()) + interval '10 hours' + (g % 600) * interval '1 minute' "
            "FROM generate_series(1, 2000) g"
        ),
        {"b": b},
    )
    await db_session.execute(
        text(
            "INSERT INTO invoices (branch_id, invoice_number, status, currency_code, subtotal, "
            "tax_amount, total, payment_method, created_at) "
            "SELECT :b, 'PERF-' || g, 'completed', 'EGP', 30.00, 0, 30.00, "
            "(ARRAY['cash','card'])[1 + g % 2], "
            "NOW() - (g % 365) * interval '1 day' - (g % 24) * interval '1 hour' "
            "FROM generate_series(2001, 22000) g"
        ),
        {"b": b},
    )
    # 2 sale lines per invoice (strip @30.00 = 10 tablets of a 2.00 batch).
    await db_session.execute(
        text(
            "INSERT INTO invoice_items (branch_id, invoice_id, medication_id, packaging_id, "
            "batch_id, quantity, qty_smallest, unit_price, line_total) "
            "SELECT i.branch_id, i.id, :m, :p, :ba, 1, 10, 30.00, 30.00 "
            "FROM invoices i, generate_series(1, 2) g "
            "WHERE i.branch_id = :b AND i.invoice_number LIKE 'PERF-%'"
        ),
        {"m": med_id, "p": pack_id, "ba": batch_id, "b": b},
    )
    # 2k credit notes netting 10.00 each, with one returned line apiece.
    await db_session.execute(
        text(
            "INSERT INTO returns (branch_id, original_invoice_id, return_number, currency_code, "
            "subtotal, total, refund_method, created_at) "
            "SELECT :b, i.id, 'PERFRET-' || i.invoice_number, 'EGP', 10.00, 10.00, 'cash', "
            "i.created_at FROM invoices i WHERE i.branch_id = :b "
            "AND i.invoice_number LIKE 'PERF-2%' LIMIT 2000"
        ),
        {"b": b},
    )
    await db_session.execute(
        text(
            "INSERT INTO return_items (branch_id, return_id, medication_id, packaging_id, "
            "batch_id, quantity, qty_smallest, unit_price, line_total) "
            "SELECT r.branch_id, r.id, :m, :p, :ba, 1, 2, 30.00, 10.00 "
            "FROM returns r WHERE r.branch_id = :b AND r.return_number LIKE 'PERFRET-%'"
        ),
        {"m": med_id, "p": pack_id, "ba": batch_id, "b": b},
    )
    await db_session.execute(
        text(
            "WITH cat AS (INSERT INTO expense_categories (name_ar) VALUES ('PERF M8') "
            "RETURNING id) "
            "INSERT INTO expenses (branch_id, expense_category_id, amount, currency_code, "
            "expense_date, payment_method) "
            "SELECT :b, cat.id, 50.00, 'EGP', CURRENT_DATE - (g % 30), 'cash' "
            "FROM generate_series(1, 500) g, cat"
        ),
        {"b": b},
    )

    today = (await db_session.execute(text("SELECT CURRENT_DATE"))).scalar_one()

    async def _measure(date_from: dt.date, date_to: dt.date, *, pnl: bool) -> float:
        start = time.perf_counter()
        if pnl:
            await reporting_service.profit_loss_report(
                db_session, branch_id=branch.id, date_from=date_from, date_to=date_to
            )
        else:
            await reporting_service.sales_report(
                db_session, branch_id=branch.id, date_from=date_from, date_to=date_to
            )
        return time.perf_counter() - start

    try:
        daily_sales = await _measure(today, today, pnl=False)
        daily_pnl = await _measure(today, today, pnl=True)
        annual_sales = await _measure(today - dt.timedelta(days=365), today, pnl=False)

        # Budget 3.0s; measured reality is tens of ms (~70x headroom on the
        # tightest daily assert) — the wall-clock assert cannot flake a
        # shared CI runner at that margin.
        assert daily_sales < 3.0, f"daily sales report took {daily_sales:.3f}s (budget 3s)"
        assert daily_pnl < 3.0, f"daily P&L took {daily_pnl:.3f}s (budget 3s)"
        assert annual_sales < 3.0, f"annual sales report took {annual_sales:.3f}s (budget 3s)"
    finally:
        await db_session.rollback()  # the synthetic dataset leaves no trace


# ==================== Alerts-summary rollup (M6 deferral) ====================


@pytest.fixture
async def branch(db_session: AsyncSession) -> Branch:
    b = Branch(name=f"فرع {uuid.uuid4().hex[:6]}", country_code="EG", currency_code="EGP")
    db_session.add(b)
    await db_session.commit()
    return b


async def _insert_alert(
    db_session: AsyncSession,
    branch_id: uuid.UUID,
    *,
    rule: str,
    severity: str,
    dedup: str,
) -> None:
    await db_session.execute(
        text(
            "INSERT INTO alerts (branch_id, rule_key, severity, message_key, dedup_key) "
            "VALUES (:b, :r, :s, :mk, :d)"
        ),
        {"b": str(branch_id), "r": rule, "s": severity, "mk": "alerts.msg." + rule, "d": dedup},
    )


async def test_alert_summary_rolls_up_all_branches(
    db_session: AsyncSession, branch: Branch
) -> None:
    """branch_id omitted = live counts across EVERY branch. Asserted as exact
    per-branch deltas over pre-insert baselines — the shared committed DB may
    hold other branches' rows, and the rollup must add up to exactly those."""
    other = Branch(name=f"فرع ثانٍ {uuid.uuid4().hex[:6]}", country_code="EG", currency_code="EGP")
    db_session.add(other)
    await db_session.commit()

    base_first = await alerts_service.alert_summary(db_session, branch_id=branch.id)
    base_second = await alerts_service.alert_summary(db_session, branch_id=other.id)
    base_rollup = await alerts_service.alert_summary(db_session)
    await _insert_alert(db_session, branch.id, rule="low_stock", severity="warning", dedup="m8-a")
    await _insert_alert(
        db_session, branch.id, rule="out_of_stock", severity="critical", dedup="m8-b"
    )
    await _insert_alert(
        db_session, other.id, rule="cash_discrepancy", severity="critical", dedup="m8-c"
    )
    await _insert_alert(db_session, other.id, rule="expired", severity="danger", dedup="m8-d")
    await db_session.commit()

    first = await alerts_service.alert_summary(db_session, branch_id=branch.id)
    second = await alerts_service.alert_summary(db_session, branch_id=other.id)
    rollup = await alerts_service.alert_summary(db_session)

    assert first["branch_id"] == str(branch.id)
    assert int(first["warning"]) == int(base_first["warning"]) + 1
    assert int(first["critical"]) == int(base_first["critical"]) + 1
    assert int(second["critical"]) == int(base_second["critical"]) + 1
    assert int(second["danger"]) == int(base_second["danger"]) + 1
    assert rollup["branch_id"] is None
    assert int(rollup["warning"]) == int(base_rollup["warning"]) + 1
    assert int(rollup["danger"]) == int(base_rollup["danger"]) + 1
    assert int(rollup["critical"]) == int(base_rollup["critical"]) + 2
    assert int(rollup["total"]) == int(base_rollup["total"]) + 4


async def test_alert_summary_http_rollup_and_permission_gate(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seeded_user: dict,
) -> None:
    """Same rollup over HTTP (authed); the alerts.view gate still applies when
    branch_id is omitted (unauthenticated 401, cashier 403)."""
    anon = await client.get("/api/v1/alerts/summary")
    assert anon.status_code == 401  # fresh client fixture — no session cookies yet

    r = await client.post(
        "/api/v1/auth/login",
        json={"username": seeded_user["username"], "password": seeded_user["password"]},
    )
    assert r.status_code == 200, r.text
    rollup = await client.get("/api/v1/alerts/summary")
    assert rollup.status_code == 200, rollup.text
    data = rollup.json()["data"]
    assert data["branch_id"] is None
    assert set(data) >= {"warning", "danger", "critical", "total"}

    role = (
        await db_session.execute(select(Role).where(Role.code == "cashier"))
    ).scalar_one_or_none()
    assert role is not None
    cashier_username = f"cash_{uuid.uuid4().hex[:8]}"
    db_session.add(
        User(
            username=cashier_username,
            full_name="كاشير الملخص",
            password_hash=hash_password("T3st@user!"),
            role_id=role.id,
        )
    )
    await db_session.commit()
    await client.post(
        "/api/v1/auth/login", json={"username": cashier_username, "password": "T3st@user!"}
    )
    forbidden = await client.get("/api/v1/alerts/summary")
    assert forbidden.status_code == 403
