"""installation_state — the wizard's state machine in PostgreSQL (decision 7).

A restored database brings its own state back, so a recovered device skips
the wizard; the table (not pgdata existence) is the source of truth.

The tests reset the table to DEFAULT_STATE before AND after each case: the
suite shares ONE database and set_values() commits — hermetic or not at all.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.services import installation_state


@pytest.fixture(autouse=True)
async def _pristine_state(db_session: AsyncSession) -> AsyncSession:
    await installation_state.set_values(db_session, dict(installation_state.DEFAULT_STATE))
    yield db_session
    await installation_state.set_values(db_session, dict(installation_state.DEFAULT_STATE))


async def test_defaults_after_migrations(db_session: AsyncSession) -> None:
    state = await installation_state.get_all(db_session)
    assert state["setup_complete"] == "0"
    assert state["setup_version"] == "1"
    assert state["last_completed_step"] == "none"
    assert await installation_state.is_setup_complete(db_session) is False


async def test_set_values_upserts_and_commits(db_session: AsyncSession) -> None:
    await installation_state.set_values(
        db_session,
        {"last_completed_step": "admin_created", "setup_complete": "1"},
    )
    assert (await installation_state.get(db_session, "last_completed_step")) == "admin_created"
    assert (await installation_state.get(db_session, "setup_complete")) == "1"
    assert await installation_state.is_setup_complete(db_session) is True

    # Idempotent upsert — a resumed wizard rewrites keys without duplicates.
    await installation_state.set_values(db_session, {"last_completed_step": "branch_created"})
    assert (await installation_state.get(db_session, "setup_complete")) == "1"
    assert (await installation_state.get(db_session, "last_completed_step")) == "branch_created"
