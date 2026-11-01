"""Audited-operations registry (CLAUDE.md AUDITED_OPERATIONS).

Append-only registry — codes are added, never removed or renamed. These are the
critical operations that MUST be recorded in audit_logs. The set is transcribed
exactly from CLAUDE.md; wiring is added as each operation's feature ships.
"""


class AuditAction:
    # Sales
    INVOICE_CREATED = "invoice.created"
    INVOICE_CANCELLED = "invoice.cancelled"
    INVOICE_DISCOUNT_APPLIED = "invoice.discount_applied"
    INVOICE_PRICE_OVERRIDDEN = "invoice.price_overridden"
    RETURN_CREATED = "return.created"
    # Inventory
    STOCK_ADJUSTED = "stock.adjusted"
    MEDICATION_DELETED = "medication.deleted"
    CATALOG_IMPORTED = "catalog.imported"
    CATALOG_EXPORTED = "catalog.exported"
    BATCH_EXPIRED_DISPENSED = "batch.expired_dispensed"
    BATCH_QUARANTINED = "batch.quarantined"
    CONTROLLED_SUBSTANCE_DISPENSED = "controlled_substance.dispensed"
    # Stocktake (الجرد)
    STOCKTAKE_CREATED = "stocktake.created"
    STOCKTAKE_COUNT_RECORDED = "stocktake.count_recorded"
    STOCKTAKE_SUBMITTED = "stocktake.submitted"
    STOCKTAKE_APPROVED = "stocktake.approved"
    STOCKTAKE_CANCELLED = "stocktake.cancelled"
    # Shortages (النواقص)
    SHORTAGE_CREATED = "shortage.created"
    SHORTAGE_UPDATED = "shortage.updated"
    # Compliance (Egypt)
    ERECEIPT_SUBMITTED = "ereceipt.submitted"
    ERECEIPT_FAILED = "ereceipt.failed"
    TT_EVENT_REPORTED = "tt_event.reported"
    # Settings / users
    USER_CREATED = "user.created"
    USER_ROLE_CHANGED = "user.role_changed"
    USER_DEACTIVATED = "user.deactivated"
    SETTINGS_CHANGED = "settings.changed"
    BACKUP_CREATED = "backup.created"
    BACKUP_RESTORED = "backup.restored"
    # Cashier
    CASH_SESSION_OPENED = "cash_session.opened"
    CASH_SESSION_CLOSED = "cash_session.closed"
    CASH_SESSION_DISCREPANCY = "cash_session.discrepancy"
    # Sync
    SYNC_CONFLICT_RESOLVED = "sync.conflict_resolved"
    SYNC_FAILED = "sync.failed"


# The authoritative set (used to validate action codes before writing).
AUDITED_OPERATIONS: frozenset[str] = frozenset(
    value
    for name, value in vars(AuditAction).items()
    if not name.startswith("_") and isinstance(value, str)
)
