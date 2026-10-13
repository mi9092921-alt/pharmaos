"""Shelf locations M5: missing_location alert + Excel bulk import.

Alert: ONE branch aggregate (never per-medication), warning severity,
zero-stock rows excluded, resolves on re-evaluation after slotting;
every _ALERT_RULES key has an evaluator; one failing rule never aborts
the rest (errors are diagnosed, not silent).
Import: multipart POST + CSRF + inventory.edit; dry_run previews without
writing; execute re-validates fully; barcode-exact else exact-name-or-reject
(never fuzzy); duplicates-in-file rejected; 2MB / 2000-row caps; quantity
and movements untouched.
"""

import datetime as dt
import io
import uuid

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.models import Branch, Medication, MedicationBarcode, Role, User
from pharmaos_api.security.passwords import hash_password
from pharmaos_api.services import alerts_service

FUTURE = (dt.date.today() + dt.timedelta(days=365)).isoformat()
IMPORT = "/api/v1/inventory/shelf-import"


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


async def _med(db_session: AsyncSession, tag: str, *, name: str | None = None) -> str:
    med = Medication(
        trade_name=name or f"ShelfM5 {tag} {uuid.uuid4().hex[:6]}", trade_name_ar="دواء رف"
    )
    db_session.add(med)
    await db_session.commit()
    return str(med.id)


async def _receive(client, csrf, *, branch_id, medication_id, qty="10"):
    body = {
        "branch_id": branch_id,
        "medication_id": medication_id,
        "batch_number": f"B-{uuid.uuid4().hex[:6]}",
        "expiry_date": FUTURE,
        "quantity": qty,
        "purchase_price": "2.50",
    }
    r = await client.post("/api/v1/inventory/receive", headers={"X-CSRF-Token": csrf}, json=body)
    assert r.status_code == 200, r.text


async def _slot(client, csrf, branch_id, med_id, shelf):
    r = await client.patch(
        "/api/v1/inventory/settings",
        headers={"X-CSRF-Token": csrf},
        json={"branch_id": branch_id, "medication_id": med_id, "shelf_location": shelf},
    )
    assert r.status_code == 200, r.text


def _xlsx_bytes(rows: list[tuple]) -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["barcode_or_name", "shelf"])
    for key, shelf in rows:
        ws.append([key, shelf])
    buf = io.BytesIO()
    wb.save(buf)
    wb.close()
    return buf.getvalue()


