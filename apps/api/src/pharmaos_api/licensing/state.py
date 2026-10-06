"""Boot evaluation + multi-source reconciliation (P4 §3).

Classification order:
  * tamper (immediate, E-LIC-006 family): external head AHEAD of the DB (tail
    truncation), a store that was SYNCED BEFORE but is now ABSENT (wiped —
    distinguishable via external_sync_state, which is why a fresh install never
    false-positives), any MAC/parse failure, a stored-payload signature
    failure, a broken chain window, a persisted tamper flag, or a clock
    rollback beyond the tolerance.
  * anomaly (accumulating): a source that REGRESSED against its own last-synced
    snapshot — anomaly_count += 1; ≥ 3 ⇒ tamper_flag (never immediate).
  * benign stale: a source BEHIND the DB head — resynced, never penalized.
  * initialization: absent externals with no prior sync snapshot — seeded from
    the post-append DB head, never an anomaly (first run / genuinely new
    device).

Sequencing: chain events (boot_seen / incidents / state_changed) are appended
FIRST, then absent-but-initializable stores are seeded from the POST-APPEND
head — a fresh install therefore ends boot #1 with a seeded store, and boot #2
sees a consistent world. Fail-closed: any unexpected exception ⇒ the `error`
state (retryable), never a silent pass.
"""

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pharmaos_api.licensing import chain as chain_mod
from pharmaos_api.licensing.canonical import iso_z
from pharmaos_api.licensing.errors import LicensingError
from pharmaos_api.licensing.event import LicenseClockEventV1
from pharmaos_api.licensing.external_stores import (
    ExternalStoreProvider,
    derive_ext_key,
    read_store,
    write_store,
)
from pharmaos_api.licensing.payload import (
    LicensePayloadV1,
    license_id_str,
    verify_stored_payload,
)
from pharmaos_api.licensing.runtime import (
    STATUS_ERROR,
    STATUS_KEY_LOST,
    LicenseRuntimeState,
    build_runtime_state,
    set_state,
)
from pharmaos_api.models.license import LicenseState
from pharmaos_api.security.keystore import get_clock_hmac_key

logger = logging.getLogger(__name__)

ROLLBACK_TOLERANCE = timedelta(hours=6)
ANOMALY_TAMPER_THRESHOLD = 3
BOOT_SEEN_MIN_ADVANCE = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class EvaluationOutcome:
    status: str
    runtime: LicenseRuntimeState
    high_water_utc: datetime | None
    tamper_flag: bool
    anomaly_count: int
    key_lost: bool
    incidents: tuple[tuple[str, str], ...]
    resynced: tuple[str, ...]


def decide_status(
    *,
    payload: LicensePayloadV1 | None,
    now_utc: datetime,
    high_water_utc: datetime | None,
    tamper: bool,
    key_lost: bool,
    structure_error: bool,
) -> str:
    """Pure status decision (P4 §3 ladder) — heavy integration paths test this
    directly. effective_now = max(now, high_water): the high-water mark never
    lets a rolled-back clock un-expire a license between reconciliations."""
    if tamper:
        return "tamper"
    if key_lost:
        return "key_lost"
    if structure_error:
        return "error"
    if payload is None:
        return "unlicensed"
    effective = now_utc if high_water_utc is None else max(now_utc, high_water_utc)
    if effective > payload.valid_until + timedelta(days=30):
        return "read_only"
    if effective > payload.valid_until:
        return "grace"
    return "active"


def _record_high_water(record: dict[str, Any]) -> datetime | None:
    raw = record.get("high_water")
    if not isinstance(raw, str):
        return None
    return _parse_iso(raw)


def _record_head(record: dict[str, Any]) -> tuple[int, str] | None:
    seq = record.get("head_seq")
    head_hash = record.get("head_hash")
    if not isinstance(seq, int) or not isinstance(head_hash, str):
        return None
    return seq, head_hash


