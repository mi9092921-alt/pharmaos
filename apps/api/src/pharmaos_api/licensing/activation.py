"""License activation (P4 §3) — the new-vs-old file rules, the key_lost
acceptance rule, and the re-seal recovery path.

Rules (frozen):
  * A file is NEW iff its rank `(issued_at, year, number)` is strictly greater
    than the maximum rank across the DB (last `activation` event) and the
    MAC-authenticated external stores — never a single source (a crash between
    store writes must not accept an older file as new).
  * NEW ⇒ high_water := issued_at (may LOWER it — the deliberate cure for a
    forward-poisoned clock), verified_from_seq := the new activation row (the
    verification window moves past any prior damage — the ONLY way a broken
    chain is healed), tamper flag and anomaly count cleared, payload updated,
    `activation` event appended with ref = license_id.
  * OLD/matching (≤) ⇒ accepted idempotently: NO flag clearing, NO payload/
    high_water change (re-activating the same file after tampering empties
    detection of all value). The activation event is still appended — the
    NEW/OLD distinction lives in the state effects, not in the ledger.
  * issued_at > effective_now + 24h ⇒ E-LIC-003 / details.reason =
    issued_at_in_future (an owner-side clock error must not push the client's
    high-water into the future). A device with NO chain has no high-water yet —
    the raw clock is used and the rejection reads "fix the device date".
  * key_lost (missing keystore key + existing state): re-activation is accepted
    ONLY for a file with issued_at ≥ the last chain row's high_water_utc (a
    trigger-protected reference readable without the key); acceptance ROTATES
    the key and re-seals. Older files are rejected (E-LIC-008) — deleting the
    keystore entry is not a reset. Declared residual (P4 §0): a fresh,
    never-activated owner file can be used once as a reset — bounded by its own
    signed issued_at, and every issuance is in the owner's ledger.
"""

import base64
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pharmaos_api.errors import ErrorCode
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
from pharmaos_api.licensing.hwid import compute_hwid
from pharmaos_api.licensing.payload import (
    LicensePayloadV1,
    license_id_parts,
    parse_license_file,
)
from pharmaos_api.licensing.runtime import (
    STATUS_ACTIVE,
    STATUS_GRACE,
    STATUS_READ_ONLY,
    STATUS_TAMPER,
    LicenseRuntimeState,
    build_runtime_state,
    set_state,
)
from pharmaos_api.models.license import LicenseState
from pharmaos_api.security.keystore import get_clock_hmac_key, set_clock_hmac_key

logger = logging.getLogger(__name__)

FUTURE_ISSUED_TOLERANCE = timedelta(hours=24)
GRACE_DAYS = 30


@dataclass(frozen=True, slots=True)
class ActivationResult:
    status: str
    runtime: LicenseRuntimeState
    license_id: str
    new_activation: bool
    key_rotated: bool
    resealed: bool
    valid_until: datetime
    grace_until: datetime


