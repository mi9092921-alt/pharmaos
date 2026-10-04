# Phase 3 — Acceptance Criteria Harvest (P3-M8)

Maps the Phase-3 acceptance criteria (pharmaos.md §Phase-3, CLAUDE.md) to their
implementation and the automated tests that prove them. Verified on `main` with
the full gate set green (migrations up/down/re-up ×28, ruff/black/mypy strict,
302 pytest on PostgreSQL 17, and the JS toolchain — prettier/eslint/tsc/build).

| Acceptance criterion (spec)                                                                            | Status                      | Where                                                               |
| ------------------------------------------------------------------------------------------------------ | --------------------------- | ------------------------------------------------------------------- |
| Sales reports (daily / monthly / annual) with charts, CSV export                                       | ✅                          | P3-M1 · `reporting_service.sales_report` · `test_reports_m1`        |
| Inventory reports (stock level, movement, valuation, slow movers)                                      | ✅                          | P3-M2 · `inventory_service` reports · `test_reports_m2`             |
| Expiry analytics 30/60/90 + waste value + trend                                                        | ✅                          | P3-M3 · `test_reports_m3`                                           |
| Profit/loss analysis (COGS from batch cost, margins, operating)                                        | ✅                          | P3-M4 · `reporting_service.profit_loss_report` · `test_reports_m4`  |
| Supplier performance + customer analytics (RFM, loyalty)                                               | ✅                          | P3-M5 · `test_reports_m5`                                           |
| **Daily report < 3 seconds** (performance guard)                                                       | ✅ (42 ms measured)         | P3-M1/M8 · `test_hardening_m8` + `test_query_plans` + numbers below |
| Smart alerts fire per CLAUDE.md `ALERT_RULES` (12 rules)                                               | ✅                          | P3-M6 · `alerts_service` · `test_alerts_m6`                         |
| Alert dedup/lifecycle: repeats never duplicate; cleared ⇒ resolved; ack persists                       | ✅                          | P3-M6 · `uq_alerts_dedup_active` · `test_alerts_m6`                 |
| Notifications delivered: in-app + desktop; email queued behind provider gate                           | ✅ (email pending provider) | P3-M7 · `notification_service` · `test_notifications_m7`            |
| Full permission-matrix coverage for every new endpoint; CSRF on mutations                              | ✅                          | per-milestone tests + `test_hardening_m8` AST gate                  |
| Dashboards polished: empty/loading states, RTL, tabular-nums (M6 deferral: multi-branch banner rollup) | ✅                          | P3-M1..M7 UI + P3-M8 · `test_alerts_m6`/`test_hardening_m8` rollup  |

## Performance (the "< 3s daily report" criterion, measured)

Committed guard: `test_hardening_m8.py::test_daily_reports_meet_the_3s_budget_at_pilot_scale`
seeds synthetic pilot scale in a rolled-back transaction — 22k invoices / 44k
invoice lines across a year with a dense "today" (2k), plus 2k credit notes and
500 expenses — then runs the service layer end-to-end and asserts the daily
sales report, the daily P&L, and the annual sales report under the 3.0s budget
(measured reality is ~10–70x below it, so the wall-clock assert cannot flake a
shared CI runner; the structural EXPLAIN guards in `test_query_plans.py` remain
the primary regression fence). The annual P&L below is a manual measurement on
the same seed, outside the committed guard:

| Report (service call)               | Measured (local PG 17, fresh DB) | Budget |
| ----------------------------------- | -------------------------------- | ------ |
| Daily sales (`sales_report`, 1 day) | 42 ms (guarded)                  | < 3 s  |
| Daily P&L (`profit_loss_report`)    | 87 ms (guarded)                  | < 3 s  |
| Annual sales (365 days)             | 298 ms (guarded)                 | < 3 s  |
| Annual P&L (365 days)               | 640 ms (manual measurement)      | < 3 s  |

M4's independent drill (20k/365d, in-transaction, rolled back) measured
~25 ms daily / ~400 ms annual — same order of magnitude, no new index needed;
every hot path is served by the M1/M2 covering indexes (`idx_invoices_branch_created`,
`idx_movements_branch_type_created`, `idx_batches_expiry`, …), each pinned by a
query-plan guard.

## OWASP gate (P3-M8, automated in `test_hardening_m8.py`)

- **A03 Injection** — AST scan over the whole API package: no SQL may be built
  by interpolating runtime values (`text(f"... {param} ...")`, concatenation,
  `.format()` all fail CI). The only allowed interpolation is the documented
  constant-fragment composition (literal WHERE fragments joined with constant
  separators; values always travel via `.bindparams()`).
- **A01/A08 Access control & integrity** — AST scan: every POST/PUT/PATCH/DELETE
  endpoint must call `enforce_csrf` (the only exemptions are the pre-auth
  login/refresh endpoints — no session to abuse; login additionally
  rate-limited 5/min/IP). Permission matrices are tested per milestone.
- **A05 Security misconfiguration** — security headers verified on success AND
  error responses; routing rejections (404/405) now speak the unified error
  envelope (new append-only code `E-GEN-001`) instead of Starlette's bare
  `{"detail": ...}`; an unhandled exception returns the generic `E-SYS-001`
  envelope and never exception internals; Swagger/ReDoc/OpenAPI are all
  disabled on the device (`docs_url`/`redoc_url`/`openapi_url = None`).
