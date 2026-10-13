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


def test_lock1_entry_hash_vector() -> None:
    """LOCK-1 literal formula (P4 §1, D9): independent inline hmac over the
    hand-assembled message — never via chain_entry_hash. Catches any drift in
    domain / u64_be / prev_bytes / canonical_event."""
    import hashlib
    import hmac as hmac_mod
    import json as json_mod

    from pharmaos_api.licensing.event import LicenseClockEventV1 as Ev
    from pharmaos_api.licensing.event import chain_entry_hash

    key = bytes.fromhex("00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff")
    moment = datetime(2026, 10, 5, 12, 30, 45, tzinfo=UTC)
    event = Ev(
        event_type="boot_seen",
        origin="boot",
        observed_at=moment,
        high_water_utc=moment,
        ref="",
        anomaly_count=0,
    )
    canonical_event = json_mod.dumps(
        {
            "anomaly_count": 0,
            "event_type": "boot_seen",
            "high_water_utc": "2026-10-05T12:30:45Z",
            "observed_at": "2026-10-05T12:30:45Z",
            "origin": "boot",
            "ref": "",
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    msg1 = b"pharmaos.clock.chain.v1\n" + (1).to_bytes(8, "big") + b"" + canonical_event
    expected1 = hmac_mod.new(key, msg1, hashlib.sha256).hexdigest()
    assert expected1 == "6902c45edbb0b3422fa48199dc13c7576089ac9f232870b5f0dcdfcbc3591140"
    assert chain_entry_hash(key, 1, b"", event) == expected1
    msg2 = (
        b"pharmaos.clock.chain.v1\n"
        + (2).to_bytes(8, "big")
        + bytes.fromhex(expected1)
        + canonical_event
    )
    expected2 = hmac_mod.new(key, msg2, hashlib.sha256).hexdigest()
    assert expected2 == "b81f1045c7ebfbb0a7ae740faa894e3e7f678875dcc060fcc4860f1284c092ba"
    assert chain_entry_hash(key, 2, bytes.fromhex(expected1), event) == expected2


def test_lock2_ext_mac_vector() -> None:
    """LOCK-2 literal MAC (D9): independent HKDF + HMAC over hand-assembled
    canonical record — never via derive_ext_key/mac_record."""
    import hashlib
    import hmac as hmac_mod
    import json as json_mod

    from cryptography.hazmat.primitives import hashes as c_hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF as C_HKDF

    from pharmaos_api.licensing.external_stores import derive_ext_key, mac_record

    key = bytes.fromhex("00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff")
    ext_key = C_HKDF(
        algorithm=c_hashes.SHA256(), length=32, salt=b"", info=b"pharmaos.clock.ext.v1"
    ).derive(key)
    assert ext_key.hex() == "fcc2cdfe33aa6560831a852bbf84e7da30db094223620d04c22d51ad405d08b3"
    assert derive_ext_key(key) == ext_key
    record = {
        "head_hash": "6902c45edbb0b3422fa48199dc13c7576089ac9f232870b5f0dcdfcbc3591140",
        "head_seq": 1,
        "high_water": "2026-10-05T12:30:45Z",
        "last_activation_issued_at": None,
        "last_activation_license_id": None,
        "last_successful_sync_utc": "2026-10-05T12:30:45Z",
        "verified_from_seq": None,
    }
    canonical_record = json_mod.dumps(
        record, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    expected = hmac_mod.new(ext_key, canonical_record, hashlib.sha256).hexdigest()
    assert expected == "04e35959f8773a736b15922186972936f4ac269fc8f29358d7a5d6b3a140159e"
    assert mac_record(ext_key, record) == expected


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
