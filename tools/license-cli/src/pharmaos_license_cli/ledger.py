"""Issue ledger — the owner's local CRM (P4: `list --expiring-soon`)."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pharmaos_license_cli.payload import license_id_parts

LEDGER_KEYS = frozenset(
    {
        "license_id",
        "customer",
        "hwid",
        "kind",
        "issued_at",
        "valid_until",
        "features",
        "file",
        "kid",
        "created_at",
    }
)


def append_entry(ledger_path: Path, entry: dict[str, Any]) -> None:
    if set(entry) != LEDGER_KEYS:
        raise ValueError("ledger entry keys drifted from the frozen set")
    ledger_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n")


def read_entries(ledger_path: Path) -> list[dict[str, Any]]:
    if not ledger_path.is_file():
        return []
    entries: list[dict[str, Any]] = []
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entries.append(json.loads(line))
    return entries


def issued_numbers(entries: list[dict[str, Any]]) -> list[int]:
    numbers: list[int] = []
    for entry in entries:
        license_id = entry.get("license_id", "")
        try:
            _, number = license_id_parts(str(license_id))
        except ValueError:
            continue
        numbers.append(number)
    return numbers


def expiring_soon(
    entries: list[dict[str, Any]], days: int, *, now: datetime | None = None
) -> list[dict[str, Any]]:
    moment = now or datetime.now(UTC)
    horizon = moment + timedelta(days=days)
    result: list[dict[str, Any]] = []
    for entry in entries:
        valid_until = entry.get("valid_until")
        if not isinstance(valid_until, str):
            continue
        try:
            expires = datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
        except ValueError:
            continue
        if expires <= horizon:
            result.append(entry)
    return sorted(result, key=lambda entry: str(entry.get("valid_until")))