async def activate_license(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    license_file: bytes,
    public_key: Any,
    accepted_kids: frozenset[str],
    external_providers: list[ExternalStoreProvider],
    now_utc: datetime | None = None,
    hwid: str | None = None,
) -> ActivationResult:
    now = (now_utc or datetime.now(UTC)).astimezone(UTC)

    # 1) structural + signature verification (E-LIC-003 with details.reason)
    container = parse_license_file(license_file, public_key=public_key, accepted_kids=accepted_kids)

    # 2) expiry at activation (E-LIC-005)
    if now > container.payload.valid_until:
        raise LicensingError(ErrorCode.LICENSE_EXPIRED)

    # 3) hardware binding (E-LIC-004)
    machine_hwid = hwid or compute_hwid()
    if container.payload.hwid != machine_hwid:
        raise LicensingError(ErrorCode.LICENSE_DEVICE_MISMATCH)

    async with session_factory() as session:
        row = (await session.execute(select(LicenseState).limit(1))).scalar_one_or_none()
        head = await chain_mod.current_head(session)

        # --- keystore resolution --------------------------------------------
        clock_key = get_clock_hmac_key()
        key_lost = clock_key is None and (
            head is not None or _externals_present(external_providers)
        )
        key_rotated = False
        if clock_key is None:
            if key_lost:
                # Acceptance rule: issued_at ≥ the last chain row's
                # high_water_utc (None when only external stores exist — there
                # is no older on-chain reference to lose against).
                head_high_water = head[2] if head is not None else None
                if head_high_water is not None and container.payload.issued_at < head_high_water:
                    raise LicensingError(ErrorCode.LICENSE_KEY_LOST)
                logger.warning(
                    "licensing: keystore clock key lost — rotating on activation of %s",
                    container.payload.license_id,
                )
            key_bytes = os.urandom(32)
            set_clock_hmac_key(key_bytes)
            clock_key = key_bytes
            key_rotated = key_lost

        # future-dated check against the effective clock (P4 §3)
        head_high_water = head[2] if head is not None else None
        effective_now = now if head_high_water is None else max(now, head_high_water)
        if container.payload.issued_at > effective_now + FUTURE_ISSUED_TOLERANCE:
            raise LicensingError(ErrorCode.LICENSE_INVALID_SIGNATURE, "issued_at_in_future")

        # chain verification → a broken window is healed ONLY by a new file
        verification = await chain_mod.verify_chain(
            session_factory,
            clock_key=clock_key,
            verified_from_seq=row.verified_from_seq if row is not None else None,
        )
        chain_broken = not verification.ok

        # --- new-vs-old rank: max across DB + MAC-verified externals ---------
        baseline = verification.last_activation_rank
        ext_key = derive_ext_key(clock_key)
        for provider in external_providers:
            try:
                record = read_store(provider, ext_key)
            except LicensingError:
                record = None
            rank = _record_activation_rank(record) if record else None
            if rank is not None and (baseline is None or rank > baseline):
                baseline = rank

        new_activation = baseline is None or container.payload.activation_rank() > baseline

        # --- append the activation event (always) ----------------------------
        seq, entry_hash = await chain_mod.append_clock_event(
            session_factory,
            clock_key=clock_key,
            event=LicenseClockEventV1(
                event_type="activation",
                origin="activation",
                observed_at=now,
                high_water_utc=container.payload.issued_at,
                ref=container.payload.license_id,
                anomaly_count=0 if new_activation else (row.anomaly_count if row else 0),
            ),
        )

        if new_activation:
            high_water: datetime | None = container.payload.issued_at
            verified_from_seq: int | None = seq  # re-seal — window moves past damage
            anomaly_count = 0
            tamper_flag = False
            resealed = chain_broken or key_lost
        else:
            high_water = row.high_water_utc if row is not None else None
            verified_from_seq = row.verified_from_seq if row is not None else None
            anomaly_count = row.anomaly_count if row is not None else 0
            tamper_flag = bool(row.tamper_flag) if row is not None else False
            resealed = False

        status = _post_activation_status(container.payload, now, high_water, tamper_flag)

        # --- persist (same transaction the reads auto-began) ------------------
        fresh_row = (await session.execute(select(LicenseState).limit(1))).scalar_one_or_none()
        if fresh_row is None:
            fresh_row = LicenseState()
            session.add(fresh_row)
            await session.flush()
        if new_activation:
            fresh_row.payload = container.payload.to_canonical_dict()
            fresh_row.signature = base64.b64encode(container.signature).decode("ascii")
            fresh_row.license_id = container.payload.license_id
            fresh_row.customer_name = container.payload.customer
            fresh_row.hwid = container.payload.hwid
            fresh_row.kid = container.kid
            fresh_row.activated_at = now
            fresh_row.high_water_utc = high_water
            fresh_row.verified_from_seq = verified_from_seq
            fresh_row.anomaly_count = anomaly_count
            fresh_row.tamper_flag = tamper_flag
            fresh_row.last_activation_issued_at = container.payload.issued_at
            fresh_row.last_activation_license_id = container.payload.license_id
        # idempotent (old-file) activation touches NOTHING licensing-relevant —
        # only the last-seen heartbeat and the derived status below.
        fresh_row.status = status
        fresh_row.last_seen_utc = now
        await session.commit()

    # --- external stores: post-activation record (MAC'd), best-effort --------
    record = {
        "high_water": iso_z(high_water) if high_water is not None else None,
        "head_seq": seq,
        "head_hash": entry_hash,
        "last_activation_issued_at": iso_z(container.payload.issued_at),
        "last_activation_license_id": container.payload.license_id,
        "verified_from_seq": verified_from_seq,
        "last_successful_sync_utc": iso_z(now),
    }
    for provider in external_providers:
        try:
            write_store(provider, ext_key, record)
        except OSError:
            logger.warning("licensing: could not write external store %s", provider.name)

    runtime = build_runtime_state(
        status=status,
        payload=container.payload,
        now_utc=now,
        high_water_utc=high_water,
        hwid=machine_hwid,
        tamper_flag=tamper_flag,
    )
    set_state(runtime)
    return ActivationResult(
        status=status,
        runtime=runtime,
        license_id=container.payload.license_id,
        new_activation=new_activation,
        key_rotated=key_rotated,
        resealed=resealed,
        valid_until=container.payload.valid_until,
        grace_until=container.payload.valid_until + timedelta(days=GRACE_DAYS),
    )


def _record_activation_rank(record: dict[str, Any]) -> tuple[datetime, int, int] | None:
    issued_raw = record.get("last_activation_issued_at")
    license_id = record.get("last_activation_license_id")
    if not isinstance(issued_raw, str) or not isinstance(license_id, str):
        return None
    try:
        issued_at = datetime.fromisoformat(issued_raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    try:
        year, number = license_id_parts(license_id)
    except ValueError:
        return None
    return issued_at, year, number


def _externals_present(external_providers: list[ExternalStoreProvider]) -> bool:
    return any(provider.read_raw() is not None for provider in external_providers)


def _post_activation_status(
    payload: LicensePayloadV1, now: datetime, high_water: datetime | None, tamper_flag: bool
) -> str:
    if tamper_flag:
        return STATUS_TAMPER
    effective = now if high_water is None else max(now, high_water)
    if effective > payload.valid_until + timedelta(days=GRACE_DAYS):
        return STATUS_READ_ONLY
    if effective > payload.valid_until:
        return STATUS_GRACE
    return STATUS_ACTIVE
