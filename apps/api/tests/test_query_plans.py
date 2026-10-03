"""Query-plan guards for the latency-critical hot paths.

CLAUDE.md sets hard latency targets (barcode scan -> display < 50ms, search
< 100ms). Those are verified END-TO-END on the target device
(docs/pilot-checklist.md) — a wall-clock assert on a shared, CPU-throttled CI
runner is flaky and meaningless (it measured latency, not correctness, and
produced false failures).

Instead we guard the STRUCTURAL guarantee that actually delivers those targets,
deterministically: each hot query MUST be served by its index, never a
sequential scan. Technique: `SET LOCAL enable_seqscan = off` makes the planner
reveal the index-backed path it would choose; we assert the expected index
appears in the EXPLAIN output. This is dataset-size- and machine-independent,
so it catches a dropped-index / seq-scan regression without ever flaking on
timing. (Matches CLAUDE.md's "EXPLAIN ANALYZE any query > 50ms" guidance.)
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def _plan(db_session: AsyncSession, explain_sql: str, *, no_bitmap: bool = False) -> str:
    """Return the EXPLAIN plan text with sequential scans disabled, so the
    planner exposes the index path the hot query relies on.

    no_bitmap also disables bitmap scans: use it when several partial indexes
    share a `WHERE NOT is_deleted` predicate and the planner might bitmap-scan a
    NON-ideal one for a given data distribution — forcing a plain index scan
    makes the composite index that satisfies the ORDER BY the deterministic
    choice. (Do NOT use it for GIN indexes — those are bitmap-only.)"""
    await db_session.execute(text("SET LOCAL enable_seqscan = off"))
    if no_bitmap:
        await db_session.execute(text("SET LOCAL enable_bitmapscan = off"))
    rows = (await db_session.execute(text(explain_sql))).scalars().all()
    return "\n".join(rows)


async def test_barcode_lookup_is_index_backed(db_session: AsyncSession) -> None:
    """POS scan resolves by exact barcode (sales_service.resolve_barcode) —
    must hit idx_barcodes_barcode (delivers the < 50ms scan target)."""
    plan = await _plan(
        db_session,
        "EXPLAIN SELECT id FROM medication_barcodes WHERE barcode = 'EXPLAIN-PROBE-123'",
    )
    assert "idx_barcodes_barcode" in plan, plan


async def test_arabic_fts_search_is_index_backed(db_session: AsyncSession) -> None:
    """Arabic FTS search (catalog_service.list_medications) must hit the GIN
    search_vector index idx_medications_fts (delivers the < 100ms search target)."""
    plan = await _plan(
        db_session,
        "EXPLAIN SELECT id FROM medications WHERE NOT is_deleted "
        "AND search_vector @@ plainto_tsquery('arabic_simple', normalize_arabic('كونجيستال'))",
    )
    assert "idx_medications_fts" in plan, plan


async def test_arabic_trigram_fallback_is_index_backed(db_session: AsyncSession) -> None:
    """The trigram typo/partial fallback must hit the normalized-name GIN index
    idx_medications_trgm."""
    plan = await _plan(
        db_session,
        "EXPLAIN SELECT id FROM medications "
        "WHERE normalize_arabic(trade_name_ar) % normalize_arabic('كونجستال')",
    )
    assert "idx_medications_trgm" in plan, plan


async def test_expiry_alert_scan_is_index_backed(db_session: AsyncSession) -> None:
    """P2-M4 expiry alerts scan a branch's ACTIVE batches by expiry horizon —
    must hit the partial idx_batches_expiry (branch_id, expiry_date) index."""
    plan = await _plan(
        db_session,
        "EXPLAIN SELECT b.id FROM medication_batches b "
        "WHERE b.branch_id = '00000000-0000-0000-0000-000000000001' "
        "AND b.status = 'active' AND b.quantity > 0 "
        "AND b.expiry_date <= CURRENT_DATE + 90",
    )
    assert "idx_batches_expiry" in plan, plan


async def test_batch_status_report_is_index_backed(db_session: AsyncSession) -> None:
    """P2-M4 batch reports filter a branch's batches by a selective (non-active)
    status — must hit idx_batches_branch_status (branch_id, status)."""
    plan = await _plan(
        db_session,
        "EXPLAIN SELECT id FROM medication_batches "
        "WHERE branch_id = '00000000-0000-0000-0000-000000000001' "
        "AND status = 'quarantined' AND NOT is_deleted",
    )
    assert "idx_batches_branch_status" in plan, plan


async def test_customer_name_search_is_index_backed(db_session: AsyncSession) -> None:
    """P2-M5 customer lookup by Arabic name (trigram) must hit the normalized-name
    GIN index idx_customers_name_trgm."""
    plan = await _plan(
        db_session,
        "EXPLAIN SELECT id FROM customers "
        "WHERE normalize_arabic(name) % normalize_arabic('محمد')",
    )
    assert "idx_customers_name_trgm" in plan, plan


async def test_controlled_substance_log_medication_scan_is_index_backed(
    db_session: AsyncSession,
) -> None:
    """P2-M8 — a pharmacist looking up one controlled drug's dispensing history
    must hit idx_controlled_log_medication (medication_id, created_at DESC)."""
    plan = await _plan(
        db_session,
        "EXPLAIN SELECT id FROM controlled_substance_log "
        "WHERE medication_id = '00000000-0000-0000-0000-000000000001' "
        "ORDER BY created_at DESC",
    )
    assert "idx_controlled_log_medication" in plan, plan


async def test_expenses_branch_date_scan_is_index_backed(db_session: AsyncSession) -> None:
    """P2-M9 — a branch's expense list/report over a date range must hit
    idx_expenses_branch_date (branch_id, expense_date DESC)."""
    plan = await _plan(
        db_session,
        "EXPLAIN SELECT id FROM expenses "
        "WHERE branch_id = '00000000-0000-0000-0000-000000000001' "
        "AND expense_date >= CURRENT_DATE - 30 AND NOT is_deleted "
        "ORDER BY expense_date DESC",
        # Several expenses indexes share `WHERE NOT is_deleted`; forcing a plain
        # index scan makes the composite (branch_id, expense_date DESC) index the
        # deterministic pick regardless of how many rows the test DB accumulated.
        no_bitmap=True,
    )
    assert "idx_expenses_branch_date" in plan, plan


async def test_sales_report_scan_is_index_backed(db_session: AsyncSession) -> None:
    """P3-M1 — a branch's sales report (summary/trend/top-items) scans invoices by
    (branch_id, created_at range) over a local-day window. Must hit the partial
    idx_invoices_branch_created (branch_id, created_at) rather than the
    date-leading idx_invoices_date, delivering the < 3s daily-report budget."""
    plan = await _plan(
        db_session,
        "EXPLAIN SELECT id FROM invoices "
        "WHERE branch_id = '00000000-0000-0000-0000-000000000001' "
        "AND NOT is_deleted AND created_at >= CURRENT_DATE - 30 "
        "AND created_at < CURRENT_DATE + 1 "
        "ORDER BY created_at",
        # invoices carries several branch-leading indexes (uq_invoices_number,
        # idx_invoices_cash_session, idx_invoices_customer). ORDER BY created_at +
        # no_bitmap makes (branch_id, created_at) the deterministic pick — it alone
        # serves both the branch equality and the time order without a sort.
        no_bitmap=True,
    )
    assert "idx_invoices_branch_created" in plan, plan
