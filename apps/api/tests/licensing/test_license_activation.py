"""Activation tests (P4 §3) — new/old file rules, key_lost acceptance, re-seal,
device binding, expiry, and the future-dated rejection. Each test runs on its
own cloned DB with its own keystore: full independence."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from pharmaos_api.licensing.activation import activate_license
from pharmaos_api.licensing.errors import LicensingError
from pharmaos_api.licensing.external_stores import ExternalStoreProvider
from pharmaos_api.security.keystore import get_clock_hmac_key
from tests.licensing.helpers import (
    TEST_HWID,
    TEST_KID,
    make_license,
    make_vendor_key,
)

VENDOR = make_vendor_key()
ACCEPTED = frozenset({TEST_KID})


class FakeStore(ExternalStoreProvider):
    def __init__(self) -> None:
        self.name = "fake"
        self._data: bytes | None = None

    def read_raw(self) -> bytes | None:
        return self._data

    def write_raw(self, data: bytes) -> None:
        self._data = data


def _now() -> datetime:
    return datetime.now(UTC)


async def _activate(factory, raw: bytes, *, now: datetime, hwid: str = TEST_HWID) -> object:  # type: ignore[no-untyped-def]
    return await activate_license(
        factory,
        license_file=raw,
        public_key=VENDOR.public_key(),
        accepted_kids=ACCEPTED,
        external_providers=[FakeStore()],
        now_utc=now,
        hwid=hwid,
    )


async def test_first_activation_is_active_and_persists(licensing_fresh, keystore_per_test) -> None:  # type: ignore[no-untyped-def]
    factory = licensing_fresh
    now = _now()
    raw = make_license(VENDOR, license_id="LIC-2026-000001", issued_at=now - timedelta(minutes=1))
    result = await _activate(factory, raw, now=now)
    assert result.status == "active"
    assert result.new_activation is True
    assert result.license_id == "LIC-2026-000001"

    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT license_id, status, customer_name, hwid, kid FROM license_state LIMIT 1"
                )
            )
        ).first()
        assert row is not None
        assert row.license_id == "LIC-2026-000001"
        assert row.status == "active"
        assert row.customer_name == "صيدلية الاختبار"
        assert row.hwid == TEST_HWID
        assert row.kid == TEST_KID
        head = (
            await session.execute(
                text("SELECT event_type, ref FROM license_clock_events ORDER BY seq DESC LIMIT 1")
            )
        ).first()
        assert head is not None
        assert head.event_type == "activation"
        assert head.ref == "LIC-2026-000001"


async def test_reactivating_same_file_is_idempotent(licensing_fresh, keystore_per_test) -> None:  # type: ignore[no-untyped-def]
    factory = licensing_fresh
    now = _now()
    raw = make_license(VENDOR, license_id="LIC-2026-000001", issued_at=now - timedelta(minutes=1))
    await _activate(factory, raw, now=now)
    result = await _activate(factory, raw, now=now)
    assert result.new_activation is False


async def test_device_mismatch_rejected(licensing_fresh, keystore_per_test) -> None:  # type: ignore[no-untyped-def]
    raw = make_license(
        VENDOR,
        license_id="LIC-2026-000001",
        hwid="PHAR-ZZZZ-YYYY-XXXX-WWWW",
        issued_at=_now() - timedelta(minutes=1),
    )
    with pytest.raises(LicensingError) as excinfo:
        await _activate(licensing_fresh, raw, now=_now())
    assert excinfo.value.code == "E-LIC-004"


async def test_expired_file_rejected(licensing_fresh, keystore_per_test) -> None:  # type: ignore[no-untyped-def]
    raw = make_license(
        VENDOR,
        license_id="LIC-2026-000001",
        issued_at=_now() - timedelta(days=40),
        valid_until=_now() - timedelta(days=10),
    )
    with pytest.raises(LicensingError) as excinfo:
        await _activate(licensing_fresh, raw, now=_now())
    assert excinfo.value.code == "E-LIC-005"


async def test_future_issued_at_rejected(licensing_fresh, keystore_per_test) -> None:  # type: ignore[no-untyped-def]
    raw = make_license(VENDOR, license_id="LIC-2026-000001", issued_at=_now() + timedelta(hours=48))
    with pytest.raises(LicensingError) as excinfo:
        await _activate(licensing_fresh, raw, now=_now())
    assert excinfo.value.code == "E-LIC-003"
    assert excinfo.value.reason == "issued_at_in_future"


async def test_future_issued_at_tolerated_when_high_water_leads(
    licensing_fresh, keystore_per_test
) -> None:  # type: ignore[no-untyped-def]
    """CMOS-style tolerance: a poisoned-forward high-water makes effective_now
    lead the raw clock — renewals issued between the two are accepted (P4 §3),
    but the poisoned clock never licenses arbitrary future dates."""
    factory = licensing_fresh
    now = _now()
    raw_a = make_license(VENDOR, license_id="LIC-2026-000001", issued_at=now - timedelta(minutes=1))
    await _activate(factory, raw_a, now=now)

    # poison the high-water via a boot evaluation "from the future"
    from pharmaos_api.licensing.state import evaluate_license

    poisoned = now + timedelta(hours=72)
    await evaluate_license(
        factory, public_key=VENDOR.public_key(), external_providers=[FakeStore()], now_utc=poisoned
    )

    # B: issued now+48h — beyond the RAW clock's 24h tolerance, covered by the
    # poisoned high-water (effective_now = now+72h) → accepted as a renewal
    raw_b = make_license(VENDOR, license_id="LIC-2026-000002", issued_at=now + timedelta(hours=48))
    result = await _activate(factory, raw_b, now=now)
    assert result.status == "active"
    assert result.new_activation is True

    # C: issued now+100h — beyond effective_now (now+48h after B re-anchored)
    # + 24h tolerance → rejected
    raw_c = make_license(VENDOR, license_id="LIC-2026-000003", issued_at=now + timedelta(hours=100))
    with pytest.raises(LicensingError) as excinfo:
        await _activate(factory, raw_c, now=now)
    assert excinfo.value.code == "E-LIC-003"
    assert excinfo.value.reason == "issued_at_in_future"


async def test_new_file_lowers_high_water_forward_poison_cure(
    licensing_fresh, keystore_per_test
) -> None:  # type: ignore[no-untyped-def]
    factory = licensing_fresh
    now = _now()
    # poison the high-water with a boot "from the future"
    from pharmaos_api.licensing.state import evaluate_license

    await evaluate_license(
        factory,
        public_key=VENDOR.public_key(),
        external_providers=[FakeStore()],
        now_utc=now + timedelta(days=30),
    )
    async with factory() as session:
        poisoned = (
            await session.execute(text("SELECT high_water_utc FROM license_state LIMIT 1"))
        ).scalar()
    assert poisoned is not None
    # the cure: a NEW owner file re-anchors high_water at its own signed issued_at
    raw = make_license(VENDOR, license_id="LIC-2026-000001", issued_at=now - timedelta(minutes=1))
    result = await _activate(factory, raw, now=now)
    assert result.new_activation is True
    async with factory() as session:
        cured = (
            await session.execute(text("SELECT high_water_utc FROM license_state LIMIT 1"))
        ).scalar()
    assert cured is not None and cured < poisoned


async def test_tamper_then_new_file_heals_reseal(licensing_fresh, keystore_per_test) -> None:  # type: ignore[no-untyped-def]
    factory = licensing_fresh
    now = _now()
    raw1 = make_license(VENDOR, license_id="LIC-2026-000001", issued_at=now - timedelta(days=1))
    await _activate(factory, raw1, now=now)
    # force a tamper (clock rollback beyond tolerance)
    from pharmaos_api.licensing.state import evaluate_license

    broken = await evaluate_license(
        factory,
        public_key=VENDOR.public_key(),
        external_providers=[FakeStore()],
        now_utc=now - timedelta(days=2),
    )
    assert broken.status == "tamper"
    # the OLD file cannot heal it
    result = await _activate(factory, raw1, now=now)
    assert result.status == "tamper"
    # a NEW file (higher rank) clears the tamper — no re-seal needed here: the
    # tamper came from the clock, the chain itself is intact (resealed stays
    # False; the re-seal path is exercised by the key_lost test)
    raw2 = make_license(VENDOR, license_id="LIC-2026-000002", issued_at=now - timedelta(minutes=1))
    healed = await _activate(factory, raw2, now=now)
    assert healed.status == "active"
    assert healed.new_activation is True
    assert healed.resealed is False
    assert healed.runtime.status == "active"


async def test_tamper_then_old_file_stays_tamper(licensing_fresh, keystore_per_test) -> None:  # type: ignore[no-untyped-def]
    factory = licensing_fresh
    now = _now()
    raw1 = make_license(VENDOR, license_id="LIC-2026-000001", issued_at=now - timedelta(days=1))
    await _activate(factory, raw1, now=now)
    raw0 = make_license(VENDOR, license_id="LIC-2026-000000", issued_at=now - timedelta(days=2))
    await _activate(factory, raw0, now=now)  # newer id at an older time → NEW (rank by tuple)
    from pharmaos_api.licensing.state import evaluate_license

    await evaluate_license(
        factory,
        public_key=VENDOR.public_key(),
        external_providers=[FakeStore()],
        now_utc=now - timedelta(days=2),
    )
    result = await _activate(factory, raw0, now=now)  # re-activate the older file
    assert result.status == "tamper"


async def test_key_lost_old_file_rejected_new_file_rotates(
    licensing_fresh, keystore_per_test
) -> None:  # type: ignore[no-untyped-def]
    factory = licensing_fresh
    now = _now()
    raw1 = make_license(VENDOR, license_id="LIC-2026-000001", issued_at=now - timedelta(days=1))
    await _activate(factory, raw1, now=now)
    # delete the keystore entry (the deletion attack — P4 §0/§3)
    for path in keystore_per_test.glob("*"):
        path.unlink()
    # an OLDER file (issued before the head's high_water = raw1's issued_at)
    # must be REJECTED
    older = make_license(VENDOR, license_id="LIC-2026-000000", issued_at=now - timedelta(days=2))
    with pytest.raises(LicensingError) as excinfo:
        await _activate(factory, older, now=now)
    assert excinfo.value.code == "E-LIC-008"
    # a NEWER file (issued after) is accepted: key rotation + re-seal
    newer = make_license(VENDOR, license_id="LIC-2026-000002", issued_at=now - timedelta(minutes=1))
    result = await _activate(factory, newer, now=now)
    assert result.status == "active"
    assert result.key_rotated is True
    assert result.resealed is True
    assert get_clock_hmac_key() is not None


async def test_key_lost_virgin_device_generates_normally(
    licensing_fresh, keystore_per_test
) -> None:  # type: ignore[no-untyped-def]
    """A genuinely fresh device (no chain, no stores) generates its key on the
    first activation — the virgin path never reports key_lost."""
    raw = make_license(
        VENDOR, license_id="LIC-2026-000001", issued_at=_now() - timedelta(minutes=1)
    )
    result = await _activate(licensing_fresh, raw, now=_now())
    assert result.status == "active"
    assert result.key_rotated is False
