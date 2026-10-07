"""Operational CLI.

bootstrap-admin: create the first super_admin user (Phase 0 acceptance:
"clean copy -> docker compose up -> migrate -> super_admin login").

The super_admin ROLE row is upserted here by code (the same code-defined
role the M7 seeder maintains) — never created by hand in the DB.
Credentials come from arguments/environment — never hardcoded (forbidden #4).

backup create / backup restore-drill / backup export-key / backup import-key /
backup restore: operational entry points for the encrypted-backup subsystem.
export-key prints the backup key ONCE for the owner to store OFFLINE — it is
the recovery root; import-key brings it back on a fresh device (never as a
command-line argument — process lists/shell history must not see it); restore
is the safe cluster-level path (staging -> verify -> promote, decision 11).

migrate: apply pending SQL migrations + re-apply code-defined seeds with the
EXACT semantics of packages/db/scripts/apply-migrations.sh (the bash script
stays the CI path; the device has no bash/psql — asyncpg only).

compliance-drain: branch-wide ETA/EDA outbox drain for the scheduled task —
no actor (audit rows carry NULL actor = system, by design) and FAIL-CLOSED in
production when only the local simulator is configured.

Every maintenance command (backup/restore/migrate/catalog-seed and the
first-run flow) holds the device-wide maintenance mutex — a 02:00 scheduled
backup can never collide with a manual restore.

Usage:
    python -m pharmaos_api.cli bootstrap-admin --username <name> --full-name <name>
    (password via PHARMAOS_ADMIN_PASSWORD env var or interactive prompt)
    python -m pharmaos_api.cli backup create [--backup-dir PATH] [--no-cloud]
    python -m pharmaos_api.cli backup restore-drill --file PATH --drill-database-url URL
    python -m pharmaos_api.cli backup export-key
    python -m pharmaos_api.cli backup import-key [--stdin]
    python -m pharmaos_api.cli backup restore --file PATH [--pgdata DIR]
    python -m pharmaos_api.cli migrate
    python -m pharmaos_api.cli compliance-drain
"""

import argparse
import asyncio
import getpass
import json as _json
import os
import sys
import uuid
from pathlib import Path

from sqlalchemy import func, select

from pharmaos_api.db import get_session_factory
from pharmaos_api.maintenance import MaintenanceBusyError, maintenance_lock
from pharmaos_api.models import Role, User
from pharmaos_api.security.passwords import hash_password, validate_password_policy

SUPER_ADMIN_ROLE_CODE = "super_admin"
SUPER_ADMIN_ROLE_NAME_AR = "مالك النظام"

# Exit code for "another maintenance operation is running" — schedulers can
# distinguish it from a real failure (StartWhenAvailable retries later).
MAINTENANCE_BUSY_EXIT = 5