def _parse_iso(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


async def evaluate_license(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    public_key: Any,
    external_providers: list[ExternalStoreProvider],
    now_utc: datetime | None = None,
    hwid: str | None = None,
) -> EvaluationOutcome:
    """Full boot evaluation: read state row → resolve the keystore key →
    authenticate externals → reconcile → decide → persist → append events →
    seed/resync externals from the post-append head → atomic runtime swap."""
    now = (now_utc or datetime.now(UTC)).astimezone(UTC)
    try:
        return await _evaluate(
            session_factory,
            public_key=public_key,
            external_providers=external_providers,
            now=now,
            hwid=hwid,
        )
    except Exception:
        logger.exception("licensing boot: unexpected failure — fail-closed error state")
        return _terminal_outcome(
            status=STATUS_ERROR,
            payload=None,
            now=now,
            high_water=None,
            hwid=hwid,
            tamper_flag=False,
            anomaly_count=0,
            incidents=(),
            resynced=(),
        )


async def _evaluate(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    public_key: Any,
    external_providers: list[ExternalStoreProvider],
    now: datetime,
    hwid: str | None,
) -> EvaluationOutcome:
    incidents: list[tuple[str, str]] = []  # (event_type, ref)

    async with session_factory() as session:
        row = (await session.execute(select(LicenseState).limit(1))).scalar_one_or_none()
        head_row = await chain_mod.current_head(session)

        # --- stored payload re-verification (DB-side edits detectable) -------
        stored_payload: LicensePayloadV1 | None = None
        if row is not None and row.signature:
            try:
                stored_payload = verify_stored_payload(
                    dict(row.payload), row.signature, public_key=public_key
                )
            except LicensingError:
                incidents.append(("rollback_detected", "stored_payload"))
        if row is not None and row.tamper_flag:
            incidents.append(("rollback_detected", "persisted_tamper_flag"))

        clock_key = get_clock_hmac_key()
        ext_key: bytes | None = derive_ext_key(clock_key) if clock_key is not None else None

        # --- keystore resolution: virgin-only generation (P4 §3) -------------
        if clock_key is None:
            external_present_any = any(
                provider.read_raw() is not None for provider in external_providers
            )
            if external_present_any or head_row is not None:
                return await _key_lost_outcome(session, now=now, hwid=hwid, public_key=public_key)
            key_bytes = os.urandom(32)
            from pharmaos_api.security.keystore import set_clock_hmac_key

            set_clock_hmac_key(key_bytes)
            clock_key = key_bytes
            ext_key = derive_ext_key(clock_key)

        resolved_key = clock_key
        resolved_ext_key = ext_key
        if resolved_key is None or resolved_ext_key is None:
            # Unreachable — both branches above return or generate; fail-closed
            # rather than assert so a future refactor cannot weaken it.
            logger.error("licensing boot: clock key failed to resolve")
            return _terminal_outcome(
                status=STATUS_ERROR,
                payload=None,
                now=now,
                high_water=None,
                hwid=hwid,
                tamper_flag=False,
                anomaly_count=0,
                incidents=(),
                resynced=(),
            )

        # --- authenticate external stores ------------------------------------
        records: dict[str, dict[str, Any] | None] = {}
        for provider in external_providers:
            try:
                records[provider.name] = read_store(provider, resolved_ext_key)
            except LicensingError as exc:
                records[provider.name] = None
                incidents.append(("rollback_detected", exc.reason or provider.name))

        verification = await chain_mod.verify_chain(
            session_factory,
            clock_key=resolved_key,
            verified_from_seq=row.verified_from_seq if row is not None else None,
        )
        if not verification.ok:
            incidents.append(("rollback_detected", f"chain:{verification.first_bad_seq}"))

        # --- directional head rules (P4 §3) -----------------------------------
        db_head_seq = verification.head_seq
        db_head_hash = verification.head_hash
        sync_state: dict[str, Any] = dict(row.external_sync_state) if row is not None else {}
        for name, record in records.items():
            ext_head = _record_head(record) if record else None
            if ext_head is None or db_head_seq is None or db_head_hash is None:
                continue
            ext_seq, ext_hash = ext_head
            if ext_seq > db_head_seq or (ext_seq == db_head_seq and ext_hash != db_head_hash):
                incidents.append(("rollback_detected", f"{name}:truncated_tail"))
        # wiped: the store was synced before (sync_state remembers) but is gone
        for provider in external_providers:
            if records[provider.name] is None and provider.name in sync_state:
                incidents.append(("rollback_detected", f"{provider.name}:wiped"))

        # --- high-water: never lowered; DB raised to any higher source --------
        highs = [
            value
            for value in (
                verification.db_high_water_utc,
                head_row[2] if head_row is not None else None,
                *(_record_high_water(record) for record in records.values() if record is not None),
            )
            if value is not None
        ]
        trusted_high_water = max(highs) if highs else None

        # --- clock rollback (the only immediate-lock clock rule) --------------
        # detected against the TRUSTED sources high-water, BEFORE now is folded
        # in (a boot's own now must never mask a rollback)
        if trusted_high_water is not None and now < trusted_high_water - ROLLBACK_TOLERANCE:
            incidents.append(("rollback_detected", "clock"))

        # --- update rule (P4 §3): high_water := max(old, now) — never lowered ---
        # folding now in is what makes a forward-poisoned clock STICK (and a
        # new-file activation is its only cure)
        high_water = max([*highs, now])

        # --- per-source regression vs its own last-synced snapshot ------------
        anomaly_count = row.anomaly_count if row is not None else 0
        for name, record in records.items():
            if record is None:
                continue
            previous = sync_state.get(name)
            current_hw = _record_high_water(record)
            if isinstance(previous, dict) and current_hw is not None:
                previous_hw = previous.get("high_water")
                if isinstance(previous_hw, str):
                    previous_dt = _parse_iso(previous_hw)
                    if previous_dt is not None and current_hw < previous_dt:
                        anomaly_count += 1
                        incidents.append(("source_regression", name))
        if row is not None and not row.tamper_flag and anomaly_count >= ANOMALY_TAMPER_THRESHOLD:
            incidents.append(("rollback_detected", "anomaly_threshold"))

        persisted_tamper = bool(row.tamper_flag) if row is not None else False
        hard_incidents = [i for i in incidents if i[0] == "rollback_detected"]
        tamper_flag = persisted_tamper or bool(hard_incidents)

        status = decide_status(
            payload=stored_payload,
            now_utc=now,
            high_water_utc=high_water,
            tamper=tamper_flag,
            key_lost=False,
            structure_error=False,
        )

        # --- persist the evaluation (same transaction the reads auto-began) ---
        fresh_row = (await session.execute(select(LicenseState).limit(1))).scalar_one_or_none()
        if fresh_row is None:
            fresh_row = LicenseState()
            session.add(fresh_row)
            await session.flush()
        fresh_row.last_seen_utc = now
        fresh_row.high_water_utc = high_water
        fresh_row.tamper_flag = tamper_flag
        fresh_row.anomaly_count = anomaly_count
        fresh_row.status = status
        for name, record in records.items():
            if record is not None:
                sync_state[name] = _sync_entry(record, now)
        if stored_payload is not None:
            fresh_row.payload = stored_payload.to_canonical_dict()
            fresh_row.license_id = stored_payload.license_id
            fresh_row.customer_name = stored_payload.customer
            fresh_row.hwid = stored_payload.hwid
        await session.commit()

        # --- chain events (incidents always; boot_seen throttled; P4 §1) ------
        if verification.ok:
            if chain_mod.high_water_advanced_since_last_event(
                verification.head_high_water_utc, high_water or now
            ):
                await _append(
                    session_factory,
                    resolved_key,
                    "boot_seen",
                    "boot",
                    now,
                    high_water or now,
                    "",
                    anomaly_count,
                )
            for event_type, ref in incidents:
                await _append(
                    session_factory,
                    resolved_key,
                    event_type,
                    "boot",
                    now,
                    high_water or now,
                    ref,
                    anomaly_count,
                )
            if row is not None and row.status != status:
                await _append(
                    session_factory,
                    resolved_key,
                    "state_changed",
                    "boot",
                    now,
                    high_water or now,
                    status,
                    anomaly_count,
                )

    # --- seed/resync ABSENT stores from the POST-APPEND head ------------------
    # (after the events above, a fresh install's boot_seen IS the anchor —
    # boot #2 sees a consistent world). Wiped stores are NOT rewritten: the
    # tamper evidence stays. Stale-present stores are rewritten without
    # penalty.
    resynced: list[str] = []
    if not tamper_flag and verification.ok:
        async with session_factory() as read_session:
            post_head = await chain_mod.current_head(read_session)
            fresh_row = (
                await read_session.execute(select(LicenseState).limit(1))
            ).scalar_one_or_none()
            current_verified_from = fresh_row.verified_from_seq if fresh_row else None
        for provider in external_providers:
            record = records[provider.name]
            if record is not None:
                # stale-present: behind the DB head → rewrite without penalty
                ext_head = _record_head(record)
                if (
                    ext_head is None
                    or db_head_seq is None
                    or ext_head[0] >= db_head_seq
                    or post_head is None
                ):
                    continue
                fresh = _canonical_record(
                    head_seq=post_head[0],
                    head_hash=post_head[1],
                    high_water=high_water or post_head[2] or now,
                    last_activation_rank=verification.last_activation_rank,
                    verified_from_seq=current_verified_from,
                    synced_at=now,
                )
                write_store(provider, resolved_ext_key, fresh)
                sync_state[provider.name] = _sync_entry(fresh, now)
                resynced.append(provider.name)
                continue
            if provider.name in sync_state:
                continue  # wiped — evidence, never rewritten
            if post_head is None:
                continue  # initialization: nothing to anchor yet
            seed = _canonical_record(
                head_seq=post_head[0],
                head_hash=post_head[1],
                high_water=high_water or post_head[2] or now,
                last_activation_rank=verification.last_activation_rank,
                verified_from_seq=current_verified_from,
                synced_at=now,
            )
            write_store(provider, resolved_ext_key, seed)
            sync_state[provider.name] = _sync_entry(seed, now)
            resynced.append(provider.name)
        # the row's high-water must also reflect the POST-APPEND head (the
        # boot_seen event carries it; a virgin boot's row would otherwise stay
        # NULL until the next boot)
        if post_head is not None:
            candidates = [value for value in (high_water, post_head[2]) if value is not None]
            if candidates:
                high_water = max(candidates)
        if resynced or post_head is not None:
            async with session_factory() as persist_session, persist_session.begin():
                persist_row = (
                    await persist_session.execute(select(LicenseState).limit(1))
                ).scalar_one_or_none()
                if persist_row is not None:
                    persist_row.external_sync_state = sync_state
                    persist_row.high_water_utc = high_water
    runtime = build_runtime_state(
        status=status,
        payload=stored_payload,
        now_utc=now,
        high_water_utc=high_water,
        hwid=hwid,
        tamper_flag=tamper_flag,
    )
    set_state(runtime)
    return EvaluationOutcome(
        status=status,
        runtime=runtime,
        high_water_utc=high_water,
        tamper_flag=tamper_flag,
        anomaly_count=anomaly_count,
        key_lost=False,
        incidents=tuple(incidents),
        resynced=tuple(resynced),
    )


async def _key_lost_outcome(
    session: AsyncSession, *, now: datetime, hwid: str | None, public_key: Any
) -> EvaluationOutcome:
    """Missing keystore key WITH existing state ⇒ key_lost (E-LIC-008). The
    chain reference (last row's high_water_utc) stays readable for the
    re-activation acceptance rule; NOTHING is regenerated here (P4 §3). The
    stored payload's SIGNATURE is vendor-key material — verification still
    works, so the screen can show who the license belongs to."""
    row = (await session.execute(select(LicenseState).limit(1))).scalar_one_or_none()
    payload: LicensePayloadV1 | None = None
    if row is not None and row.signature:
        try:
            payload = verify_stored_payload(dict(row.payload), row.signature, public_key=public_key)
        except LicensingError:
            payload = None
    runtime = build_runtime_state(
        status=STATUS_KEY_LOST,
        payload=payload,
        now_utc=now,
        high_water_utc=None,
        hwid=hwid,
    )
    set_state(runtime)
    return EvaluationOutcome(
        status=STATUS_KEY_LOST,
        runtime=runtime,
        high_water_utc=None,
        tamper_flag=False,
        anomaly_count=0,
        key_lost=True,
        incidents=(),
        resynced=(),
    )


def _terminal_outcome(
    *,
    status: str,
    payload: LicensePayloadV1 | None,
    now: datetime,
    high_water: datetime | None,
    hwid: str | None,
    tamper_flag: bool,
    anomaly_count: int,
    incidents: tuple[tuple[str, str], ...],
    resynced: tuple[str, ...],
) -> EvaluationOutcome:
    runtime = build_runtime_state(
        status=status,
        payload=payload,
        now_utc=now,
        high_water_utc=high_water,
        hwid=hwid,
        tamper_flag=tamper_flag,
    )
    set_state(runtime)
    return EvaluationOutcome(
        status=status,
        runtime=runtime,
        high_water_utc=high_water,
        tamper_flag=tamper_flag,
        anomaly_count=anomaly_count,
        key_lost=False,
        incidents=incidents,
        resynced=resynced,
    )


async def _append(
    session_factory: async_sessionmaker[AsyncSession],
    clock_key: bytes,
    event_type: str,
    origin: str,
    observed_at: datetime,
    high_water: datetime,
    ref: str,
    anomaly_count: int,
) -> None:
    try:
        await chain_mod.append_clock_event(
            session_factory,
            clock_key=clock_key,
            event=LicenseClockEventV1(
                event_type=event_type,  # type: ignore[arg-type]
                origin=origin,  # type: ignore[arg-type]
                observed_at=observed_at,
                high_water_utc=high_water,
                ref=ref,
                anomaly_count=anomaly_count,
            ),
        )
    except Exception:
        # An event-append failure never blocks boot — but it is loud.
        logger.exception("licensing: failed to append %s event", event_type)


def _canonical_record(
    *,
    head_seq: int | None,
    head_hash: str | None,
    high_water: datetime | None,
    last_activation_rank: tuple[datetime, int, int] | None,
    verified_from_seq: int | None,
    synced_at: datetime,
) -> dict[str, Any]:
    issued_at, license_id = (
        (last_activation_rank[0], license_id_str(*last_activation_rank[1:]))
        if last_activation_rank is not None
        else (None, None)
    )
    return {
        "high_water": iso_z(high_water) if high_water is not None else None,
        "head_seq": head_seq,
        "head_hash": head_hash,
        "last_activation_issued_at": iso_z(issued_at) if issued_at is not None else None,
        "last_activation_license_id": license_id,
        "verified_from_seq": verified_from_seq,
        "last_successful_sync_utc": iso_z(synced_at),
    }


def _sync_entry(record: dict[str, Any], now: datetime) -> dict[str, Any]:
    return {
        "high_water": (
            record.get("high_water") if isinstance(record.get("high_water"), str) else None
        ),
        "head_seq": record.get("head_seq") if isinstance(record.get("head_seq"), int) else None,
        "synced_at": iso_z(now),
    }
