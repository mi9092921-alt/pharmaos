"""Catalog import/export (super_admin only) — template, export, import.

Contract under test (CLAUDE.md "قالب ملف استيراد/تصدير الكاتلوج"):
- Template: header-only sheet + Arabic instructions sheet, generated from the
  same column constant the parser validates against; uploading it unchanged
  must be a harmless no-op.
- Export: pure catalog without a branch; per-ACTIVE-batch quantity rows with a
  branch; ASCII-only filename; audited as catalog.exported.
- Import: dry_run writes nothing; execute creates medications + level-1
  packaging + price history + batches + purchase_in movements + cache deltas;
  updates are idempotent (empty cells keep values, name never renamed);
  round-trip of an exported file re-imports without rejects or duplicates;
  every bad row is rejected with a named reason; catalog.import + CSRF gate.
"""

import datetime as dt
import io
import os
import subprocess
import uuid
from pathlib import Path

import httpx
import openpyxl
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.models import Branch, Medication, Role, User
from pharmaos_api.security.passwords import hash_password
from pharmaos_api.services import catalog_io_service as catalog_io

EXPORT = "/api/v1/catalog/export"
TEMPLATE = "/api/v1/catalog/import-template"
IMPORT = "/api/v1/catalog/import"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

FUTURE = (dt.date.today() + dt.timedelta(days=365)).isoformat()

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SEED_FILE = _REPO_ROOT / "packages" / "db" / "seeds" / "rbac_seed.sql"


@pytest.fixture(scope="session", autouse=True)
def _fresh_rbac_seed() -> None:
    """Boot migrations are skipped in the test env, so a test DB migrated
    before the latest permissions.ts change would 403 super_admin on the new
    codes. The seed is idempotent ("code wins") — apply it like test_rbac."""
    subprocess.run(
        ["psql", os.environ["DATABASE_URL"], "-v", "ON_ERROR_STOP=1", "-q", "-f", str(_SEED_FILE)],
        check=True,
        capture_output=True,
    )


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


