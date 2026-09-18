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


async def test_movement_report_scan_is_index_backed(db_session: AsyncSession) -> None:
    """P3-M2 — movement analysis (fast/slow movers, by-type totals) scans
    stock_movements by (branch_id, movement_type, created_at range). Must hit
    idx_movements_branch_type_created rather than the batch_id-only
    idx_movements_batch."""
    plan = await _plan(
        db_session,
        "EXPLAIN SELECT id FROM stock_movements "
        "WHERE branch_id = '00000000-0000-0000-0000-000000000001' "
        "AND movement_type = 'sale_out' AND NOT is_deleted "
        "AND created_at >= CURRENT_DATE - 30 AND created_at < CURRENT_DATE + 1",
    )
    assert "idx_movements_branch_type_created" in plan, plan


async def test_valuation_report_scan_is_index_backed(db_session: AsyncSession) -> None:
    """P3-M2 — per-medication valuation groups ACTIVE batches for a branch,
    joined to medications, GROUP BY medication_id/trade_name/trade_name_ar,
    ORDER BY value DESC. This is the query EXACTLY as inventory_service.
    inventory_valuation_report writes it (join, full GROUP BY, ORDER BY,
    OFFSET/LIMIT included) — not a stripped-down proxy.

    That distinction matters: an earlier version of this guard checked a
    simplified probe (SELECT medication_id, quantity, purchase_price ...
    ORDER BY medication_id, no join, no GROUP BY) and went flaky once the
    full test suite ran together — accumulated data from other tests shifted
    medication_batches' statistics enough that the simplified probe's tie
    between idx_batches_branch_med / idx_batches_branch_status /
    idx_batches_expiry (all three share the branch_id + status='active'
    partial predicate) landed on a different winner than it had on a smaller
    table. The REAL query, with its GROUP BY on medication_id specifically,
    does NOT share that ambiguity — verified deterministic against both a
    fresh database and the exact post-full-suite state that broke the old
    probe (idx_batches_branch_med's (branch_id, medication_id) column order
    lets the aggregation use an Incremental Sort off an already-partially-
    sorted index scan, a real structural advantage the other two candidate
    indexes don't have, not just a cost-estimate tie-break)."""
    plan = await _plan(
        db_session,
        "EXPLAIN SELECT b.medication_id, m.trade_name, m.trade_name_ar, "
        "SUM(b.quantity) AS qty, SUM(b.quantity * b.purchase_price) AS value "
        "FROM medication_batches b JOIN medications m ON m.id = b.medication_id "
        "WHERE b.branch_id = '00000000-0000-0000-0000-000000000001' "
        "AND NOT b.is_deleted AND b.status = 'active' AND b.quantity > 0 "
        "GROUP BY b.medication_id, m.trade_name, m.trade_name_ar "
        "ORDER BY value DESC, qty DESC OFFSET 0 LIMIT 50",
    )
    assert "idx_batches_branch_med" in plan, plan
    assert "Seq Scan" not in plan, plan


async def test_expiry_trend_scan_is_index_backed(db_session: AsyncSession) -> None:
    """P3-M3 — the forward expiry trend groups ACTIVE batches for a branch by
    week-of-expiry. The un-wrapped version of this query (WHERE branch_id +
    status='active', no ORDER BY) matched THREE overlapping partial indexes
    equally well (idx_batches_branch_med, idx_batches_branch_status,
    idx_batches_expiry) — caught FLAKY when the full suite ran against a
    genuinely fresh database and the planner picked a different one than it
    had on the long-lived dev database this guard was first written against.
    The real service query was restructured with an inner ORDER BY
    expiry_date subquery specifically to fix this (not just to satisfy the
    test) — verified deterministic across multiple independent database
    instances before adopting it, matching exactly what production runs."""
    plan = await _plan(
        db_session,
        "EXPLAIN SELECT week_idx, COUNT(*), SUM(qty), SUM(qty * price) FROM ("
        "  SELECT LEAST(FLOOR((expiry_date - CURRENT_DATE) / 7.0), 12)::int AS week_idx, "
        "         quantity AS qty, purchase_price AS price FROM medication_batches "
        "  WHERE branch_id = '00000000-0000-0000-0000-000000000001' "
        "  AND NOT is_deleted AND status = 'active' AND quantity > 0 "
        "  AND expiry_date >= CURRENT_DATE AND expiry_date <= CURRENT_DATE + 90 "
        "  ORDER BY expiry_date"
        ") sub GROUP BY week_idx",
    )
    assert "idx_batches_expiry" in plan, plan
    assert "Seq Scan" not in plan, plan


async def test_expiry_waste_scan_is_index_backed(db_session: AsyncSession) -> None:
    """P3-M3 — waste value swept in a date range filters stock_movements by
    (branch_id, movement_type='expiry_writeoff', created_at range) — the exact
    shape idx_movements_branch_type_created (P3-M2) was built for; reused
    here, not a new index."""
    plan = await _plan(
        db_session,
        "EXPLAIN SELECT id FROM stock_movements "
        "WHERE branch_id = '00000000-0000-0000-0000-000000000001' "
        "AND movement_type = 'expiry_writeoff' AND NOT is_deleted "
        "AND created_at >= CURRENT_DATE - 30 AND created_at < CURRENT_DATE + 1",
    )
    assert "idx_movements_branch_type_created" in plan, plan
