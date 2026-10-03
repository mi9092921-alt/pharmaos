"""Audit-log writer (CLAUDE.md append-only audit trail).

Two write patterns:

- record(session, ...): enqueues the audit row in the CALLER's transaction, so
  the audit entry and the audited state change commit or roll back together
  (correct for successful operations — invoice.created commits with the invoice).

- record_independent(...): writes the audit row in its OWN transaction/session,
  so it persists even if the surrounding operation fails. Use for failure and
  incident events that must be recorded regardless (ereceipt.failed,
  cash_session.discrepancy, sync.failed).

`metadata` must never contain sensitive data (passwords, national IDs, tokens);
it holds structured, non-sensitive context only.
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from pharmaos_api.audit import AUDITED_OPERATIONS
from pharmaos_api.models import AuditLog
from pharmaos_api.models.user import User


def _validate_action(action: str) -> None:
    if action not in AUDITED_OPERATIONS:
        # Programming error — an unregistered action code was used.
        raise ValueError(f"unknown audit action: {action!r}")


def _build_entry(
    *,
    action: str,
    actor: User | None,
    branch_id: uuid.UUID | None,
    entity_type: str | None,
    entity_id: uuid.UUID | None,
    ip_address: str | None,
    metadata: dict[str, Any] | None,
) -> AuditLog:
    return AuditLog(
        action=action,
        actor_user_id=actor.id if actor is not None else None,
        actor_username=actor.username if actor is not None else None,
        branch_id=branch_id,
        entity_type=entity_type,
        entity_id=entity_id,
        ip_address=ip_address,
        metadata_=metadata or {},
    )


async def record(
    session: AsyncSession,
    action: str,
    *,
    actor: User | None = None,
    branch_id: uuid.UUID | None = None,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    ip_address: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Add an audit entry to the caller's transaction (does NOT commit)."""
    _validate_action(action)
    session.add(
        _build_entry(
            action=action,
            actor=actor,
            branch_id=branch_id,
            entity_type=entity_type,
            entity_id=entity_id,
            ip_address=ip_address,
            metadata=metadata,
        )
    )
    await session.flush()


async def record_independent(
    action: str,
    *,
    actor: User | None = None,
    branch_id: uuid.UUID | None = None,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    ip_address: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Write an audit entry in a fresh, independent transaction (persists on failure)."""
    _validate_action(action)
    from pharmaos_api.db import get_session_factory

    async with get_session_factory()() as session:
        session.add(
            _build_entry(
                action=action,
                actor=actor,
                branch_id=branch_id,
                entity_type=entity_type,
                entity_id=entity_id,
                ip_address=ip_address,
                metadata=metadata,
            )
        )
        await session.commit()
