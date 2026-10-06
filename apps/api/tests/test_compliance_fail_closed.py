"""Fail-closed compliance gate (installer decisions 8/6).

Production + local simulator ⇒ rows are NEVER marked accepted/reported: the
scheduled 15-minute drain must not be able to turn simulated acceptances
into fake compliance. Dev/test keep the simulator (existing behavior).
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.config import Settings
from pharmaos_api.models import Branch, TtEvent, User
from pharmaos_api.services.compliance import fail_closed, tt_service


@pytest.fixture
def production_settings() -> Settings:
    return Settings(pharmaos_env="production")


def test_gate_blocks_simulator_in_production(
    production_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert production_settings.is_production
    monkeypatch.setattr(fail_closed, "get_settings", lambda: production_settings)
    with pytest.raises(fail_closed.ComplianceFailClosedError, match="fail-closed"):
        fail_closed.ensure_allowed(simulator=True)


def test_gate_allows_real_adapter_in_production(
    production_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A real (non-simulator) adapter is exactly what production wants.
    monkeypatch.setattr(fail_closed, "get_settings", lambda: production_settings)
    fail_closed.ensure_allowed(simulator=False)


def test_gate_allows_simulator_outside_production() -> None:
    # The test env is not production — the simulator stays dev/test-legitimate.
    fail_closed.ensure_allowed(simulator=True)


async def test_drain_leaves_rows_untouched_in_production(
    db_session: AsyncSession,
    seeded_user: dict,
    monkeypatch: pytest.MonkeyPatch,
    production_settings: Settings,
) -> None:
    """The M1 security gate end-to-end: a pending track-and-trace event under
    production+simulator survives a drain EXACTLY as it was — status pending,
    attempts untouched, nothing audited as reported."""
    monkeypatch.setattr(fail_closed, "get_settings", lambda: production_settings)

    user = (
        await db_session.execute(select(User).where(User.username == seeded_user["username"]))
    ).scalar_one()
    branch = Branch(name=f"فرع {uuid.uuid4().hex[:6]}", country_code="EG", currency_code="EGP")
    db_session.add(branch)
    await db_session.flush()
    event = TtEvent(
        branch_id=branch.id,
        event_type="receive",
        pack_serial_id=None,
        gtin="06224000000017",
        serial_number=f"S-{uuid.uuid4().hex[:10]}",
        status="pending",
        created_by=user.id,
        updated_by=user.id,
    )
    db_session.add(event)
    await db_session.commit()

    result = await tt_service.drain(db_session, branch_id=branch.id, actor=None)
    assert result["skipped_fail_closed"] == 1
    assert result["processed"] == 1
    assert result["reported"] == 0

    await db_session.refresh(event)
    assert event.status == "pending"  # untouched — fail-closed, not "failed"
    assert event.report_attempts == 0
    assert event.last_error is None
