"""QA crash rounds runner (Phase 1): launches the guarded harness in the
background and waits for it to finish. Avoids the 30s tool timeout.
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

    if args.iterations < 1:
        parser.error("--iterations must be greater than zero")
    if args.poll_seconds <= 0 or args.timeout_minutes <= 0:
        parser.error("--poll-seconds and --timeout-minutes must be greater than zero")

    repo = Path(__file__).resolve().parents[2]
    results_dir_value = os.environ.get("QA_RESULTS_DIR", "").strip()
    if not results_dir_value:
        parser.error("QA_RESULTS_DIR is required")
    results_dir = Path(results_dir_value)
    if not results_dir.is_absolute():
        parser.error("QA_RESULTS_DIR must be an absolute path")
    before = (
        {p.name for p in results_dir.glob("crash_results_*.json")}
        if results_dir.exists()
        else set()
    )

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
        proc = subprocess.Popen(
            cmd,
            cwd=str(repo),
            stdout=log,
            stderr=subprocess.STDOUT,
            env=os.environ.copy(),
        )
    print(f"launched pid={proc.pid} log={log_path}")
    deadline = time.monotonic() + args.timeout_minutes * 60
    while time.monotonic() < deadline:
        return_code = proc.poll()
        if return_code is not None:
            break
        time.sleep(args.poll_seconds)
    else:
        print(
            f"runner exceeded timeout; process remains active pid={proc.pid} log={log_path}"
        )
        return 2

    current = {p.name for p in results_dir.glob("crash_results_*.json")}
    fresh = sorted(current - before)
    if not fresh:
        print(f"no results file; runner rc={return_code} log={log_path}")
        return 2
    found = results_dir / fresh[-1]
    rows = json.loads(found.read_text(encoding="utf-8"))
    failures = sum(1 for r in rows if not (r.get("consistent") and r.get("amcheck_ok")))
    incomplete = len(rows) != args.iterations
    print(
        f"results={found} iterations={len(rows)} expected={args.iterations} "
        f"failures={failures} runner_rc={return_code}"
    )
    for r in rows:
        if not (r.get("consistent") and r.get("amcheck_ok")):
            print(f"FAILED iter={r.get('iteration')} {r.get('checks')}")
    return 0 if failures == 0 and not incomplete and return_code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
