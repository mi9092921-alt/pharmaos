"""State machine + reconciliation tests (P4 §3). Runs on the SHARED scratch DB
(relative assertions) with a fresh keystore per test."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from pharmaos_api.licensing.external_stores import (
    ExternalStoreProvider,
    write_store,
)
from pharmaos_api.licensing.state import (
    ANOMALY_TAMPER_THRESHOLD,
    decide_status,
    evaluate_license,
)
from pharmaos_api.security.keystore import get_clock_hmac_key
from tests.licensing.helpers import TEST_HWID, make_vendor_key


class FakeStore(ExternalStoreProvider):
    """In-memory provider — full provider contract without filesystem."""

    def __init__(self, name: str = "fake") -> None:
        self.name = name
        self._data: bytes | None = None
        self.writes = 0

    def read_raw(self) -> bytes | None:
        return self._data

    def write_raw(self, data: bytes) -> None:
        self._data = data
        self.writes += 1


def _now() -> datetime:
    return datetime.now(UTC)


# --- pure status ladder -------------------------------------------------------


def test_decide_status_unlicensed() -> None:
    assert (
        decide_status(
            payload=None,
            now_utc=_now(),
            high_water_utc=None,
            tamper=False,
            key_lost=False,
            structure_error=False,
        )
        == "unlicensed"
    )


def test_decide_status_active_with_warning_window() -> None:
    now = _now()
    payload = _payload(valid_until=now + timedelta(days=5))  # inside 14d window
    status = decide_status(
        payload=payload,
        now_utc=now,
        high_water_utc=None,
        tamper=False,
        key_lost=False,
        structure_error=False,
    )
    assert status == "active"


def test_decide_status_grace_and_read_only() -> None:
    now = _now()
    soon_expired = _payload(valid_until=now - timedelta(days=10))
    assert (
        decide_status(
            payload=soon_expired,
            now_utc=now,
            high_water_utc=None,
            tamper=False,
            key_lost=False,
            structure_error=False,
        )
        == "grace"
    )
    long_expired = _payload(valid_until=now - timedelta(days=40))
    assert (
        decide_status(
            payload=long_expired,
            now_utc=now,
            high_water_utc=None,
            tamper=False,
            key_lost=False,
            structure_error=False,
        )
        == "read_only"
    )


def test_decide_status_tamper_and_key_lost_precede_ladder() -> None:
    payload = _payload(valid_until=_now() + timedelta(days=100))
    assert (
        decide_status(
            payload=payload,
            now_utc=_now(),
            high_water_utc=None,
            tamper=True,
            key_lost=False,
            structure_error=False,
        )
        == "tamper"
    )
    assert (
        decide_status(
            payload=payload,
            now_utc=_now(),
            high_water_utc=None,
            tamper=False,
            key_lost=True,
            structure_error=False,
        )
        == "key_lost"
    )


def test_decide_status_effective_now_uses_high_water() -> None:
    now = _now()
    # the license expires 5 days BEFORE the high-water — effective_now (the
    # high-water) puts it past grace even though the raw clock says active
    payload = _payload(valid_until=now - timedelta(days=40))
    high_water = now  # rolled forward
    assert (
        decide_status(
            payload=payload,
            now_utc=now - timedelta(days=1),
            high_water_utc=high_water,
            tamper=False,
            key_lost=False,
            structure_error=False,
        )
        == "read_only"
    )


def _payload(valid_until: datetime) -> object:
    from pharmaos_api.licensing.payload import LicensePayloadV1

    return LicensePayloadV1(
        schema_version=1,
        license_id="LIC-2026-000100",
        customer="x",
        hwid=TEST_HWID,
        issued_at=valid_until - timedelta(days=365),
        valid_until=valid_until,
        kind="subscription",
    )


# --- evaluate_license integration (shared scratch DB) --------------------------


async def test_first_evaluate_generates_key_and_boot_seen(
    licensing_fresh, keystore_per_test
) -> None:  # type: ignore[no-untyped-def]
    factory = licensing_fresh
    assert get_clock_hmac_key() is None  # fresh store
    outcome = await evaluate_license(
        factory, public_key=make_vendor_key().public_key(), external_providers=[FakeStore()]
    )
    assert outcome.status == "unlicensed"
    assert get_clock_hmac_key() is not None  # virgin generation happened
    assert outcome.key_lost is False

    async with factory() as session:
        head = (
            await session.execute(
                text("SELECT event_type FROM license_clock_events ORDER BY seq DESC LIMIT 1")
            )
        ).scalar()
        assert head == "boot_seen"  # the fresh install's first anchor


async def test_second_evaluate_throttles_boot_seen(licensing_fresh, keystore_per_test) -> None:  # type: ignore[no-untyped-def]
    factory = licensing_fresh
    store = FakeStore()  # ONE instance — a fresh object per evaluate would
    # look like a wiped store (sync_state remembers) and correctly tamper
    await evaluate_license(
        factory, public_key=make_vendor_key().public_key(), external_providers=[store]
    )
    async with factory() as session:
        before = (await session.execute(text("SELECT count(*) FROM license_clock_events"))).scalar()
    await evaluate_license(
        factory, public_key=make_vendor_key().public_key(), external_providers=[store]
    )
    async with factory() as session:
        after = (await session.execute(text("SELECT count(*) FROM license_clock_events"))).scalar()
    # boot_seen only when high_water advanced ≥ 1h — an immediate re-boot adds none
    assert after == before


async def test_clock_rollback_is_tamper(licensing_fresh, keystore_per_test) -> None:  # type: ignore[no-untyped-def]
    factory = licensing_fresh
    now = _now()
    await evaluate_license(
        factory,
        public_key=make_vendor_key().public_key(),
        external_providers=[FakeStore()],
        now_utc=now,
    )
    outcome = await evaluate_license(
        factory,
        public_key=make_vendor_key().public_key(),
        external_providers=[FakeStore()],
        now_utc=now - timedelta(days=2),
    )
    assert outcome.status == "tamper"
    assert outcome.tamper_flag is True
    assert any(ref == "clock" for _, ref in outcome.incidents)


async def test_wiped_store_is_tamper_but_fresh_install_is_not(
    licensing_fresh, keystore_per_test
) -> None:  # type: ignore[no-untyped-def]
    factory = licensing_fresh
    store = FakeStore()
    now = _now()
    # boot #1: fresh install — the boot_seen anchors the store seeding
    first = await evaluate_license(
        factory, public_key=make_vendor_key().public_key(), external_providers=[store], now_utc=now
    )
    assert first.status == "unlicensed"
    assert store.writes >= 1  # seeded from the post-append head
    # boot #2 (consistent world) — no tamper
    second = await evaluate_license(
        factory, public_key=make_vendor_key().public_key(), external_providers=[store], now_utc=now
    )
    assert second.status == "unlicensed"
    assert second.tamper_flag is False
    # boot #3: the store is WIPED (sync_state remembers it existed) ⇒ tamper
    store._data = None
    third = await evaluate_license(
        factory, public_key=make_vendor_key().public_key(), external_providers=[store], now_utc=now
    )
    assert third.status == "tamper"
    assert any(ref.endswith(":wiped") for _, ref in third.incidents)


async def test_truncated_tail_external_ahead_is_tamper(
    licensing_fresh, keystore_per_test
) -> None:  # type: ignore[no-untyped-def]
    factory = licensing_fresh
    store = FakeStore()
    now = _now()
    await evaluate_license(
        factory, public_key=make_vendor_key().public_key(), external_providers=[store], now_utc=now
    )
    key = get_clock_hmac_key()
    assert key is not None

    async with factory() as session:
        head = (
            await session.execute(
                text("SELECT seq, entry_hash FROM license_clock_events ORDER BY seq DESC LIMIT 1")
            )
        ).first()
    assert head is not None
    # a MAC-VALID record claiming a head AHEAD of the DB = tail truncation
    record = {
        "high_water": now.isoformat().replace("+00:00", "Z"),
        "head_seq": int(head.seq) + 5,
        "head_hash": str(head.entry_hash),
        "last_activation_issued_at": None,
        "last_activation_license_id": None,
        "verified_from_seq": None,
        "last_successful_sync_utc": now.isoformat().replace("+00:00", "Z"),
    }
    write_store(store, _ext_key(key), record)
    outcome = await evaluate_license(
        factory, public_key=make_vendor_key().public_key(), external_providers=[store], now_utc=now
    )
    assert outcome.status == "tamper"
    assert any("truncated_tail" in ref for _, ref in outcome.incidents)


async def test_source_regression_accumulates_to_tamper(
    licensing_fresh, keystore_per_test
) -> None:  # type: ignore[no-untyped-def]
    factory = licensing_fresh
    store = FakeStore()
    now = _now()
    await evaluate_license(
        factory, public_key=make_vendor_key().public_key(), external_providers=[store], now_utc=now
    )
    key = get_clock_hmac_key()
    assert key is not None
    async with factory() as session:
        head = (
            await session.execute(
                text("SELECT seq, entry_hash FROM license_clock_events ORDER BY seq DESC LIMIT 1")
            )
        ).first()
    assert head is not None

    def _record(hw: datetime) -> dict[str, object]:
        return {
            "high_water": hw.isoformat().replace("+00:00", "Z"),
            "head_seq": int(head.seq),
            "head_hash": str(head.entry_hash),
            "last_activation_issued_at": None,
            "last_activation_license_id": None,
            "verified_from_seq": None,
            "last_successful_sync_utc": now.isoformat().replace("+00:00", "Z"),
        }

    # regression #1 and #2: MAC-valid records with an OLDER high_water than the
    # last-synced snapshot — anomalies, not (yet) tamper
    write_store(store, _ext_key(key), _record(now - timedelta(hours=2)))
    outcome = await evaluate_license(
        factory, public_key=make_vendor_key().public_key(), external_providers=[store], now_utc=now
    )
    assert outcome.status != "tamper"
    assert outcome.anomaly_count >= 1
    write_store(store, _ext_key(key), _record(now - timedelta(hours=3)))
    outcome = await evaluate_license(
        factory, public_key=make_vendor_key().public_key(), external_providers=[store], now_utc=now
    )
    assert outcome.anomaly_count >= 2
    # regression #3 crosses the threshold ⇒ tamper_flag
    write_store(store, _ext_key(key), _record(now - timedelta(hours=4)))
    outcome = await evaluate_license(
        factory, public_key=make_vendor_key().public_key(), external_providers=[store], now_utc=now
    )
    assert outcome.anomaly_count >= ANOMALY_TAMPER_THRESHOLD
    assert outcome.status == "tamper"


async def test_key_lost_when_state_exists(licensing_fresh, keystore_per_test, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    factory = licensing_fresh
    store = FakeStore()
    now = _now()
    await evaluate_license(
        factory, public_key=make_vendor_key().public_key(), external_providers=[store], now_utc=now
    )
    # wipe the KEY (dev store file) — the state (chain + stores) still exists
    for path in keystore_per_test.glob("*"):
        path.unlink()
    outcome = await evaluate_license(
        factory, public_key=make_vendor_key().public_key(), external_providers=[store], now_utc=now
    )
    assert outcome.status == "key_lost"
    assert outcome.key_lost is True


def _ext_key(clock_key: bytes) -> bytes:
    from pharmaos_api.licensing.external_stores import derive_ext_key

    return derive_ext_key(clock_key)