async def _post_import(client, csrf, branch_id, payload: bytes, *, dry_run=True, csrf_on=True):
    headers = {"X-CSRF-Token": csrf} if csrf_on else {}
    return await client.post(
        IMPORT,
        data={"branch_id": branch_id, "dry_run": str(dry_run).lower()},
        files={
            "file": (
                "shelf.xlsx",
                payload,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
        headers=headers,
    )


# ------------------------------ alert ------------------------------


async def test_missing_location_single_aggregate_and_resolve(
    admin, branch, db_session: AsyncSession
):
    client, csrf = admin
    bid = str(branch.id)
    m1 = await _med(db_session, "one")
    m2 = await _med(db_session, "two")
    m3 = await _med(db_session, "zero")
    await _receive(client, csrf, branch_id=bid, medication_id=m1)
    await _receive(client, csrf, branch_id=bid, medication_id=m2)
    # zero-stock known SKU (same shape as a PATCH-created 0-quantity slot row):
    # must NOT count toward the aggregate.
    await db_session.execute(
        text(
            "INSERT INTO branch_inventory (branch_id, medication_id, cached_quantity) "
            "VALUES (:b, :m, 0)"
        ).bindparams(b=branch.id, m=uuid.UUID(m3))
    )
    await db_session.commit()

    first = await alerts_service.evaluate_branch(db_session, branch.id)
    assert first["errors"] == []
    assert first["findings"].get("missing_location") == 1  # ONE aggregate, not two
    rows = (
        await db_session.execute(
            text(
                "SELECT severity, entity_type, entity_id, params FROM alerts "
                "WHERE branch_id = :b AND rule_key = 'missing_location' AND status <> 'resolved'"
            ).bindparams(b=branch.id)
        )
    ).all()
    assert len(rows) == 1
    assert (rows[0][0], rows[0][1], rows[0][2]) == ("warning", "branch", None)
    assert rows[0][3]["count"] == "2"  # zero-stock row excluded

    # idempotent re-evaluation, then resolve by slotting everything
    again = await alerts_service.evaluate_branch(db_session, branch.id)
    assert again["findings"].get("missing_location") == 1
    await _slot(client, csrf, bid, m1, "A1")
    await _slot(client, csrf, bid, m2, "A2")
    cleared = await alerts_service.evaluate_branch(db_session, branch.id)
    assert cleared["findings"].get("missing_location", 0) == 0


async def test_every_rule_has_an_evaluator():
    missing = [k for k in alerts_service._ALERT_RULES if k not in alerts_service._EVALUATORS]
    assert missing == []


async def test_failing_rule_is_diagnosed_not_fatal(
    admin, branch, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    async def _boom(session, branch_id):
        raise RuntimeError("simulated evaluator failure")

    monkeypatch.setitem(alerts_service._EVALUATORS, "low_stock", _boom)
    report = await alerts_service.evaluate_branch(db_session, branch.id)
    assert report["errors"] == ["low_stock"]
    # the rest still evaluated (missing_location key present either way)
    assert "missing_location" in report["findings"]


# ------------------------------ import ------------------------------


async def test_import_dry_run_writes_nothing(admin, branch, db_session: AsyncSession):
    client, csrf = admin
    bid = str(branch.id)
    suffix = uuid.uuid4().hex[:6]
    m1 = await _med(db_session, "one", name=f"DryRunA {suffix}")
    m2 = await _med(db_session, "two", name=f"DryRunB {suffix}")
    await _receive(client, csrf, branch_id=bid, medication_id=m1)
    await _receive(client, csrf, branch_id=bid, medication_id=m2)
    # same display name twice -> ambiguous
    dup = f"DupName {suffix}"
    await _med(db_session, "dup1", name=dup)
    await _med(db_session, "dup2", name=dup)

    payload = _xlsx_bytes(
        [
            (f"DryRunA {suffix}", "b7"),  # name-exact, lowercase shelf -> B7
            (f"DryRunB {suffix}", "A0"),  # invalid shelf
            ("no-such-med", "C2"),  # unknown
            (dup, "C3"),  # ambiguous
            (f"DryRunA {suffix}", "C4"),  # duplicate in file (both occurrences rejected)
            ("", "C5"),  # empty key
            (f"DryRunB {suffix}", ""),  # empty shelf
        ]
    )
    r = await _post_import(client, csrf, bid, payload, dry_run=True)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["dry_run"] is True and data["total"] == 7
    assert data["valid_count"] == 0  # the only resolvable row is duplicated
    reasons = {e["reason"] for e in data["rejected"]}
    assert {
        "invalid_shelf",
        "unknown_medication",
        "ambiguous_name",
        "duplicate_in_file",
        "empty_key",
        "empty_shelf",
    } <= reasons
    # preview wrote nothing
    inv = await client.get("/api/v1/inventory", params={"branch_id": bid})
    assert all(x["shelf_location"] is None for x in inv.json()["data"])


async def test_import_execute_applies_and_rejects(admin, branch, db_session: AsyncSession):
    client, csrf = admin
    bid = str(branch.id)
    suffix = uuid.uuid4().hex[:6]
    m1 = await _med(db_session, "one", name=f"ExecA {suffix}")
    m2 = await _med(db_session, "two", name=f"ExecB {suffix}")
    await _receive(client, csrf, branch_id=bid, medication_id=m1, qty="30")
    await _receive(client, csrf, branch_id=bid, medication_id=m2, qty="30")
    moves_before = (
        await db_session.execute(
            text("SELECT COUNT(*) FROM stock_movements WHERE branch_id = :b").bindparams(
                b=branch.id
            )
        )
    ).scalar_one()

    # barcode-exact hit for m1
    code = f"622{uuid.uuid4().int % 10**10:010d}"
    db_session.add(MedicationBarcode(medication_id=uuid.UUID(m1), barcode=code))
    await db_session.commit()

    payload = _xlsx_bytes([(code, "A1"), (f"ExecB {suffix}", "B2"), ("ghost", "C3")])
    r = await _post_import(client, csrf, bid, payload, dry_run=False)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert (data["total"], data["applied"]) == (3, 2)
    assert [e["reason"] for e in data["rejected"]] == ["unknown_medication"]

    inv = await client.get("/api/v1/inventory", params={"branch_id": bid})
    slots = {x["medication_id"]: x["shelf_location"] for x in inv.json()["data"]}
    assert slots[m1] == "A1" and slots[m2] == "B2"
    # quantities and movements untouched; drift clean
    assert all(x["cached_quantity"] in ("30.000", "30") for x in inv.json()["data"])
    moves_after = (
        await db_session.execute(
            text("SELECT COUNT(*) FROM stock_movements WHERE branch_id = :b").bindparams(
                b=branch.id
            )
        )
    ).scalar_one()
    assert moves_after == moves_before
    drift = await client.get("/api/v1/inventory/drift", params={"branch_id": bid})
    assert drift.json()["data"]["ok"] is True
    # per-row audit exists
    n_audit = (
        await db_session.execute(
            text(
                "SELECT COUNT(*) FROM audit_logs WHERE action = 'settings.changed' "
                "AND metadata->>'scope' = 'stock_settings' "
                "AND metadata->>'reason' IS DISTINCT FROM 'shelf_norm_migration'"
            )
        )
    ).scalar_one()
    assert n_audit >= 2


async def test_import_execute_revalidates_stale_preview(admin, branch, db_session: AsyncSession):
    client, csrf = admin
    bid = str(branch.id)
    m = await _med(db_session, "stale", name=f"StaleMed {uuid.uuid4().hex[:6]}")
    await _receive(client, csrf, branch_id=bid, medication_id=m)
    med_name = (
        await db_session.execute(select(Medication.trade_name).where(Medication.id == uuid.UUID(m)))
    ).scalar_one()
    payload = _xlsx_bytes([(med_name, "D4")])

    preview = await _post_import(client, csrf, bid, payload, dry_run=True)
    assert preview.json()["data"]["valid_count"] == 1

    # medication soft-deleted after the preview -> execute must reject, not apply
    await db_session.execute(
        text("UPDATE medications SET is_deleted = TRUE WHERE id = :m").bindparams(m=uuid.UUID(m))
    )
    await db_session.commit()
    done = await _post_import(client, csrf, bid, payload, dry_run=False)
    assert done.status_code == 200, done.text
    assert done.json()["data"]["applied"] == 0
    assert done.json()["data"]["rejected"][0]["reason"] in ("unknown_medication", "apply_failed")


async def test_import_caps_and_shape(admin, branch, db_session: AsyncSession):
    client, csrf = admin
    bid = str(branch.id)
    # missing columns
    import openpyxl

    wb = openpyxl.Workbook()
    wb.active.append(["wrong", "header"])
    buf = io.BytesIO()
    wb.save(buf)
    wb.close()
    r = await _post_import(client, csrf, bid, buf.getvalue(), dry_run=True)
    assert r.status_code == 422

    # unreadable workbook
    r = await _post_import(client, csrf, bid, b"not a spreadsheet", dry_run=True)
    assert r.status_code == 422

    # oversize file (checked before parsing)
    big = b"x" * (2 * 1024 * 1024 + 1)
    r = await _post_import(client, csrf, bid, big, dry_run=True)
    assert r.status_code == 422
    assert "large" in r.json()["error"]["message"]

    # too many rows
    many = _xlsx_bytes([(f"med-{i}", "A1") for i in range(2001)])
    r = await _post_import(client, csrf, bid, many, dry_run=True)
    assert r.status_code == 422


async def test_import_permission_and_csrf(client, branch, db_session: AsyncSession):
    payload = _xlsx_bytes([("x", "A1")])

    cashier_csrf = await _login(client, await _seed_user(db_session, "cashier"))
    denied = await client.post(
        IMPORT,
        data={"branch_id": str(branch.id)},
        files={
            "file": (
                "s.xlsx",
                payload,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
        headers={"X-CSRF-Token": cashier_csrf},
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "E-AUTH-002"

    await _login(client, await _seed_user(db_session, "super_admin"))
    no_csrf = await client.post(
        IMPORT,
        data={"branch_id": str(branch.id)},
        files={
            "file": (
                "s.xlsx",
                payload,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert no_csrf.status_code == 403
    assert no_csrf.json()["error"]["code"] == "E-AUTH-004"
