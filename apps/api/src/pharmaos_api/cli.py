"""Operational CLI.

bootstrap-admin: create the first super_admin user (Phase 0 acceptance:
"clean copy -> docker compose up -> migrate -> super_admin login").

The super_admin ROLE row is upserted here by code (the same code-defined
role the M7 seeder maintains) — never created by hand in the DB.
Credentials come from arguments/environment — never hardcoded (forbidden #4).

backup create / backup restore-drill / backup export-key: operational entry
points for the encrypted-backup subsystem (M9). export-key prints the backup
key ONCE for the owner to store OFFLINE — it is the recovery root.

Usage:
    python -m pharmaos_api.cli bootstrap-admin --username <name> --full-name <name>
    (password via PHARMAOS_ADMIN_PASSWORD env var or interactive prompt)
    python -m pharmaos_api.cli backup create [--backup-dir PATH] [--no-cloud]
    python -m pharmaos_api.cli backup restore-drill --file PATH --drill-database-url URL
    python -m pharmaos_api.cli backup export-key
"""

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

from sqlalchemy import select

from pharmaos_api.db import get_session_factory
from pharmaos_api.models import Role, User
from pharmaos_api.security.passwords import hash_password, validate_password_policy

SUPER_ADMIN_ROLE_CODE = "super_admin"
SUPER_ADMIN_ROLE_NAME_AR = "مالك النظام"


async def _bootstrap_admin(username: str, full_name: str, password: str) -> int:
    violations = validate_password_policy(password)
    if violations:
        print(f"password policy violations: {', '.join(violations)}", file=sys.stderr)
        return 2

    async with get_session_factory()() as session:
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
    import json as _json

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pharmaos-api")
    sub = parser.add_subparsers(dest="command", required=True)

    boot = sub.add_parser("bootstrap-admin", help="Create the first super_admin user.")
    boot.add_argument("--username", required=True)
    boot.add_argument("--full-name", required=True)

    backup = sub.add_parser("backup", help="Encrypted backup operations.")
    backup_sub = backup.add_subparsers(dest="backup_command", required=True)
    b_create = backup_sub.add_parser("create", help="Create an encrypted backup now.")
    b_create.add_argument("--backup-dir", default=os.environ.get("BACKUP_PATH", "./backups"))
    b_create.add_argument("--no-cloud", action="store_true")
    b_drill = backup_sub.add_parser("restore-drill", help="Restore into a scratch DB and verify.")
    b_drill.add_argument("--file", required=True)
    b_drill.add_argument("--drill-database-url", required=True)
    backup_sub.add_parser("export-key", help="Print the backup key for OFFLINE safekeeping.")

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

    args = parser.parse_args(argv)
    if args.command == "bootstrap-admin":
        password = os.environ.get("PHARMAOS_ADMIN_PASSWORD") or getpass.getpass(
            "super_admin password: "
        )
        return asyncio.run(_bootstrap_admin(args.username, args.full_name, password))
    if args.command == "backup":
        if args.backup_command == "create":
            return _backup_create(Path(args.backup_dir), cloud=not args.no_cloud)
        if args.backup_command == "restore-drill":
            return _backup_restore_drill(Path(args.file), args.drill_database_url)
        if args.backup_command == "export-key":
            return _backup_export_key()
    if args.command == "bootstrap-branch":
        return asyncio.run(_bootstrap_branch(args.name))
    if args.command == "skeleton-demo-data":
        return asyncio.run(_skeleton_demo_data())
    if args.command == "inventory":
        cmd = {"rebuild-cache": "rebuild", "expiry-sweep": "expiry-sweep"}.get(
            args.inventory_command, "check"
        )
        return asyncio.run(_inventory_maintenance(cmd))
    if args.command == "catalog-seed":
        return asyncio.run(_catalog_seed(args.file, args.source))
    if args.command == "skeleton-sale":
        return asyncio.run(_skeleton_sale(args.barcode, args.qty, args.print_host, args.out_file))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
