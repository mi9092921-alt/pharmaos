"""Canonical JSON serialization contract (P4 §2) — the single API-side
implementation. tools/license-cli reproduces it byte-identically; the committed
cross-vector tests prove the two implementations agree.

Rules (frozen): UTF-8, sort_keys=True, compact separators, ensure_ascii=False,
built from VALIDATED models only — never from raw user-controlled dicts.
"""

import json
from datetime import UTC, datetime
from typing import Any


class DuplicateKeyError(ValueError):
    """A JSON object contained the same key twice — rejected before any
    validation (P4 §2: never rely on json.loads' last-wins default)."""


def _no_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def canonical_json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def loads_strict(raw: bytes | str) -> Any:
    """json.loads with duplicate-key rejection (P4 §2)."""
    return json.loads(raw, object_pairs_hook=_no_duplicate_pairs)


def iso_z(value: datetime) -> str:
    """ISO-8601 UTC with a literal `Z` suffix — the only timestamp rendering
    allowed inside signed payloads, chain events, and store records."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
