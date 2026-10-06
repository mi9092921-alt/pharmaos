"""Chain append protocol (P4 §1 — LOCK-1/LOCK-1b): concurrency serialization,
rollback safety, and forged-row detection. Runs on its OWN fresh scratch DB so
the absolute assertions hold regardless of test order."""

import asyncio
import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from pharmaos_api.licensing.chain import (
    append_clock_event,
    verify_chain,
)
from pharmaos_api.licensing.event import LicenseClockEventV1


def _event(now: datetime, ref: str = "") -> LicenseClockEventV1:
    return LicenseClockEventV1(
        event_type="boot_seen",
        origin="boot",
        observed_at=now,
        high_water_utc=now,
        ref=ref,
        anomaly_count=0,
    )


async def test_fresh_db_genesis_and_n50_concurrent(chain_scratch) -> None:  # type: ignore[no-untyped-def]
    factory = chain_scratch.session_factory()
    key = os.urandom(32)
    now = datetime.now(UTC)

    # absolute: the fresh DB starts with an EMPTY chain
    virgin = await verify_chain(factory, clock_key=key, verified_from_seq=None)
    assert virgin.ok
    assert virgin.head_seq is None

    # N=50 concurrent appends ⇒ exactly 50 rows, seq 1..50, no gaps,
    # every prev_hash points at the preceding row, every hash verifies
    results = await asyncio.gather(
        *(
            append_clock_event(factory, clock_key=key, event=_event(now, ref=str(i)))
            for i in range(50)
        )
    )
    seqs = sorted(seq for seq, _ in results)
    assert seqs == list(range(1, 51))

    verification = await verify_chain(factory, clock_key=key, verified_from_seq=None)
    assert verification.ok
    assert verification.rows_verified == 50
    assert verification.head_seq == 50


async def test_transaction_rollback_leaves_no_gap(chain_scratch) -> None:  # type: ignore[no-untyped-def]
    factory = chain_scratch.session_factory()
    key = os.urandom(32)
    now = datetime.now(UTC)

    head_before = await _head_seq(factory)

    # a transaction that takes the lock, reads the head, then dies before
    # INSERT — the advisory lock releases with the rollback, and the next
    # append must reuse the same seq (no gap burned)
    with pytest.raises(RuntimeError, match="simulated crash"):
        async with factory() as session:
            async with session.begin():
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": 740029001}
                )
                await _head_seq_factory(session)
                raise RuntimeError("simulated crash")

    seq, _ = await append_clock_event(factory, clock_key=key, event=_event(now))
    assert seq == head_before + 1  # no gap

    # the window anchored at the pre-crash head verifies (the new row only —
    # earlier rows were hashed by earlier keys and live behind the anchor)
    verification = await verify_chain(factory, clock_key=key, verified_from_seq=head_before)
    assert verification.ok


async def test_forged_row_is_detected_and_reseal_heals(chain_scratch) -> None:  # type: ignore[no-untyped-def]
    """LOCK-1b: an INSERT with correct linkage but a WRONG hash (anyone with
    INSERT) is structurally accepted by the DB but DETECTED at verification;
    a new-file activation's re-seal moves the window past the damage."""
    factory = chain_scratch.session_factory()
    key = os.urandom(32)
    now = datetime.now(UTC)

    # ensure at least one genuine row exists to forge against (order-independence)
    if await _head_seq(factory) == 0:
        await append_clock_event(factory, clock_key=key, event=_event(now))
    head_seq, head_hash = await _head(factory)

    # raw-SQL forgery: linkage correct (seq = head+1, prev_hash = head hash) so
    # the DB trigger ACCEPTS it — but the HMAC is fake (LOCK-1 violated)
    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO license_clock_events "
                "(seq, event_type, origin, observed_at, high_water_utc, ref, anomaly_count, "
                " prev_hash, entry_hash) "
                "VALUES (:seq, 'boot_seen', 'boot', NOW(), NOW(), 'forged', 0, :prev, :hash)"
            ),
            {"seq": head_seq + 1, "prev": head_hash, "hash": "f" * 64},
        )
        await session.commit()  # the forgery must SURVIVE to prove detection

    forged_seq = head_seq + 1
    # the window anchored at the pre-forgery head contains exactly the forged
    # row — its structure is valid but its HMAC is fake ⇒ detected there
    broken = await verify_chain(factory, clock_key=key, verified_from_seq=head_seq)
    assert not broken.ok
    assert broken.first_bad_seq == forged_seq

    # re-seal (what a new-file activation does): anchor := the forged head —
    # the verification window moves past the damage
    healed = await verify_chain(factory, clock_key=key, verified_from_seq=forged_seq)
    assert healed.ok

    # and the chain continues cleanly from the forged head's hash
    seq, _ = await append_clock_event(factory, clock_key=key, event=_event(now))
    assert seq == forged_seq + 1
    after = await verify_chain(factory, clock_key=key, verified_from_seq=forged_seq)
    assert after.ok
    assert after.rows_verified == 1


async def test_wrong_key_fails_verification(chain_scratch) -> None:  # type: ignore[no-untyped-def]
    factory = chain_scratch.session_factory()
    now = datetime.now(UTC)
    await append_clock_event(factory, clock_key=os.urandom(32), event=_event(now))
    # a different key cannot verify any row — authenticity boundary
    stranger = await verify_chain(factory, clock_key=os.urandom(32), verified_from_seq=None)
    assert not stranger.ok


async def _head_seq(factory) -> int:  # type: ignore[no-untyped-def]
    async with factory() as session:
        row = (
            await session.execute(text("SELECT COALESCE(max(seq), 0) FROM license_clock_events"))
        ).scalar()
        return int(row)


async def _head_seq_factory(session) -> int:  # type: ignore[no-untyped-def]
    row = (
        await session.execute(text("SELECT COALESCE(max(seq), 0) FROM license_clock_events"))
    ).scalar()
    return int(row)


async def _head(factory) -> tuple[int, str]:  # type: ignore[no-untyped-def]
    async with factory() as session:
        row = (
            await session.execute(
                text("SELECT seq, entry_hash FROM license_clock_events ORDER BY seq DESC LIMIT 1")
            )
        ).first()
        assert row is not None
        return int(row.seq), str(row.entry_hash)
