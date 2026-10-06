"""Device first-run state (installer decision 7).

The wizard's state machine lives in PostgreSQL (``installation_state``,
migration 20260711003000) — never the filesystem: ``pgdata`` existence proves
nothing after a cluster swap, and a RESTORED database brings its own state
back, so a recovered device skips the wizard on its own. Every wizard step is
re-entrant: the UI reads this table (via the API) to decide what is still
missing instead of replaying a fixed script.

First run resumes after a mid-wizard interruption (decision 5): the resume
logic infers state by READING (does a super_admin exist? a branch?), never
from a filesystem marker, and ``setup_complete`` flips only after the last
step succeeds.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# The keys the schema seeds on creation — keep in lockstep with the migration.
KNOWN_KEYS = ("setup_complete", "setup_version", "last_completed_step")
# The pristine first-run state (the wizard flips setup_complete when the LAST
# step succeeds; a restored database brings its own values back).
DEFAULT_STATE = {
    "setup_complete": "0",
    "setup_version": "1",
    "last_completed_step": "none",
}


async def get_all(session: AsyncSession) -> dict[str, str]:
    rows = await session.execute(text("SELECT key, value FROM installation_state"))
    return {str(key): str(value) for key, value in rows.all()}


async def get(session: AsyncSession, key: str) -> str | None:
    value = await session.execute(
        text("SELECT value FROM installation_state WHERE key = :key"), {"key": key}
    )
    return value.scalar_one_or_none()


async def set_values(session: AsyncSession, values: dict[str, str]) -> None:
    """Upsert state keys (commits — wizard steps are discrete transactions)."""
    await session.execute(
        text(
            "INSERT INTO installation_state (key, value) "
            "VALUES (:key, :value) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()"
        ),
        [{"key": key, "value": value} for key, value in values.items()],
    )
    await session.commit()


async def is_setup_complete(session: AsyncSession) -> bool:
    return (await get(session, "setup_complete")) == "1"
