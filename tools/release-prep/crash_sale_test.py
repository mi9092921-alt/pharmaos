"""Release-prep crash-safety harness (Phase 1 of the go-live plan).

Simulates sudden power loss around a POS sale and verifies that the database
always recovers to a CONSISTENT state — never an invoice without its stock
deduction (or vice versa).

What it does per iteration:
  1. spawns a child process that runs sales_service.create_sale (the real
     atomic sale path: batches + stock_movements + invoice + items + audit,
     one transaction) with a realistic 10-line cart;
  2. waits for the TXN-START marker, sleeps a RANDOM delay covering the
     transaction window, then hard-kills:
       - "client" mode: only the child (the API process died);
       - "power"  mode: the child AND the PostgreSQL server tree
         (taskkill /F /T) — the closest thing to pulling the plug;
  3. restarts the cluster (power mode), waits for WAL recovery, then checks:
       - pg_amcheck (B-tree + heap integrity — PostgreSQL's integrity_check);
       - invoice/movement/ledger consistency SQL (c1..c5 below).

Invariants checked after EVERY iteration (all must hold):
  c1: no completed invoice without sale_out movements
  c2: no sale_out movement pointing at a missing invoice
  c3: per invoice: sum(movement deltas) == -sum(item qty_smallest)
  c4: branch_inventory.cached_quantity == SUM(active batch quantities)
  c5: stock ledger equation: SUM(batch quantities) == SUM(movement deltas)
      (receives +sales — every batch change writes exactly one movement in
      the same transaction, so the two sides can never legitimately diverge)

Usage:
  python tools/release-prep/crash_sale_test.py run --port 5434 --iterations 50
  python tools/release-prep/crash_sale_test.py child --port 5434   # internal

The harness ONLY ever touches its own dedicated cluster/database
(pharmaos_crash) — never live or test-shared data. Test data is clearly
fictional ("GEN-TEST-CRASH-*" medications).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import queue as queue_mod
import random
import statistics
import subprocess
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path

DB_NAME = "pharmaos_crash"
MARKER_BRANCH = "فرع اختبار الانقطاع"
MED_PREFIX = "GEN-TEST-CRASH-"
MARKER_USER = "crash_cashier"
RESULTS_DIR = Path(__file__).resolve().parent / "results"


def db_url(port: int, db: str = DB_NAME) -> str:
    return f"postgresql://postgres@127.0.0.1:{port}/{db}"


def scratch_dir() -> Path:
    """Harness-owned scratch area (pgdata + logs) — outside the repo, per-run
    disposable. Never points at live or project data."""
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Temp"
    return base / "pharmaos-relprep"


def pgdata_default() -> Path:
    return scratch_dir() / "pgcrash"


def pg_bin() -> Path:
    return Path(r"D:\projects\pharmaos\installer\vendor\pg\bin")


# ---------------------------------------------------------------------------
# child mode: one sale process (killed mid-flight by the runner)
# ---------------------------------------------------------------------------


def run_child(port: int) -> int:
    from sqlalchemy import select

    from pharmaos_api.db import get_session_factory
    from pharmaos_api.models import (
        Branch,
        Medication,
        MedicationBarcode,
        User,
    )
    from pharmaos_api.services.sales_service import SaleLine, create_sale

    async def _sale() -> None:
        factory = get_session_factory()
        async with factory() as session:
            branch = (
                await session.execute(select(Branch).where(Branch.name == MARKER_BRANCH))
            ).scalar_one()
            cashier = (
                await session.execute(select(User).where(User.username == MARKER_USER))
            ).scalar_one()
            # A REALISTIC multi-line cart: 10 random lines out of the seeded
            # medications — widens the transaction so a random kill can land
            # mid-flight (a single-line sale commits in tens of ms).
            barcodes = (
                (
                    await session.execute(
                        select(MedicationBarcode.barcode)
                        .join(Medication, Medication.id == MedicationBarcode.medication_id)
                        .where(Medication.trade_name.like(MED_PREFIX + "%"))
                    )
                )
                .scalars()
                .all()
            )
            picked = random.sample(sorted(barcodes), 10)
            lines = [SaleLine(barcode=b, quantity=Decimal(random.randint(1, 5))) for b in picked]
            print("TXN-START", flush=True)
            t0 = time.perf_counter()
            await create_sale(
                session,
                branch_id=branch.id,
                lines=lines,
                cashier=cashier,
            )
            print(f"TXN-COMMIT {time.perf_counter() - t0:.4f}", flush=True)

    asyncio.run(_sale())
    return 0


# ---------------------------------------------------------------------------
# seed: fictional branch/medications/stock (fresh DB per harness run)
# ---------------------------------------------------------------------------


async def _seed_async(port: int) -> None:
    import datetime as dt

    from sqlalchemy import select, text

    from pharmaos_api.db import get_session_factory
    from pharmaos_api.models import (
        Branch,
        Medication,
        MedicationBarcode,
        MedicationPackaging,
        Role,
        User,
    )
    from pharmaos_api.security.passwords import hash_password

    factory = get_session_factory()
    async with factory() as session:
        role = (
            await session.execute(select(Role).where(Role.code == "cashier"))
        ).scalar_one()
        session.add(
            User(
                username=MARKER_USER,
                full_name="كاشير اختبار الانقطاع (بيانات وهمية)",
                password_hash=hash_password("Cr4sh@Test!"),
                role_id=role.id,
            )
        )
        branch = Branch(name=MARKER_BRANCH, country_code="EG", currency_code="EGP")
        session.add(branch)
        await session.flush()

        unit_ids = {}
        for name_ar in ("علبة", "شريط", "قرص"):
            unit_ids[name_ar] = (
                await session.execute(
                    text(
                        "INSERT INTO units (name_ar) VALUES (:n) "
                        "ON CONFLICT (name_ar) DO UPDATE SET name_ar=EXCLUDED.name_ar "
                        "RETURNING id"
                    ).bindparams(n=name_ar)
                )
            ).scalar_one()

        # 20 fictional medications so each sale is a realistic 10-line cart.
        for i in range(20):
            med = Medication(
                trade_name=f"{MED_PREFIX}{i:02d}",
                trade_name_ar=f"دواء اختبار انقطاع {i:02d} (وهمي تماماً)",
            )
            session.add(med)
            await session.flush()
            strip = MedicationPackaging(
                medication_id=med.id,
                level=2,
                unit_id=unit_ids["شريط"],
                name_ar="شريط",
                qty_in_parent=Decimal(3),
                selling_price=Decimal("30.00"),
                is_default_sale=True,
            )
            session.add_all(
                [
                    MedicationPackaging(
                        medication_id=med.id,
                        level=1,
                        unit_id=unit_ids["علبة"],
                        name_ar="علبة",
                        qty_in_parent=None,
                        selling_price=Decimal("90.00"),
                    ),
                    strip,
                    MedicationPackaging(
                        medication_id=med.id,
                        level=3,
                        unit_id=unit_ids["قرص"],
                        name_ar="قرص",
                        qty_in_parent=Decimal(10),
                        selling_price=Decimal("3.50"),
                    ),
                ]
            )
            await session.flush()
            session.add(
                MedicationBarcode(
                    medication_id=med.id,
                    packaging_id=strip.id,
                    barcode=f"6229900000{i:02d}",
                    is_primary=True,
                )
            )
        await session.commit()

    # Stock enters through the REAL receive primitive (batch row + purchase_in
    # movement + cache delta, one transaction) — never a raw INSERT, so the
    # derived cache starts consistent exactly like a live device.
    from pharmaos_api.services.inventory_service import receive_batch

    async with factory() as session:
        branch = (
            await session.execute(select(Branch).where(Branch.name == MARKER_BRANCH))
        ).scalar_one()
        meds = (
            (
                await session.execute(
                    select(Medication)
                    .where(Medication.trade_name.like(MED_PREFIX + "%"))
                    .order_by(Medication.trade_name)
                )
            )
            .scalars()
            .all()
        )
        actor = (
            await session.execute(select(User).where(User.username == MARKER_USER))
        ).scalar_one()
        for i, med in enumerate(meds):
            await receive_batch(
                session,
                actor=actor,
                branch_id=branch.id,
                medication_id=med.id,
                batch_number=f"CRASH-B{i:02d}",
                expiry_date=dt.date.today() + dt.timedelta(days=720),
                quantity=Decimal(10_000_000),
                purchase_price=Decimal("2.00"),
            )
        await session.commit()
    print(f"seeded fictional crash-test data ({len(meds)} meds via receive_batch)")


async def _reset_db_async(port: int) -> None:
    import asyncpg

    conn = await asyncpg.connect(db_url(port, "postgres"))
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{DB_NAME}"')
    finally:
        await conn.close()


async def _install_amcheck_async(port: int) -> None:
    """Install PostgreSQL's integrity-check extension in the throwaway DB.

    ``pg_amcheck`` skips databases without the ``amcheck`` extension — the
    crash harness owns this fresh database, so enabling it here makes the
    integrity gate real instead of a false failure on every iteration."""
    import asyncpg

    conn = await asyncpg.connect(db_url(port))
    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS amcheck")
    finally:
        await conn.close()


def reset_db(port: int) -> None:
    """Pristine database per harness run: no cross-run state contaminates
    the ledger-equation invariants."""
    asyncio.run(_reset_db_async(port))
    from pharmaos_api.migrations_runner import run_migrations

    applied = run_migrations(db_url(port))
    asyncio.run(_install_amcheck_async(port))
    print(f"migrations applied fresh: {len(applied['applied'])} (seeds: {applied['seeds']})")


def seed(port: int) -> None:
    os.environ["PHARMAOS_ENV"] = "test"
    os.environ["DATABASE_URL"] = db_url(port)
    asyncio.run(_seed_async(port))


# ---------------------------------------------------------------------------
# postgres process control
# ---------------------------------------------------------------------------


def kill_tree(pid: int) -> None:
    subprocess.run(  # noqa: S603
        ["taskkill", "/F", "/T", "/PID", str(pid)],
        capture_output=True,
        check=False,
    )


def stop_cluster_pgdata(pgdata: Path) -> None:
    pid_file = pgdata / "postmaster.pid"
    if pid_file.is_file():
        try:
            pid = int(pid_file.read_text().splitlines()[0])
            kill_tree(pid)
        except (ValueError, OSError):
            pass


def start_cluster(pgdata: Path, logfile: Path, port: int) -> None:
    # No captured pipes: pg_ctl detaches postgres.exe which inherits handles
    # and hangs communicate() (documented in backup_service._pg_ctl_start).
    subprocess.run(  # noqa: S603
        [str(pg_bin() / "pg_ctl.exe"), "-D", str(pgdata), "-l", str(logfile), "start"],
        check=False,
        timeout=120,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(120):
        probe = subprocess.run(  # noqa: S603
            [str(pg_bin() / "pg_isready.exe"), "-h", "127.0.0.1", "-p", str(port)],
            capture_output=True,
            check=False,
            timeout=10,
        )
        if probe.returncode == 0:
            return
        time.sleep(0.5)
    raise RuntimeError("crash cluster did not become ready in 60s")


# ---------------------------------------------------------------------------
# consistency checks
# ---------------------------------------------------------------------------

# All five invariants take $1 = branch_id and must return 0.
CHECK_QUERIES: list[tuple[str, str]] = [
    (
        "c1_invoice_without_movements",
        """SELECT count(*) FROM invoices i
           WHERE i.branch_id = $1 AND i.is_deleted = FALSE
             AND NOT EXISTS (SELECT 1 FROM stock_movements m
                             WHERE m.reference_id = i.id AND m.movement_type = 'sale_out')""",
    ),
    (
        "c2_movement_without_invoice",
        """SELECT count(*) FROM stock_movements m
           WHERE m.branch_id = $1 AND m.movement_type = 'sale_out'
             AND m.reference_type = 'invoice' AND m.reference_id IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM invoices i WHERE i.id = m.reference_id)""",
    ),
    (
        "c3_invoice_movement_mismatch",
        """SELECT count(*) FROM (
               SELECT i.id FROM invoices i
               LEFT JOIN stock_movements m
                 ON m.reference_id = i.id AND m.movement_type = 'sale_out'
               WHERE i.branch_id = $1 AND i.is_deleted = FALSE
               GROUP BY i.id
               HAVING COALESCE(SUM(m.quantity_delta), 0)
                      <> -COALESCE((SELECT SUM(ii.qty_smallest) FROM invoice_items ii
                                    WHERE ii.invoice_id = i.id), 0)
           ) x""",
    ),
    (
        "c4_inventory_cache_drift",
        """SELECT count(*) FROM branch_inventory bi
           WHERE bi.branch_id = $1
             AND bi.cached_quantity
                 <> (SELECT COALESCE(SUM(b.quantity), 0) FROM medication_batches b
                     WHERE b.branch_id = bi.branch_id AND b.medication_id = bi.medication_id
                       AND b.is_deleted = FALSE)""",
    ),
    (
        "c5_batch_ledger_equation",
        """SELECT count(*) FROM (
               SELECT (SELECT COALESCE(SUM(b.quantity), 0) FROM medication_batches b
                       WHERE b.branch_id = $1 AND b.is_deleted = FALSE) AS stock,
                      (SELECT COALESCE(SUM(m.quantity_delta), 0) FROM stock_movements m
                       WHERE m.branch_id = $1) AS moved
           ) led
           WHERE led.stock <> led.moved""",
    ),
]


async def _checks_async(port: int) -> dict[str, int]:
    import asyncpg

    conn = await asyncpg.connect(db_url(port))
    try:
        branch_id = await conn.fetchval(
            "SELECT id FROM branches WHERE name = $1", MARKER_BRANCH
        )
        results: dict[str, int] = {}
        for name, sql in CHECK_QUERIES:
            results[name] = int(await conn.fetchval(sql, branch_id))
        results["invoice_count"] = int(
            await conn.fetchval(
                "SELECT count(*) FROM invoices WHERE branch_id = $1", branch_id
            )
        )
        return results
    finally:
        await conn.close()


def run_checks(port: int) -> dict[str, int]:
    return asyncio.run(_checks_async(port))


def run_amcheck(port: int, db: str) -> tuple[bool, str]:
    proc = subprocess.run(  # noqa: S603
        [
            str(pg_bin() / "pg_amcheck.exe"),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--username",
            "postgres",
            "--database",
            db,
            "--heapallindexed",
            "--verbose",
        ],
        capture_output=True,
        check=False,
        timeout=300,
    )
    out = (proc.stdout + proc.stderr).decode(errors="replace")
    return proc.returncode == 0, out.strip()[:500]


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------


def spawn_child(port: int) -> tuple[subprocess.Popen[str], "queue_mod.Queue[str]"]:
    env = os.environ.copy()
    env["PHARMAOS_ENV"] = "test"
    env["DATABASE_URL"] = db_url(port)
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, str(Path(__file__).resolve()), "child", "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )
    queue: "queue_mod.Queue[str]" = queue_mod.Queue()

    def _pump() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            queue.put(line.strip())

    threading.Thread(target=_pump, daemon=True).start()
    return proc, queue


def measure_commit_time(port: int, runs: int = 3) -> float:
    times: list[float] = []
    for _ in range(runs):
        proc, queue = spawn_child(port)
        for _ in range(600):
            line = queue.get(timeout=120)
            if line.startswith("TXN-COMMIT"):
                times.append(float(line.split()[1]))
                break
        else:  # pragma: no cover
            raise RuntimeError("child never reported TXN-COMMIT")
        proc.wait(timeout=30)
    median = statistics.median(times)
    print(f"clean sale commit times: {[f'{t:.3f}s' for t in times]} (median {median:.3f}s)")
    return median


def run_harness(args: argparse.Namespace) -> int:
    pgdata: Path = Path(args.pgdata)
    logfile = pgdata.parent / "pgcrash.log"
    reset_db(args.port)
    seed(args.port)

    commit_s = measure_commit_time(args.port)
    window_lo, window_hi = 0.005, commit_s * 1.4
    print(f"kill window: {window_lo:.3f}s .. {window_hi:.3f}s (random uniform)")

    RESULTS_DIR.mkdir(exist_ok=True)
    results_path = RESULTS_DIR / f"crash_results_{time.strftime('%Y%m%dT%H%M%S')}.json"
    results: list[dict[str, object]] = []
    failures = 0

    for i in range(1, args.iterations + 1):
        mode = "power" if i % 2 == 1 else "client"
        delay = random.uniform(window_lo, window_hi)

        invoices_before = run_checks(args.port)["invoice_count"]
        proc, queue = spawn_child(args.port)
        marker = queue.get(timeout=120)
        if marker != "TXN-START":
            kill_tree(proc.pid)
            raise RuntimeError(f"child output before marker: {marker!r}")
        time.sleep(delay)

        recovery_logged = False
        if mode == "power":
            kill_tree(proc.pid)
            stop_cluster_pgdata(pgdata)
            start_cluster(pgdata, logfile, args.port)
            with logfile.open("r", encoding="utf-8", errors="replace") as fh:
                tail = fh.read()[-4000:]
            recovery_logged = "automatic recovery in progress" in tail
        else:
            kill_tree(proc.pid)

        checks = run_checks(args.port)
        ok_am, am_out = run_amcheck(args.port, DB_NAME)
        committed = checks["invoice_count"] > invoices_before
        consistent = all(
            checks[name] == 0
            for name in (
                "c1_invoice_without_movements",
                "c2_movement_without_invoice",
                "c3_invoice_movement_mismatch",
                "c4_inventory_cache_drift",
                "c5_batch_ledger_equation",
            )
        )
        ok = consistent and ok_am
        failures += 0 if ok else 1
        record = {
            "iteration": i,
            "mode": mode,
            "delay_s": round(delay, 3),
            "committed": committed,
            "consistent": consistent,
            "amcheck_ok": ok_am,
            "wal_recovery_logged": recovery_logged,
            "checks": checks,
            "amcheck_output": am_out if not ok_am else "",
        }
        results.append(record)
        print(
            f"[{i:>3}/{args.iterations}] mode={mode:<6} delay={delay:.3f}s "
            f"committed={'Y' if committed else 'N'} consistent={'Y' if consistent else 'N'} "
            f"amcheck={'Y' if ok_am else 'N'} recovery_log={'Y' if recovery_logged else 'n/a'}"
        )
        if not ok:
            print(f"  FAILED checks: {record['checks']} amcheck: {am_out}")

    committed_n = sum(1 for r in results if r["committed"])
    recovered_n = sum(1 for r in results if r["wal_recovery_logged"])
    print("=" * 70)
    print(
        f"iterations={args.iterations} failures={failures} "
        f"committed_after_kill={committed_n} killed_before_commit={args.iterations - committed_n} "
        f"pg_crash_recoveries={recovered_n}"
    )
    print(f"results written to {results_path}")
    results_path.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0 if failures == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="PharmaOS sale crash-safety harness")
    sub = parser.add_subparsers(dest="mode", required=True)
    child = sub.add_parser("child", help="internal: run exactly one sale")
    child.add_argument("--port", type=int, default=5434)
    runner = sub.add_parser("run", help="run the kill/restart/verify loop")
    runner.add_argument("--port", type=int, default=5434)
    runner.add_argument("--iterations", type=int, default=50)
    runner.add_argument("--pgdata", default=str(pgdata_default()))
    args = parser.parse_args()
    if args.mode == "child":
        return run_child(args.port)
    return run_harness(args)


if __name__ == "__main__":
    raise SystemExit(main())