def _xlsx(rows: list[dict[str, str]]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(list(catalog_io._COLUMNS))  # noqa: SLF001 — the shared format contract
    for row in rows:
        ws.append([row.get(c, "") for c in catalog_io._COLUMNS])  # noqa: SLF001
    buf = io.BytesIO()
    wb.save(buf)
    wb.close()
    return buf.getvalue()


def _csv_bytes(rows: list[dict[str, str]]) -> bytes:
    """CSV twin of _xlsx — with a UTF-8 BOM, exactly like the system's own
    exports, so the parser's utf-8-sig path is what gets exercised."""
    lines = [",".join(catalog_io._COLUMNS)]  # noqa: SLF001
    for row in rows:
        lines.append(",".join(row.get(c, "") for c in catalog_io._COLUMNS))  # noqa: SLF001
    return ("\ufeff" + "\n".join(lines) + "\n").encode("utf-8")


async def _post_import(
    client, csrf, branch_id, payload: bytes, *, name="catalog.xlsx", dry_run=True, csrf_on=True
):
    headers = {"X-CSRF-Token": csrf} if csrf_on else {}
    return await client.post(
        IMPORT,
        data={"branch_id": str(branch_id), "dry_run": str(dry_run).lower()},
        files={"file": (name, payload, XLSX_MIME)},
        headers=headers,
    )


def _row(name: str, **kw: str) -> dict[str, str]:
    base = {"trade_name_en": name, "expiry_date": FUTURE}
    base.update(kw)
    return base


async def _med_count(db_session: AsyncSession) -> int:
    return len(
        (await db_session.execute(text("SELECT id FROM medications WHERE NOT is_deleted"))).all()
    )


# ------------------------------ template ------------------------------


async def test_template_shape(admin):
    client, csrf = admin
    r = await client.get(TEMPLATE)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith(XLSX_MIME)
    wb = openpyxl.load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames == ["القالب", "تعليمات"]
    rows = list(wb.worksheets[0].iter_rows(values_only=True))
    assert [c.lower() for c in rows[0]] == list(catalog_io._COLUMNS)  # noqa: SLF001
    assert len(rows) == 1  # header only — nothing importable by accident
    # the instructions sheet documents every column
    doc_rows = list(wb.worksheets[1].iter_rows(values_only=True))
    documented = {r[0] for r in doc_rows if r and r[0] in catalog_io._COLUMNS}  # noqa: SLF001
    assert documented == set(catalog_io._COLUMNS)  # noqa: SLF001


async def test_template_upload_is_noop(admin, branch):
    client, csrf = admin
    r = await client.get(TEMPLATE)
    resp = await _post_import(client, csrf, branch.id, r.content, name="catalog_template.xlsx")
    assert resp.status_code == 200, resp.text
    report = resp.json()["data"]
    assert report["total"] == 0 and report["rejected"] == []


async def test_template_permission_denied(client, db_session: AsyncSession):
    await _login(client, await _seed_user(db_session, "pharmacist"))
    assert (await client.get(TEMPLATE)).status_code == 403
    assert (await client.get(EXPORT)).status_code == 403


# ------------------------------- export -------------------------------


async def test_export_catalog_only_and_with_branch(admin, branch, db_session: AsyncSession):
    client, csrf = admin
    med = Medication(trade_name=f"Export {uuid.uuid4().hex[:6]}", trade_name_ar="دواء تصدير")
    db_session.add(med)
    await db_session.commit()

    r = await client.get(EXPORT)
    assert r.status_code == 200, r.text
    wb = openpyxl.load_workbook(io.BytesIO(r.content))
    rows = list(wb.worksheets[0].iter_rows(values_only=True))
    assert [c.lower() for c in rows[0]] == list(catalog_io._COLUMNS)  # noqa: SLF001
    med_row = next(x for x in rows[1:] if x[0] == med.trade_name)
    idx = {c: i for i, c in enumerate(catalog_io._COLUMNS)}  # noqa: SLF001
    # openpyxl reads written empty cells back as None
    assert not med_row[idx["branch"]] and not med_row[idx["quantity"]]

    # receive a batch through the normal API, then export that branch
    body = {
        "branch_id": str(branch.id),
        "medication_id": str(med.id),
        "batch_number": "B-EXP-1",
        "expiry_date": FUTURE,
        "quantity": "7.5",
        "purchase_price": "3.25",
    }
    rr = await client.post("/api/v1/inventory/receive", headers={"X-CSRF-Token": csrf}, json=body)
    assert rr.status_code == 200, rr.text

    r2 = await client.get(EXPORT, params={"branch_id": str(branch.id)})
    assert r2.status_code == 200
    assert branch.id.hex[:8] in r2.headers["content-disposition"]
    wb2 = openpyxl.load_workbook(io.BytesIO(r2.content))
    rows2 = list(wb2.worksheets[0].iter_rows(values_only=True))[1:]
    batch_rows = [x for x in rows2 if x[0] == med.trade_name]
    assert len(batch_rows) == 1  # one row per active batch — no extra catalog row
    assert batch_rows[0][idx["batch_number"]] == "B-EXP-1"
    assert batch_rows[0][idx["quantity"]] == "7.500"  # Numeric(12,3) string
    assert batch_rows[0][idx["branch"]] == branch.name

    # the export is audited
    log = (
        await db_session.execute(
            text(
                "SELECT action FROM audit_logs WHERE action = 'catalog.exported' "
                "ORDER BY created_at DESC LIMIT 1"
            )
        )
    ).scalar()
    assert log == "catalog.exported"


async def test_export_unknown_branch_422(admin):
    client, _ = admin
    r = await client.get(EXPORT, params={"branch_id": str(uuid.uuid4())})
    assert r.status_code == 422


# ------------------------------- import -------------------------------


async def test_import_dry_run_writes_nothing(admin, branch, db_session: AsyncSession):
    client, csrf = admin
    before = await _med_count(db_session)
    payload = _xlsx([_row("ImportDry New", price_egp="10.00", quantity="5", batch_number="B1")])
    resp = await _post_import(client, csrf, branch.id, payload)
    assert resp.status_code == 200, resp.text
    report = resp.json()["data"]
    assert report["dry_run"] is True and report["to_create"] == 1 and report["batches"] == 1
    assert await _med_count(db_session) == before


async def test_import_execute_creates_full_chain(admin, branch, db_session: AsyncSession):
    client, csrf = admin
    name = f"ImportExec {uuid.uuid4().hex[:6]}"
    payload = _xlsx(
        [
            _row(
                name,
                trade_name_ar="دواء استيراد",
                price_egp="12.50",
                quantity="10",
                batch_number="B-X1",
                purchase_price="8.00",
            )
        ]
    )
    resp = await _post_import(client, csrf, branch.id, payload, dry_run=False)
    assert resp.status_code == 200, resp.text
    report = resp.json()["data"]
    assert report["created"] == 1 and report["batches"] == 1

    med = (
        await db_session.execute(select(Medication).where(Medication.trade_name == name))
    ).scalar_one()
    pkg = (
        await db_session.execute(
            text(
                "SELECT selling_price, price_source FROM medication_packaging "
                "WHERE medication_id = :m AND level = 1"
            ).bindparams(m=med.id)
        )
    ).one()
    assert str(pkg.selling_price) == "12.50" and pkg.price_source == "import"
    hist = (
        await db_session.execute(
            text(
                "SELECT old_price, new_price, price_source FROM medication_price_history "
                "WHERE medication_id = :m"
            ).bindparams(m=med.id)
        )
    ).one()
    assert hist.old_price is None and str(hist.new_price) == "12.50"
    batch = (
        await db_session.execute(
            text(
                "SELECT b.quantity, m.movement_type, m.reference_type FROM medication_batches b "
                "JOIN stock_movements m ON m.batch_id = b.id "
                "WHERE b.medication_id = :m AND b.branch_id = :br"
            ).bindparams(m=med.id, br=branch.id)
        )
    ).one()
    assert str(batch.quantity) == "10.000"
    assert batch.movement_type == "purchase_in" and batch.reference_type == "catalog_import"
    cached = (
        await db_session.execute(
            text(
                "SELECT cached_quantity FROM branch_inventory "
                "WHERE branch_id = :br AND medication_id = :m"
            ).bindparams(br=branch.id, m=med.id)
        )
    ).scalar()
    assert str(cached) == "10.000"
    log = (
        await db_session.execute(
            text("SELECT action FROM audit_logs WHERE action = 'catalog.imported' LIMIT 1")
        )
    ).scalar()
    assert log == "catalog.imported"


async def test_import_updates_existing_idempotently(admin, branch, db_session: AsyncSession):
    client, csrf = admin
    med = Medication(
        trade_name=f"ImportUpd {uuid.uuid4().hex[:6]}",
        scientific_name="Old Molecule",
        manufacturer="Old Co",
    )
    db_session.add(med)
    await db_session.commit()
    await db_session.execute(
        text(
            "INSERT INTO medication_packaging (medication_id, level, unit_id, name_ar, "
            "is_sellable, selling_price, is_default_sale, price_source) "
            "SELECT :m, 1, id, 'علبة', TRUE, 5.00, TRUE, 'manual' FROM units LIMIT 1"
        ).bindparams(m=med.id)
    )
    await db_session.commit()

    # empty cells keep values; provided price updates level-1 + history
    payload = _xlsx([_row(med.trade_name, price_egp="6.00")])
    resp = await _post_import(client, csrf, branch.id, payload, dry_run=False)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["updated"] == 1
    pkg = (
        await db_session.execute(
            text("SELECT scientific_name, manufacturer FROM medications WHERE id = :m").bindparams(
                m=med.id
            )
        )
    ).one()
    assert pkg.scientific_name == "Old Molecule" and pkg.manufacturer == "Old Co"
    price = (
        await db_session.execute(
            text(
                "SELECT p.selling_price, h.old_price, h.new_price, h.price_source "
                "FROM medication_packaging p JOIN medication_price_history h "
                "ON h.packaging_id = p.id WHERE p.medication_id = :m AND p.level = 1 "
                "ORDER BY h.created_at DESC LIMIT 1"
            ).bindparams(m=med.id)
        )
    ).one()
    assert str(price.selling_price) == "6.00" and str(price.old_price) == "5.00"
    assert price.price_source == "import"


async def test_import_round_trip_clean(admin, branch):
    client, csrf = admin
    name = f"ImportRT {uuid.uuid4().hex[:6]}"
    payload = _xlsx([_row(name, price_egp="9.00", quantity="4", batch_number="B-RT")])
    resp = await _post_import(client, csrf, branch.id, payload, dry_run=False)
    assert resp.status_code == 200, resp.text

    exported = await client.get(EXPORT, params={"branch_id": str(branch.id)})
    assert exported.status_code == 200
    # re-import ONLY this medication's rows — the shared test DB holds
    # security-corpus names that must not leak into the round-trip assertion
    wb = openpyxl.load_workbook(io.BytesIO(exported.content))
    data_rows = list(wb.worksheets[0].iter_rows(values_only=True))
    cols = [str(c).lower() if c is not None else "" for c in data_rows[0]]
    mine = [
        {c: ("" if v is None else str(v)) for c, v in zip(cols, row, strict=False)}
        for row in data_rows[1:]
        if row and row[0] == name
    ]
    assert len(mine) == 1  # one row per active batch — no extra catalog row
    again = await _post_import(client, csrf, branch.id, _xlsx(mine), name="roundtrip.xlsx")
    assert again.status_code == 200, again.text
    report = again.json()["data"]
    # the catalog row AND the batch row both match the existing medication —
    # duplicates are only per (medication, batch_number), so nothing is rejected
    assert report["rejected"] == []
    assert report["to_update"] == 1
    assert report["batches"] == 1  # the exported batch, seen once
    assert report["quantity_total"] == "4.000"


async def test_import_rejects_bad_rows_with_reasons(admin, branch):
    client, csrf = admin
    payload = _xlsx(
        [
            _row("", price_egp="1"),  # empty_name
            _row("Bad Price", price_egp="abc"),  # bad_price
            _row("No Batch", quantity="3"),  # missing_batch_number
            _row("No Expiry", expiry_date="", quantity="3", batch_number="B1"),  # missing_expiry
            _row("Bad Expiry", quantity="3", batch_number="B1", expiry_date="31/05/2027"),
            _row(
                "Stale",
                quantity="3",
                batch_number="B1",
                expiry_date=(dt.date.today() - dt.timedelta(days=1)).isoformat(),
            ),
            _row("Bad Qty", quantity="-2", batch_number="B1"),  # bad_quantity
            _row("Wrong Branch", branch="فرع آخر"),  # branch_mismatch
            _row("Dup", quantity="1", batch_number="B9"),  # duplicate_batch_in_file
            _row("Dup", quantity="2", batch_number="B9"),
        ]
    )
    resp = await _post_import(client, csrf, branch.id, payload)
    assert resp.status_code == 200, resp.text
    report = resp.json()["data"]
    reasons = {r["reason"] for r in report["rejected"]}
    assert reasons == {
        "empty_name",
        "bad_price",
        "missing_batch_number",
        "missing_expiry",
        "bad_expiry",
        "expired_batch",
        "bad_quantity",
        "branch_mismatch",
        "duplicate_batch_in_file",
    }
    dup_rows = [r["row"] for r in report["rejected"] if r["reason"] == "duplicate_batch_in_file"]
    # first occurrence applies, the repeat is rejected — no silent last-wins
    assert dup_rows == [11]


async def test_import_ambiguous_name_rejected(admin, branch, db_session: AsyncSession):
    client, csrf = admin
    shared = f"Ambig {uuid.uuid4().hex[:6]}"
    db_session.add(Medication(trade_name=shared))
    db_session.add(Medication(trade_name=shared))
    await db_session.commit()
    payload = _xlsx([_row(shared, price_egp="1.00")])
    resp = await _post_import(client, csrf, branch.id, payload)
    report = resp.json()["data"]
    assert [r["reason"] for r in report["rejected"]] == ["ambiguous_name"]


async def test_import_csv_with_bom(admin, branch, db_session: AsyncSession):
    client, csrf = admin
    name = f"ImportCSV {uuid.uuid4().hex[:6]}"
    payload = _csv_bytes(
        [
            _row(
                name,
                trade_name_ar="دواء CSV",
                price_egp="9.00",
                quantity="2.5",
                batch_number="B-CSV",
                purchase_price="1.00",
            )
        ]
    )
    resp = await _post_import(client, csrf, branch.id, payload, name="catalog.csv", dry_run=False)
    assert resp.status_code == 200, resp.text
    report = resp.json()["data"]
    assert report["created"] == 1 and report["batches"] == 1
    med = (
        await db_session.execute(select(Medication).where(Medication.trade_name == name))
    ).scalar_one()
    assert med.trade_name_ar == "دواء CSV"


async def test_import_permission_and_csrf(client, db_session: AsyncSession, branch):
    # cashier: denied even with a valid session
    cashier_csrf = await _login(client, await _seed_user(db_session, "cashier"))
    r = await _post_import(client, cashier_csrf, branch.id, _xlsx([_row("X")]))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "E-AUTH-002"
    # super_admin without the CSRF header: rejected
    admin_csrf = await _login(client, await _seed_user(db_session, "super_admin"))
    r2 = await _post_import(client, admin_csrf, branch.id, _xlsx([_row("X")]), csrf_on=False)
    assert r2.status_code == 403


async def test_import_caps(admin, branch):
    client, csrf = admin
    payload = _csv_bytes(
        [
            {"trade_name_en": f"CapRow {i}", "branch": branch.name}
            for i in range(catalog_io.CATALOG_IMPORT_MAX_ROWS + 1)  # noqa: SLF001
        ]
    )
    resp = await _post_import(client, csrf, branch.id, payload, name="big.csv")
    assert resp.status_code == 422


async def test_import_unsupported_type(admin, branch):
    client, csrf = admin
    resp = await _post_import(client, csrf, branch.id, b"hello", name="catalog.txt")
    assert resp.status_code == 422
    assert "Unsupported" in resp.json()["error"]["message"]


# --------------------------- all-or-nothing execute ---------------------------


async def test_import_execute_aborts_on_any_rejected_row(admin, branch, db_session: AsyncSession):
    """ALL-OR-NOTHING: one bad row in an execute call aborts the WHOLE import —
    even fully-valid rows in the same file write NOTHING (review decision:
    a partial stock import is worse than a failed one)."""
    client, csrf = admin
    before_meds = await _med_count(db_session)
    name = f"AbortOK {uuid.uuid4().hex[:6]}"
    payload = _xlsx(
        [
            _row(name, price_egp="5.00", quantity="3", batch_number="B-OK"),
            _row("Abort Bad Row", price_egp="not-a-price"),
        ]
    )
    resp = await _post_import(client, csrf, branch.id, payload, dry_run=False)
    assert resp.status_code == 200, resp.text
    report = resp.json()["data"]
    assert report["aborted"] is True
    assert report["created"] == 0 and report["batches"] == 0
    assert [r["reason"] for r in report["rejected"]] == ["bad_price"]
    assert await _med_count(db_session) == before_meds, "valid row wrote nothing either"


async def test_import_execute_aborts_on_apply_failure_rolls_back(
    admin, branch, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """An apply-time failure (a race that passed validation) rolls back the
    ENTIRE transaction — medications, packaging, prices, batches, cache — and
    the response names the failing row."""
    from pharmaos_api.errors import ApiError, ErrorCode
    from pharmaos_api.services import inventory_service

    client, csrf = admin
    before_meds = await _med_count(db_session)
    before_batches = (
        await db_session.execute(text("SELECT count(*) FROM medication_batches"))
    ).scalar_one()

    real_receive = inventory_service.receive_batch
    calls = {"n": 0}

    async def flaky_receive(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:  # 2nd batch row "races" at apply time
            raise ApiError(ErrorCode.STOCK_INSUFFICIENT, 422, message="simulated race")
        return await real_receive(*args, **kwargs)

    monkeypatch.setattr(inventory_service, "receive_batch", flaky_receive)

    n1, n2 = f"Rollback1 {uuid.uuid4().hex[:6]}", f"Rollback2 {uuid.uuid4().hex[:6]}"
    payload = _xlsx(
        [
            _row(n1, price_egp="5.00", quantity="3", batch_number="B-R1"),
            _row(n2, price_egp="6.00", quantity="4", batch_number="B-R2"),
        ]
    )
    resp = await _post_import(client, csrf, branch.id, payload, dry_run=False)
    assert resp.status_code == 200, resp.text
    report = resp.json()["data"]
    assert report["aborted"] is True
    assert [r["reason"] for r in report["rejected"]] == ["apply_failed"]
    assert report["rejected"][0]["row"] == 3
    assert await _med_count(db_session) == before_meds, "first row rolled back too"
    after_batches = (
        await db_session.execute(text("SELECT count(*) FROM medication_batches"))
    ).scalar_one()
    assert after_batches == before_batches
    cache = (
        await db_session.execute(
            text(
                "SELECT count(*) FROM branch_inventory bi JOIN medications m "
                "ON m.id = bi.medication_id WHERE m.trade_name IN (:a, :b)"
            ).bindparams(a=n1, b=n2)
        )
    ).scalar_one()
    assert cache == 0, "cache deltas rolled back too"


async def test_import_10k_rows_single_transaction(admin, branch, db_session: AsyncSession):
    """Size probe (~10k rows, the review's requested scale): one transaction,
    timed — total wall clock is the lock-duration proxy (there is exactly ONE
    commit). Prints the duration; the ceiling guards against pathology."""
    import time as time_module

    client, csrf = admin
    stamp = uuid.uuid4().hex[:6]
    payload = _csv_bytes(
        [
            _row(
                f"Bulk {stamp} {i}",
                price_egp="10.00",
                quantity="5",
                batch_number=f"BLK-{i}",
                purchase_price="7.00",
            )
            for i in range(10_000)
        ]
    )
    started = time_module.perf_counter()
    resp = await _post_import(client, csrf, branch.id, payload, name="bulk10k.csv", dry_run=False)
    elapsed = time_module.perf_counter() - started
    assert resp.status_code == 200, resp.text
    report = resp.json()["data"]
    assert report["aborted"] is False
    assert report["created"] == 10_000
    assert report["batches"] == 10_000
    assert report["quantity_total"] == "50000.000"
    print(f"\n10k-row import: {elapsed:.1f}s (single transaction)")
    assert elapsed < 300, f"10k-row import regressed badly: {elapsed:.1f}s"
