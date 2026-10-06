"""Central resolver for PostgreSQL client binaries (installer decision 6).

The device bundle ships PG17 binaries under <install>\\resources\\pg\\bin and
points PG_BIN_DIR at them; dev/CI resolves from PATH. ONE resolver for
backup/restore/migrate/diagnostics — never scattered per-call lookups — and
it exposes version parsing so a client/server mismatch is detectable (the
CI "pg_dump refuses a newer server" lesson, made structural).
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

from pharmaos_api.config import get_settings

# Everything the device runtime may need, resolved through this module only.
CLIENT_TOOLS = ("initdb", "pg_ctl", "pg_dump", "pg_restore", "pg_isready", "psql")

_VERSION_RE = re.compile(r"\(PostgreSQL\) (\d+)\.")


class PgBinaryNotFoundError(RuntimeError):
    """A required PostgreSQL binary is neither in PG_BIN_DIR nor on PATH."""


def configured_pg_bin_dir() -> Path | None:
    raw = get_settings().pg_bin_dir
    return Path(raw) if raw else None


def _exe_name(tool: str) -> str:
    return f"{tool}.exe" if os.name == "nt" else tool


def resolve(tool: str) -> Path:
    """Absolute path to a PostgreSQL client binary. PG_BIN_DIR (the bundled
    device binaries) wins over PATH; a configured dir that lacks the binary
    is an error, not a silent PATH fallback — the bundle must be complete."""
    if tool not in CLIENT_TOOLS:
        raise ValueError(f"unknown PostgreSQL tool: {tool}")
    override = configured_pg_bin_dir()
    if override is not None:
        candidate = override / _exe_name(tool)
        if candidate.is_file():
            return candidate
        raise PgBinaryNotFoundError(
            f"{tool} not found in PG_BIN_DIR ({override}) — the bundled "
            "PostgreSQL binaries are incomplete."
        )
    found = shutil.which(tool)
    if found:
        return Path(found)
    raise PgBinaryNotFoundError(
        f"{tool} not found on PATH — on a device, PG_BIN_DIR must point at "
        "the bundled PostgreSQL binaries."
    )


def version_major(tool_path: Path) -> int | None:
    """Major version of a PG binary, e.g. 'pg_dump (PostgreSQL) 17.2' -> 17."""
    result = subprocess.run(  # noqa: S603  (resolved path from this module)
        [str(tool_path), "--version"], capture_output=True, text=True, check=False
    )
    match = _VERSION_RE.search(result.stdout)
    return int(match.group(1)) if match else None
