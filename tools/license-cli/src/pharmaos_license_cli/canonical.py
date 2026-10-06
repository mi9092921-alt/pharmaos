"""Canonical JSON contract — the byte-identical twin of
pharmaos_api.licensing.canonical (P4 §2). The committed cross-vector tests
prove both implementations produce identical bytes for identical payloads."""

import json
from datetime import UTC, datetime
from typing import Any


class DuplicateKeyError(ValueError):
    """A JSON object contained the same key twice."""


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
    return json.loads(raw, object_pairs_hook=_no_duplicate_pairs)


def iso_z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
