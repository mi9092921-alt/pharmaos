"""Append-only clock-event chain (P4 §1 — LOCK-1/LOCK-1b).

Append protocol (the plan's binding order):
    BEGIN (READ COMMITTED, set explicitly — REPEATABLE READ snapshots before
    the lock is taken and would read a stale head)
    → pg_advisory_xact_lock(740029001)          # Python holds it BEFORE reading
    → SELECT last seq / entry_hash              # under the lock
    → build event → entry_hash = HMAC(...)      # over the just-read head
    → INSERT (NEW.seq = last+1, NEW.prev_hash = last_hash)
    → trigger re-acquires the same lock (no-op) and VALIDATES ONLY — it never
      rewrites NEW.seq / NEW.prev_hash
    → COMMIT

The trigger makes the ordering/linkage invariants un-bypassable (even via raw
SQL); hash AUTHENTICITY is enforced by re-computation in verify_chain — a
forged row with a well-linked but wrong hash is detected (E-LIC-006), and
crafting a chain that VERIFIES requires the keystore key.
"""

import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pharmaos_api.licensing.event import LicenseClockEventV1, chain_entry_hash
from pharmaos_api.licensing.payload import license_id_parts

ADVISORY_LOCK_ID = 740029001
GENESIS_SEQ = 1

HeadRow = tuple[int, str, datetime | None]

_HEAD_QUERY = text(
    "SELECT seq, entry_hash, high_water_utc FROM license_clock_events " "ORDER BY seq DESC LIMIT 1"
)


async def current_head(session: AsyncSession) -> HeadRow | None:
    """(seq, entry_hash, high_water_utc) of the newest row, or None on genesis."""
    head = (await session.execute(_HEAD_QUERY)).first()
    if head is None:
        return None
    high_water = _aware(head.high_water_utc)
    return int(head.seq), str(head.entry_hash), high_water


async def append_clock_event(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    clock_key: bytes,
    event: LicenseClockEventV1,
) -> tuple[int, str]:
    """Append one event under the double lock; returns (seq, entry_hash) — the
    new head, so callers can anchor external-store records without re-reading."""
    async with session_factory() as session, session.begin():
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
        await session.execute(
            text("SELECT pg_advisory_xact_lock(CAST(:lock_id AS BIGINT))"),
            {"lock_id": ADVISORY_LOCK_ID},
        )
        head = await current_head(session)
        if head is None:
            seq = GENESIS_SEQ
            prev_hash: str | None = None
            prev_hash_bytes = b""
        else:
            head_seq, head_hash, _ = head
            seq = head_seq + 1
            prev_hash = head_hash
            prev_hash_bytes = bytes.fromhex(head_hash)
        entry = chain_entry_hash(clock_key, seq, prev_hash_bytes, event)
        await session.execute(
            text(
                "INSERT INTO license_clock_events "
                "(seq, event_type, origin, observed_at, high_water_utc, ref, "
                " anomaly_count, prev_hash, entry_hash) "
                "VALUES (:seq, :event_type, :origin, :observed_at, :high_water_utc, "
                ":ref, :anomaly_count, :prev_hash, :entry_hash)"
            ),
            {
                "seq": seq,
                "event_type": event.event_type,
                "origin": event.origin,
                "observed_at": event.observed_at,
                "high_water_utc": event.high_water_utc,
                "ref": event.ref,
                "anomaly_count": event.anomaly_count,
                "prev_hash": prev_hash,
                "entry_hash": entry,
            },
        )
    return seq, entry


@dataclass(frozen=True, slots=True)
class ChainVerification:
    ok: bool
    rows_verified: int
    first_bad_seq: int | None
    head_seq: int | None
    head_hash: str | None
    head_high_water_utc: datetime | None
    last_activation_rank: tuple[datetime, int, int] | None
    db_high_water_utc: datetime | None


