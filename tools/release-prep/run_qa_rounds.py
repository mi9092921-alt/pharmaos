"""QA crash rounds runner (Phase 1): launches the guarded harness in the
background and polls its JSON results file. Avoids the 30s tool timeout.
Usage: python tools/release-prep/run_qa_rounds.py --iterations 50
Requires the same QA_* env contract as crash_sale_test.py.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=50)
    parser.add_argument("--poll-seconds", type=float, default=10.0)
    parser.add_argument("--timeout-minutes", type=float, default=55.0)
    args = parser.parse_args()

    repo = Path(__file__).resolve().parents[2]
    results_dir = Path(os.environ["QA_RESULTS_DIR"])
    before = {p.name for p in results_dir.glob("crash_results_*.json")} if results_dir.exists() else set()

    cmd = [
        sys.executable,
        str(repo / "tools" / "release-prep" / "crash_sale_test.py"),
        "run",
        "--iterations",
        str(args.iterations),
    ]
    log_path = results_dir / "qa_rounds_runner.log"
    results_dir.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(cmd, cwd=str(repo), stdout=log, stderr=subprocess.STDOUT, env=os.environ.copy())
    print(f"launched pid={proc.pid} log={log_path}")
    deadline = time.monotonic() + args.timeout_minutes * 60
    found: Path | None = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        current = {p.name for p in results_dir.glob("crash_results_*.json")}
        fresh = sorted(current - before)
        if fresh:
            found = results_dir / fresh[-1]
            break
        time.sleep(args.poll_seconds)
    if found is None:
        current = {p.name for p in results_dir.glob("crash_results_*.json")}
        fresh = sorted(current - before)
        if fresh:
            found = results_dir / fresh[-1]
    if found is None:
        print(f"no results file yet; runner rc={proc.poll()} log={log_path}")
        return 2
    rows = json.loads(found.read_text(encoding="utf-8"))
    failures = sum(1 for r in rows if not (r.get("consistent") and r.get("amcheck_ok")))
    print(f"results={found} iterations={len(rows)} failures={failures} runner_rc={proc.poll()}")
    for r in rows:
        if not (r.get("consistent") and r.get("amcheck_ok")):
            print(f"FAILED iter={r.get('iteration')} {r.get('checks')}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