async def _bootstrap_admin(username: str, full_name: str, password: str) -> int:
    violations = validate_password_policy(password)
    if violations:
        print(f"password policy violations: {', '.join(violations)}", file=sys.stderr)
        return 2

    async with get_session_factory()() as session:
        existing_user = (
            await session.execute(
                select(func.count()).select_from(User).where(User.is_deleted.is_(False))
            )
        ).scalar_one()
        if existing_user > 0:
            print(
                "a user already exists on this device - the first-run step is "
                "already done (open PharmaOS and log in).",
                file=sys.stderr,
            )
            return 1

        role = (
            await session.execute(select(Role).where(Role.code == SUPER_ADMIN_ROLE_CODE))
        ).scalar_one_or_none()
        if role is None:
            role = Role(
                code=SUPER_ADMIN_ROLE_CODE, name_ar=SUPER_ADMIN_ROLE_NAME_AR, is_system=True
            )
            session.add(role)
            await session.flush()

        existing = (
            await session.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
        if existing is not None:
            print(f"user '{username}' already exists — nothing to do.", file=sys.stderr)
            return 1

        user = User(
            username=username,
            full_name=full_name,
            password_hash=hash_password(password),
            role_id=role.id,
        )
        session.add(user)
        await session.commit()
        print(f"super_admin '{username}' created.")
        return 0


def _backup_create(backup_dir: Path, *, cloud: bool) -> int:
    from pharmaos_api.services import backup_service

    backup_file = backup_service.create_backup(backup_dir)
    print(f"backup created: {backup_file}")
    if cloud:
        uploaded = backup_service.upload_to_cloud(backup_file)
        print("cloud copy: uploaded" if uploaded else "cloud copy: skipped (not configured)")
    return 0


def _backup_restore_drill(backup_file: Path, drill_database_url: str) -> int:
    from pharmaos_api.services import backup_service

    counts = backup_service.restore_drill(backup_file, drill_database_url=drill_database_url)
    print(f"restore drill OK: {counts}")
    return 0


def _backup_export_key() -> int:
    from pharmaos_api.security import keystore

    print(keystore.ensure_backup_key().hex())
    print(
        "⚠️  Store this backup key OFFLINE (paper/sealed envelope). "
        "Without it, backups cannot be restored after device loss.",
        file=sys.stderr,
    )
    return 0


def _backup_import_key(*, use_stdin: bool) -> int:
    """Import the offline recovery key — NEVER from a command-line argument
    (process lists, shell history and scheduler logs would capture it)."""
    from pharmaos_api.services import backup_service

    key = sys.stdin.read() if use_stdin else getpass.getpass("offline backup key (hex): ")
    try:
        backup_service.import_backup_key(key)
    except ValueError as exc:
        print(f"invalid backup key: {exc}", file=sys.stderr)
        return 2
    print("backup key imported (marked owner-provided) — keep the paper copy safe.")
    return 0


def _backup_restore(backup_file: Path, pgdata: Path, live_port: int, staging_port: int) -> int:
    """Safe cluster-level restore: staging -> verify -> promote (decision 11).
    Any failure rolls the device back to exactly its pre-restore state."""
    from pharmaos_api.services import backup_service

    try:
        report = backup_service.restore_to_cluster(
            backup_file,
            pgdata_dir=pgdata,
            live_port=live_port,
            staging_port=staging_port,
        )
    except Exception as exc:
        print(f"restore failed: {exc}", file=sys.stderr)
        return 1
    print("restore promoted and verified:")
    print(_json.dumps(report, indent=2, default=str))
    return 0


def _migrate(database_url: str | None, migrations_dir: str | None, seeds_dir: str | None) -> int:
    """Device migration path (decision 7) — bash-script semantics, asyncpg."""
    from pharmaos_api.config import get_settings
    from pharmaos_api.migrations_runner import MigrationError, run_migrations

    dsn = database_url or get_settings().resolved_database_url
    try:
        report = run_migrations(
            dsn,
            migrations_dir=Path(migrations_dir) if migrations_dir else None,
            seeds_dir=Path(seeds_dir) if seeds_dir else None,
        )
    except MigrationError as exc:
        print(f"migrate failed: {exc}", file=sys.stderr)
        return 1
    print(
        _json.dumps(
            {
                "applied": report["applied"],
                "skipped": len(report["skipped"]),  # type: ignore[arg-type]
                "seeds": report["seeds"],
            },
            indent=2,
        )
    )
    return 0


async def _compliance_drain() -> int:
    """Branch-wide ETA/EDA outbox drain (decisions 8): every active branch,
    actor=None (audit actor_user_id NULL = system, by design), fail-closed in
    production when only the local simulator is configured. Designed for the
    15-minute scheduled task — no interactive context, no branch argument."""
    from pharmaos_api.models import Branch
    from pharmaos_api.services.compliance import ereceipt_service, tt_service

    out: dict[str, object] = {}
    async with get_session_factory()() as session:
        branches = (
            (await session.execute(select(Branch).where(Branch.is_deleted.is_(False))))
            .scalars()
            .all()
        )
        for branch in branches:
            out[str(branch.id)] = {
                "ereceipts": await ereceipt_service.drain(session, branch_id=branch.id),
                "tt_events": await tt_service.drain(session, branch_id=branch.id),
            }
    print(_json.dumps({"branches": out}, indent=2, default=str))
    return 0


async def _bootstrap_branch(name: str) -> int:
    """Create the first branch (country/currency from settings — EG/EGP default)."""
    from pharmaos_api.config import get_settings
    from pharmaos_api.models import Branch

    s = get_settings()
    async with get_session_factory()() as session:
        existing = (
            (await session.execute(select(Branch).where(Branch.is_deleted.is_(False))))
            .scalars()
            .first()
        )
        if existing is not None:
            print(f"branch already exists: {existing.name} ({existing.id})", file=sys.stderr)
            return 1
        branch = Branch(name=name, country_code=s.country_code, currency_code=s.default_currency)
        session.add(branch)
        await session.commit()
        print(f"branch created: {branch.id}")
        return 0


async def _skeleton_demo_data() -> int:
    """Walking-skeleton demo data: one medication (box/strip/tablet + barcode + batch).

    Exists so the M12 hardware test (scan -> sale -> print) can run on a fresh
    device BEFORE Phase 1 catalog seeding. Idempotent by barcode.
    """
    import datetime as dt
    from decimal import Decimal

    from pharmaos_api.models import (
        Branch,
        Medication,
        MedicationBarcode,
        MedicationBatch,
    )
    from pharmaos_api.models.catalog import MedicationPackaging as Packaging

    demo_barcode = "6224000000017"
    async with get_session_factory()() as session:
        branch = (
            (await session.execute(select(Branch).where(Branch.is_deleted.is_(False))))
            .scalars()
            .first()
        )
        if branch is None:
            print("no branch — run bootstrap-branch first.", file=sys.stderr)
            return 1
        exists = (
            await session.execute(
                select(MedicationBarcode).where(MedicationBarcode.barcode == demo_barcode)
            )
        ).scalar_one_or_none()
        if exists is not None:
            print("demo data already present.", file=sys.stderr)
            return 1

        from sqlalchemy import text as sql_text

        from pharmaos_api.models.base import Base  # noqa: F401  (explicit models below)

        unit_ids: dict[str, str] = {}
        for name_ar in ("علبة", "شريط", "قرص"):
            row = (
                await session.execute(
                    sql_text(
                        "INSERT INTO units (name_ar) VALUES (:n) "
                        "ON CONFLICT (name_ar) DO UPDATE SET name_ar=EXCLUDED.name_ar RETURNING id"
                    ).bindparams(n=name_ar)
                )
            ).scalar_one()
            unit_ids[name_ar] = str(row)

        med = Medication(trade_name="Panadol Demo 500mg", trade_name_ar="بنادول تجريبي ٥٠٠")
        session.add(med)
        await session.flush()

        levels = [
            Packaging(
                medication_id=med.id,
                level=1,
                unit_id=unit_ids["علبة"],
                name_ar="علبة",
                qty_in_parent=None,
                selling_price=Decimal("90.00"),
            ),
            Packaging(
                medication_id=med.id,
                level=2,
                unit_id=unit_ids["شريط"],
                name_ar="شريط",
                qty_in_parent=Decimal(3),
                selling_price=Decimal("30.00"),
                is_default_sale=True,
            ),
            Packaging(
                medication_id=med.id,
                level=3,
                unit_id=unit_ids["قرص"],
                name_ar="قرص",
                qty_in_parent=Decimal(10),
                selling_price=Decimal("3.50"),
            ),
        ]
        session.add_all(levels)
        await session.flush()

        session.add(MedicationBarcode(medication_id=med.id, barcode=demo_barcode, is_primary=True))
        session.add(
            MedicationBatch(
                branch_id=branch.id,
                medication_id=med.id,
                batch_number="DEMO-001",
                expiry_date=dt.date.today() + dt.timedelta(days=365),
                quantity=Decimal(300),
                purchase_price=Decimal("2.00"),
            )
        )
        await session.commit()
        print(f"demo medication ready — barcode: {demo_barcode} (300 tablets in stock)")
        return 0


async def _skeleton_sale(barcode: str, qty: str, print_host: str | None, out_file: str) -> int:
    """The M12 vertical slice: scan -> sale (FEFO, atomic) -> ESC/POS receipt."""
    from decimal import Decimal

    from sqlalchemy import select as sa_select

    from pharmaos_api.models import Branch, InvoiceItem, User
    from pharmaos_api.printing.escpos import ReceiptData, ReceiptLine, build_receipt, send_raw
    from pharmaos_api.services import sales_service

    async with get_session_factory()() as session:
        branch = (
            (await session.execute(sa_select(Branch).where(Branch.is_deleted.is_(False))))
            .scalars()
            .first()
        )
        cashier = (
            (await session.execute(sa_select(User).where(User.is_deleted.is_(False))))
            .scalars()
            .first()
        )
        if branch is None or cashier is None:
            print(
                "need a branch and a user — run bootstrap-branch / bootstrap-admin.",
                file=sys.stderr,
            )
            return 1

        scan = await sales_service.resolve_barcode(session, barcode)
        invoice = await sales_service.create_sale(
            session,
            branch_id=branch.id,
            lines=[sales_service.SaleLine(barcode=barcode, quantity=Decimal(qty))],
            cashier=cashier,
        )
        items = (
            (
                await session.execute(
                    sa_select(InvoiceItem).where(InvoiceItem.invoice_id == invoice.id)
                )
            )
            .scalars()
            .all()
        )

        payload = build_receipt(
            ReceiptData(
                pharmacy_name="PharmaOS",
                branch_name=branch.name,
                invoice_number=invoice.invoice_number,
                created_at_display=invoice.created_at.strftime("%Y-%m-%d %H:%M"),
                lines=[
                    ReceiptLine(
                        name=scan.trade_name_ar or scan.trade_name,
                        quantity=item.quantity,
                        unit_name=scan.packaging_name_ar,
                        line_total=item.line_total,
                    )
                    for item in items
                ],
                subtotal=invoice.subtotal,
                discount=invoice.discount_amount,
                total=invoice.total,
                currency_symbol="ج.م",
                thank_you_message="شكراً لزيارتكم — نتمنى لكم الشفاء العاجل",
            )
        )
        summary = f"total {invoice.total} {invoice.currency_code}"
        print(f"sale completed: {invoice.invoice_number} — {summary}")
        if print_host:
            send_raw(payload, host=print_host)
            print(f"receipt sent to printer at {print_host}:9100 (drawer pulse included)")
        else:
            # One-shot CLI write; blocking I/O is fine here (no event-loop traffic).
            with open(out_file, "wb") as fh:  # noqa: ASYNC230
                fh.write(payload)
            print(f"no printer host given — ESC/POS bytes written to {out_file}")
        return 0


async def _catalog_seed(file_path: str, price_source: str) -> int:
    """P1-M6: seed/import the catalog from CSV (CC0 dataset) or XLSX (staff template)."""
    from pharmaos_api.services.seed_service import seed_catalog

    async with get_session_factory()() as session:
        report = await seed_catalog(session, file_path=Path(file_path), price_source=price_source)
    print(_json.dumps(report.as_dict(), ensure_ascii=False, indent=1))
    return 0 if not report.errors else 3


async def _inventory_maintenance(command: str) -> int:
    """P1-M7/M11: drift check / cache rebuild / expiry sweep (periodic + at boot)."""
    import json as _json

    from pharmaos_api.models import Branch
    from pharmaos_api.services import inventory_service

    async with get_session_factory()() as session:
        out: dict[str, object] = {}
        if command == "expiry-sweep":
            out["expiry_sweep"] = await inventory_service.expiry_sweep(session)
        else:
            branches = (
                (await session.execute(select(Branch).where(Branch.is_deleted.is_(False))))
                .scalars()
                .all()
            )
            for branch in branches:
                if command == "rebuild":
                    out[str(branch.id)] = {
                        "rows": await inventory_service.rebuild_cache(session, branch.id)
                    }
                else:
                    drift = await inventory_service.drift_check(session, branch.id)
                    out[str(branch.id)] = {"drift_rows": drift}
    print(_json.dumps(out, ensure_ascii=False, indent=1))
    if command == "check" and any(v["drift_rows"] for v in out.values()):  # type: ignore[index]
        return 4
    return 0


async def _alerts_evaluate() -> int:
    """Evaluate ALERT_RULES for every active branch (P3-M6, ratified D6 —
    boot / CLI / on-demand; cron-able on the device like expiry-sweep)."""
    import json as _json

    from pharmaos_api.services import alerts_service

    async with get_session_factory()() as session:
        out = await alerts_service.evaluate_all(session)
    print(_json.dumps(out, indent=2, default=str))
    return 0


async def _notifications_drain_email() -> int:
    """Drain queued email notifications through the configured provider
    (P3-M7, ratified D5/D6 — cron-able on the device like alerts-evaluate).
    The NoopEmailProvider default keeps every row pending (nothing claimed,
    nothing lost); a configured provider marks sent_at only on an actual
    send."""
    import json as _json

    from pharmaos_api.services import notification_service

    async with get_session_factory()() as session:
        out = await notification_service.dispatch_pending_email(session)
    print(_json.dumps(out, indent=2))
    return 0


def _device_init(pgdata: str | None) -> int:
    """First-run device provisioning (installer decisions 1/12) - runs in the
    DAILY USER's session so DPAPI keys are minted by the account that will
    read them forever. Generates the DB password in memory, initdb's the
    cluster with the runtime contract (SCRAM + builtin C.UTF-8), stores
    DB_PASSWORD in the keystore, and securely deletes the transient pwfile.
    Idempotent: refuses to run twice (a second run would create a cluster the
    keystore cannot authenticate to)."""
    from pathlib import Path as _Path

    from pharmaos_api.config import default_data_dir, get_settings
    from pharmaos_api.security import keystore
    from pharmaos_api.services import backup_service

    s = get_settings()
    pgdata_dir = _Path(pgdata) if pgdata else default_data_dir() / "pgdata"
    stored = keystore.get_db_password()
    cluster_ready = (pgdata_dir / "PG_VERSION").is_file()

    if stored and cluster_ready:
        print(
            "device already provisioned (keystore + cluster present) - nothing to do.",
            file=sys.stderr,
        )
        return 1

    # The DPAPI-stored password is the recovery root for the CLUSTER: when the
    # keystore survived but pgdata was wiped (or the reverse), re-create the
    # cluster WITH the stored password instead of failing - the device heals.
    password = stored or os.urandom(24).hex()  # URL-safe hex; strong (192 bits)
    if not cluster_ready and pgdata_dir.exists() and any(pgdata_dir.iterdir()):
        # initdb requires an empty/nonexistent dir; park whatever is there.
        parked = pgdata_dir.with_name(f"pgdata.broken-{uuid.uuid4().hex[:8]}")
        pgdata_dir.rename(parked)
        print(f"parked unusable pgdata at {parked}", file=sys.stderr)
    backup_service._init_staging(
        pgdata_dir, db_user=s.db_user, db_password=password, port=s.db_port
    )
    keystore.set_db_password(password)
    print(f"device cluster provisioned: {pgdata_dir}")
    print("next: pharmaos-api migrate")
    return 0


def _setup_status_cmd() -> int:
    import json as _json

    from pharmaos_api.models import Branch

    async def _collect() -> dict[str, object]:
        from pharmaos_api.services import installation_state

        async with get_session_factory()() as session:
            users = int(
                (await session.execute(select(func.count()).select_from(User))).scalar_one()
            )
            branches = int(
                (await session.execute(select(func.count()).select_from(Branch))).scalar_one()
            )
            state = await installation_state.get_all(session)
        return {
            "users": users,
            "branches": branches,
            "setup_complete": state.get("setup_complete") == "1",
            "last_completed_step": state.get("last_completed_step", "none"),
        }

    print(_json.dumps(asyncio.run(_collect()), indent=2))
    return 0


def _setup_complete() -> int:
    """Flip installation_state.setup_complete (the LAST wizard step - after
    this, a restored device skips the wizard, decision 7)."""
    from pharmaos_api.services import installation_state

    async def _mark() -> None:
        async with get_session_factory()() as session:
            await installation_state.set_values(
                session,
                {"setup_complete": "1", "last_completed_step": "complete"},
            )

    asyncio.run(_mark())
    print("setup marked complete.")
    return 0


def _device_reset() -> int:
    """Delete the device keystore secrets (smoke-test/debug tool). DANGEROUS on
    a device with real data: the encrypted fields become undecryptable unless
    the keys were bundled in a backup. Requires --yes."""
    from pharmaos_api.security import keystore

    names = [
        keystore.DB_PASSWORD_NAME,
        keystore.JWT_PRIVATE_KEY_NAME,
        keystore.JWT_PUBLIC_KEY_NAME,
        keystore.ENCRYPTION_KEY_NAME,
        keystore.BACKUP_KEY_NAME,
        keystore.BACKUP_KEY_IMPORTED_FLAG,
    ]
    for name in names:
        keystore.delete_secret(name)
    print(f"deleted {len(names)} keystore entries (service '{keystore.SERVICE_NAME}').")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pharmaos-api")
    sub = parser.add_subparsers(dest="command", required=True)

    boot = sub.add_parser("bootstrap-admin", help="Create the first super_admin user.")
    boot.add_argument("--username", required=True)
    boot.add_argument("--full-name", required=True)

    backup = sub.add_parser("backup", help="Encrypted backup operations.")
    backup_sub = backup.add_subparsers(dest="backup_command", required=True)
    b_create = backup_sub.add_parser("create", help="Create an encrypted backup now.")
    b_create.add_argument(
        "--backup-dir",
        default=None,
        help="Default: BACKUP_PATH env, or <data dir>/backups on a production device.",
    )
    b_create.add_argument("--no-cloud", action="store_true")
    b_drill = backup_sub.add_parser("restore-drill", help="Restore into a scratch DB and verify.")
    b_drill.add_argument("--file", required=True)
    b_drill.add_argument("--drill-database-url", required=True)
    backup_sub.add_parser("export-key", help="Print the backup key for OFFLINE safekeeping.")
    b_import = backup_sub.add_parser(
        "import-key",
        help="Import the offline recovery key (hidden prompt or --stdin; never an argument).",
    )
    b_import.add_argument(
        "--stdin", action="store_true", help="Read the key from stdin (automation)."
    )
    b_restore = backup_sub.add_parser(
        "restore",
        help="Safe full restore: staging cluster -> verify -> promote (rollback on failure).",
    )
    b_restore.add_argument("--file", required=True)
    b_restore.add_argument(
        "--pgdata",
        default=None,
        help="Live cluster data dir (default: <data dir>/pgdata).",
    )
    b_restore.add_argument("--live-port", type=int, default=5433)
    b_restore.add_argument("--staging-port", type=int, default=55433)

    b_branch = sub.add_parser("bootstrap-branch", help="Create the first branch.")
    b_branch.add_argument("--name", required=True)

    sub.add_parser("skeleton-demo-data", help="Seed one demo medication for the M12 hardware test.")

    b_sale = sub.add_parser("skeleton-sale", help="M12 slice: scan -> sale -> ESC/POS receipt.")
    b_sale.add_argument("--barcode", required=True)
    b_sale.add_argument("--qty", default="1")
    b_sale.add_argument("--print-host", help="Network ESC/POS printer IP (port 9100).")
    b_sale.add_argument("--out-file", default="receipt.escpos.bin")

    c_seed = sub.add_parser("catalog-seed", help="Seed/import catalog from CSV or XLSX.")
    c_seed.add_argument("--file", required=True)
    c_seed.add_argument("--source", default="seed", choices=["seed", "import"])

    inv = sub.add_parser("inventory", help="Inventory cache maintenance.")
    inv_sub = inv.add_subparsers(dest="inventory_command", required=True)
    inv_sub.add_parser("drift-check", help="Verify cached_quantity == SUM(active batches).")
    inv_sub.add_parser("rebuild-cache", help="Rebuild the derived cache from batch truth.")
    inv_sub.add_parser(
        "expiry-sweep", help="Mark past-expiry active batches as expired (cron-able)."
    )
    sub.add_parser(
        "alerts-evaluate",
        help="Evaluate ALERT_RULES idempotently for every branch (P3-M6, cron-able).",
    )
    sub.add_parser(
        "notifications-drain-email",
        help="Send queued email notifications via the configured provider (P3-M7, cron-able).",
    )
    sub.add_parser(
        "compliance-drain",
        help="Branch-wide ETA/EDA outbox drain (installer decision 8; cron-able, actor=None).",
    )
    dev_init = sub.add_parser(
        "device-init",
        help="First-run provisioning: DB password, initdb (SCRAM + builtin C.UTF-8), secrets.",
    )
    dev_init.add_argument("--pgdata", default=None, help="Default: <data dir>/pgdata.")
    sub.add_parser(
        "setup-status",
        help="First-run wizard state (users/branches/setup_complete) as JSON.",
    )
    sub.add_parser("setup-complete", help="Mark first-run setup complete.")
    dev_reset = sub.add_parser(
        "device-reset",
        help="Delete device keystore secrets (debug tool; --yes required).",
    )
    dev_reset.add_argument("--yes", action="store_true")
    m_cmd = sub.add_parser(
        "migrate",
        help="Apply pending SQL migrations + re-apply seeds (device path, asyncpg).",
    )
    m_cmd.add_argument("--database-url", default=None)
    m_cmd.add_argument("--migrations-dir", default=None)
    m_cmd.add_argument("--seeds-dir", default=None)

    args = parser.parse_args(argv)

    # One device-wide maintenance lock around every maintenance command —
    # a scheduled backup can never collide with a restore/migrate/seed (ق13).
    # Non-maintenance commands (bootstrap-*, skeleton-*) run without it.
    maintenance_commands = {
        "device-init",
        "backup",
        "migrate",
        "catalog-seed",
        "inventory",
        "alerts-evaluate",
        "compliance-drain",
        "notifications-drain-email",
    }
    try:
        if args.command in maintenance_commands:
            with maintenance_lock():
                return _dispatch(args)
        return _dispatch(args)
    except MaintenanceBusyError as exc:
        print(f"busy: {exc}", file=sys.stderr)
        return MAINTENANCE_BUSY_EXIT


def _dispatch(args: argparse.Namespace) -> int:  # noqa: C901 (flat CLI dispatch)
    if args.command == "bootstrap-admin":
        password = os.environ.get("PHARMAOS_ADMIN_PASSWORD") or getpass.getpass(
            "super_admin password: "
        )
        return asyncio.run(_bootstrap_admin(args.username, args.full_name, password))
    if args.command == "backup":
        if args.backup_command == "create":
            from pharmaos_api.services.backup_service import default_backup_dir

            backup_dir = Path(args.backup_dir) if args.backup_dir else default_backup_dir()
            return _backup_create(backup_dir, cloud=not args.no_cloud)
        if args.backup_command == "restore-drill":
            return _backup_restore_drill(Path(args.file), args.drill_database_url)
        if args.backup_command == "export-key":
            return _backup_export_key()
        if args.backup_command == "import-key":
            return _backup_import_key(use_stdin=args.stdin)
        if args.backup_command == "restore":
            from pharmaos_api.config import default_data_dir

            pgdata = Path(args.pgdata) if args.pgdata else default_data_dir() / "pgdata"
            return _backup_restore(Path(args.file), pgdata, args.live_port, args.staging_port)
    if args.command == "device-reset":
        if not args.yes:
            print("refusing: pass --yes (this deletes the device secrets).", file=sys.stderr)
            return 2
        return _device_reset()
    if args.command == "setup-status":
        return _setup_status_cmd()
    if args.command == "setup-complete":
        return _setup_complete()
    if args.command == "device-init":
        return _device_init(args.pgdata)
    if args.command == "bootstrap-branch":
        return asyncio.run(_bootstrap_branch(args.name))
    if args.command == "skeleton-demo-data":
        return asyncio.run(_skeleton_demo_data())
    if args.command == "inventory":
        cmd = {"rebuild-cache": "rebuild", "expiry-sweep": "expiry-sweep"}.get(
            args.inventory_command, "check"
        )
        return asyncio.run(_inventory_maintenance(cmd))
    if args.command == "alerts-evaluate":
        return asyncio.run(_alerts_evaluate())
    if args.command == "notifications-drain-email":
        return asyncio.run(_notifications_drain_email())
    if args.command == "compliance-drain":
        return asyncio.run(_compliance_drain())
    if args.command == "migrate":
        return _migrate(args.database_url, args.migrations_dir, args.seeds_dir)
    if args.command == "catalog-seed":
        return asyncio.run(_catalog_seed(args.file, args.source))
    if args.command == "skeleton-sale":
        return asyncio.run(_skeleton_sale(args.barcode, args.qty, args.print_host, args.out_file))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