async def verify_chain(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    clock_key: bytes,
    verified_from_seq: int | None,
) -> ChainVerification:
    """Re-compute every row hash in the window (seq > anchor) and check the
    linkage between consecutive rows; the anchor row's own linkage to the
    pre-seal history is taken as given (that is what the seal means). With no
    anchor the window starts at the genesis row, which must have a NULL
    prev_hash. Head and last-activation reference rows are read regardless of
    the window (the key_lost rule reads the head; the new-vs-old baseline reads
    the last activation)."""
    async with session_factory() as session:
        head = await current_head(session)

        anchor_hash: str | None = None
        if verified_from_seq is not None:
            anchor = (
                await session.execute(
                    text("SELECT entry_hash FROM license_clock_events WHERE seq = :seq"),
                    {"seq": verified_from_seq},
                )
            ).first()
            anchor_hash = str(anchor.entry_hash) if anchor is not None else None

        rows = (
            await session.execute(
                text(
                    "SELECT seq, event_type, origin, observed_at, high_water_utc, ref, "
                    "anomaly_count, prev_hash, entry_hash FROM license_clock_events "
                    "WHERE (CAST(:anchor AS BIGINT) IS NULL AND seq >= :genesis) "
                    "OR (CAST(:anchor AS BIGINT) IS NOT NULL AND seq > CAST(:anchor AS BIGINT)) "
                    "ORDER BY seq ASC"
                ),
                {"anchor": verified_from_seq, "genesis": GENESIS_SEQ},
            )
        ).all()

        last_activation = (
            await session.execute(
                text(
                    "SELECT high_water_utc, ref FROM license_clock_events "
                    "WHERE event_type = 'activation' ORDER BY seq DESC LIMIT 1"
                )
            )
        ).first()

        db_high_water = (
            await session.execute(text("SELECT max(high_water_utc) FROM license_clock_events"))
        ).scalar_one_or_none()

    def _fail(bad_seq: int | None) -> ChainVerification:
        return ChainVerification(
            ok=False,
            rows_verified=0,
            first_bad_seq=bad_seq,
            head_seq=head[0] if head is not None else None,
            head_hash=head[1] if head is not None else None,
            head_high_water_utc=head[2] if head is not None else None,
            last_activation_rank=None,
            db_high_water_utc=_aware(db_high_water),
        )

    if verified_from_seq is not None and verified_from_seq > 0 and anchor_hash is None:
        return _fail(verified_from_seq)  # the anchor row itself vanished

    expected_prev = anchor_hash
    rows_verified = 0
    for row in rows:
        seq = int(row.seq)
        if seq == GENESIS_SEQ:
            if row.prev_hash is not None:
                return _fail(seq)
            prev_bytes = b""
        else:
            if row.prev_hash is None:
                return _fail(seq)
            prev_bytes = bytes.fromhex(str(row.prev_hash))
        if expected_prev is not None and str(row.prev_hash) != expected_prev:
            return _fail(seq)
        event = _event_from_row(row)
        if event is None:
            return _fail(seq)
        expected = chain_entry_hash(clock_key, seq, prev_bytes, event)
        if not hmac.compare_digest(expected, str(row.entry_hash)):
            return _fail(seq)
        expected_prev = str(row.entry_hash)
        rows_verified += 1

    last_activation_rank: tuple[datetime, int, int] | None = None
    if last_activation is not None and last_activation.high_water_utc is not None:
        license_id = str(last_activation.ref)
        issued_at = _aware(last_activation.high_water_utc)
        if issued_at is not None:
            last_activation_rank = (issued_at, *license_id_parts(license_id))

    return ChainVerification(
        ok=True,
        rows_verified=rows_verified,
        first_bad_seq=None,
        head_seq=head[0] if head is not None else None,
        head_hash=head[1] if head is not None else None,
        head_high_water_utc=head[2] if head is not None else None,
        last_activation_rank=last_activation_rank,
        db_high_water_utc=_aware(db_high_water),
    )


def _event_from_row(row: Any) -> LicenseClockEventV1 | None:
    from pharmaos_api.licensing.event import EVENT_TYPES, ORIGINS

    event_type, origin = str(row.event_type), str(row.origin)
    if event_type not in EVENT_TYPES or origin not in ORIGINS:
        return None
    observed = _aware(row.observed_at)
    high_water = _aware(row.high_water_utc)
    if observed is None or high_water is None:
        return None
    try:
        return LicenseClockEventV1(
            event_type=event_type,  # type: ignore[arg-type]  # membership-checked above
            origin=origin,  # type: ignore[arg-type]  # membership-checked above
            observed_at=observed,
            high_water_utc=high_water,
            ref=str(row.ref or ""),
            anomaly_count=int(row.anomaly_count),
        )
    except ValidationError:
        return None


def _aware(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def high_water_advanced_since_last_event(
    head_high_water_utc: datetime | None, new_high_water: datetime
) -> bool:
    """Growth policy (P4 §1): boot_seen rows are appended only when the
    high-water advanced ≥ 1 hour since the last event (~≤8,760 rows/year);
    incidents/activations/state changes always append."""
    if head_high_water_utc is None:
        return True
    return (new_high_water - head_high_water_utc) >= timedelta(hours=1)