- **A09 Logging/monitoring** — the `audit_logs` append-only immutability
  trigger is asserted present on the live schema (DB-enforced, not convention).
- Carried from earlier phases: argon2id passwords, RS256 JWTs with token
  versioning, OS-keystore key storage, login rate limiting, CSV formula-injection
  neutralization, HSTS cloud-only, API bound to 127.0.0.1.

## P3-M8 polish items

- **Multi-branch dashboard banner (closing the M6 deferral):** `GET
/alerts/summary` without `branch_id` rolls up live critical/danger counts
  across ALL branches; the dashboard banner uses the rollup so a second
  branch's emergency can never hide behind a first-branch-only query. Scoped
  summaries keep the exact M6 shape.
- **Windows encoding hardening (root fix):** the migration scripts now pin
  `PGCLIENTENCODING=UTF8`. Found while building a scratch verification DB on
  Windows: redirected `psql` fell back to the ANSI codepage and silently
  double-encoded Arabic literals (a mojibake `normalize_arabic` body). The
  canonical paths (CI on Linux, Supabase CLI) were never affected; the pin
  protects local Windows verification.

## Post-acceptance review fixes (2026-10-04)

An independent review-agent audit of the shipped Phase 3 confirmed six defects;
all are fixed with regression tests (309 pytest green, 302+7; all gates clean;
no new migration, no dependency, no breaking API change):

- **CSV formula injection closed on EVERY export (was stock-level only):** the
  P3-M5 supplier/customer CSV exports wrote free-text name/phone raw while the
  repo's own mitigation shipped only on the M2 stock-level CSV — the exact
  cross-role vector its docstring documents (cashier creates, manager exports).
  The mitigation now lives in the shared `services/csv_safe.py` and both M5
  exports sanitize; a leading `+` phone (ordinary international format) is
  covered, not just malicious payloads.
- **Customer analytics CSV export was always header-only (new defect, caught
  by the injection test):** the CSV passed `top_limit=0` into a report that
  sliced at `top_limit >= 0` — the M4 "0 = none" convention colliding with the
  M5 "0 = unlimited" convention the supplier export documents. Root-fixed to
  `top_limit > 0`; the router's `Query(ge=1)` keeps API callers from ever
  sending 0, both conventions are documented in the docstrings.
- **Waste report counts each swept batch ONCE:** a re-activated expired batch
  (permitted by `set_batch_status`) gets a second `expiry_writeoff` movement
  from the next sweep; the waste aggregation now groups by `batch_id` before
  summing, so its quantity/value can never silently double behind a count of 1.
- **Email queue drain is reachable from production:** new CLI
  `notifications-drain-email` (cron-able, the D6 pattern) plus a best-effort
  boot hook after alert evaluation. The default Noop provider keeps every row
  pending (nothing claimed, nothing lost); a configured provider marks
  `sent_at` on the actual send.
- **The notification bell and desktop toasts watch ALL branches:** the M8
  banner rollup's rationale extended to notifications —
  `GET /notifications/unread-count` takes an OPTIONAL `branch_id` and rolls up
  across all branches when omitted (scoped calls keep the old shape); the
  topbar consumes the rollup and feeds toasts per branch with the endpoint's
  100-row cap.
- **Soft-deleted medications leave the Phase-3 aggregates:** the stock-level
  summary counts the same rows the items list and CSV show (deleted
  medications' surviving cache rows excluded), and the stock/expiry alert
  evaluators no longer fire for items the catalog no longer carries. The
  pre-Phase-3 read models (`expiry_alerts`, `batch_status_report`,
  `drift_check`) are deliberately untouched.

Proving tests: M5 CSV injection (also proved the empty-CSV defect), M3
re-swept-batch waste consistency, M2 summary/items/CSV consistency, M6 deleted
meds fire nothing, M7 unread rollup + CLI drain through the real handler +
boot-drain wiring. Frontend changes verified by prettier/eslint/tsc/build (the
repo has no JS test runner); a manual multi-branch bell/toast smoke folds into
the standing device-pilot checklist.

## Honest scope notes (pending providers / deferred, per the plan)

- **Email channel:** queued behind the provider gateway (`sent_at` stays NULL
  until a real send); no SMTP provider configured in Phase 3 — acceptance is
  "pending provider" by design (ratified D5), nothing is claimed.
- **SMS:** deferred to Phase 4, never claimed.
- **No scheduler:** alert evaluation runs at boot / CLI / POST
  `/alerts/evaluate` (ratified D6); a Celery-beat style worker is an
  infrastructure follow-up.
- **`sync_failed` rule:** registered but permanently silent — the schema has
  no sync outbox to observe (cloud sync is Phase 4); it fires the day that
  source exists.
- **`backup_overdue` rule:** reads the real filesystem (`BACKUP_PATH`);
  cloud bucket (D5) is still pending infrastructure.

## Remaining before production (unchanged from Phase 2)

On-device pilot (`docs/pilot-checklist.md`), Supabase link (D2), backup cloud
bucket (D5), real ETA/EDA credentials, and an SMTP provider for the email
channel.
