# M0 — shelf_location classification (skeleton, DB untouched)

> Scope: `branch_inventory.shelf_location VARCHAR(50)` (`20260711000900_inventory.sql:40`).
> Baseline: `main@30af2b4`. No migration applied in this change.

## 1) How to run (read-only)

```bash
DATABASE_URL=postgresql://... packages/db/scripts/preflight-shelf-locations.sh
```

Script location (ratified): `packages/db/scripts/preflight-shelf-locations.sh`.

## 2) Result table (fill on the real pharmacy DB)

| Category                                        | Count | Action at migration                                                                                                     |
| ----------------------------------------------- | ----- | ----------------------------------------------------------------------------------------------------------------------- |
| total rows                                      | _TBD_ | —                                                                                                                       |
| NULL (no location recorded)                     | _TBD_ | keep NULL; NOT an error, NOT "intentional clear"                                                                        |
| blank/whitespace only → NULL                    | _TBD_ | normalize to NULL, no audit                                                                                             |
| valid or normalizable (`a-3`→`A3`, `B١٢`→`B12`) | _TBD_ | in-place UPDATE, `IS DISTINCT FROM` only                                                                                |
| invalid (`A0`, `A01`, `أ1`, `Ａ１`, `A100`)     | _TBD_ | → NULL **+ one `audit_logs` row each** (`settings.changed`, `reason=shelf_norm_migration`, old value in `metadata.old`) |

Top-100 invalid originals: paste output here.

## 3) Decisions locked for M1

- Migration never aborts boot: invalid converts (with audit), it does not raise.
- `LOCK TABLE branch_inventory IN ACCESS EXCLUSIVE MODE` first, then re-assert inside the same tx.
- Plain `CHECK` (no `NOT VALID`), `varchar_pattern_ops` partial index (filter-only; numeric sort stays expression-based, verified by `EXPLAIN (ANALYZE, BUFFERS)`).
- `touch_row` bumps on normalized rows only — accepted and documented; down does not reverse data.
- Perf gate fixed: `list_inventory` + shelf filter P95 < 100ms on **20,000 seeded `branch_inventory` rows**; plus a migration test on data with intentional invalids.
- Labels: `window.print()` A4 + `@page` (receipt-screen pattern), no new Electron IPC, no barcode (large code text only).
- Excel import: multipart `POST` + `enforce_csrf`, `openpyxl` read-only + size/row cap, `dry_run=true` stateless then full re-validate on execute, duplicate-row-in-file rejected, one batch audit + summary, permission `inventory.edit`.
- Alert `missing_location`: one per branch (`entity_type='branch'`, `count` in params, `cached_quantity > 0` only), `warning`, dual i18n keys (`alerts.msg_x` + `alerts.msg.x`), boot/CLI/manual evaluation, per-rule `SAVEPOINT` isolation.
- Filter/sort contract: `shelf=B7` exact, `zone=B` prefix; `sort=location` = `(zone, seq, trade_name)` with `NULLS LAST`, replacing the `low_stock` order explicitly.
- PATCH contract: `PATCH /inventory/{medication_id}/stock-settings` + `exclude_unset` (absent=no-op, explicit `null`/`""`=clear); receive paths write only when current is `NULL`, both audited.
