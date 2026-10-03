# Phase 2 — Acceptance Criteria Harvest (P2-M12)

Maps the Phase-2 acceptance criteria (pharmaos.md §Phase-2, CLAUDE.md) to their
implementation and the automated tests that prove them. Verified on `main` with
the full gate set green (migrations up/down/re-up ×24, ruff/black/mypy strict,
229 pytest on PostgreSQL 17, and the JS toolchain in CI).

| Acceptance criterion (spec)                                                                          | Status         | Where                                                              |
| ---------------------------------------------------------------------------------------------------- | -------------- | ------------------------------------------------------------------ |
| Full supplier management + purchase orders (request → approve → receive, 2D goods-in)                | ✅             | M1/M2 · `purchase_service` · `test_purchases_*`                    |
| Batch tracking: expiry alerts 30/60/90, auto-quarantine, batch reports                               | ✅             | M4 · `inventory_service` · `test_batch_tracking_m4`                |
| FEFO enforced; sale of a quarantined/expired batch blocked (E-STK-002)                               | ✅             | `sales_service` FEFO + guard · `test_batch_tracking_m4`            |
| Customers + loyalty (earn + **redeem as discount**)                                                  | ✅             | M5 + review C3 · `customer_service` · `test_customers_m5`          |
| VAT per `tax_profiles`, shown on the invoice/receipt                                                 | ✅             | M6 · `tax_service` · `test_vat_m6`                                 |
| Full credit-note return flow; original invoice never modified (rule 14)                              | ✅             | M7 · `return_service` · `test_returns_m7`                          |
| Returned stock disposition — quarantine by default (pharmacist review)                               | ✅             | review C6 (plan D3) · `settings.returned_stock_to_active`          |
| Prescriptions + **append-only** controlled-substance register (never deleted)                        | ✅             | M8 · DB trigger · `test_prescriptions_m8`                          |
| Expenses + categories; cash expenses reconcile with the cashier Z-report                             | ✅             | M9 + review C5 · `expense_service`/`cashier_service`               |
| 2D pack scan → capture serial/batch/expiry (receive) → linked at dispense                            | ✅             | M3 + review C1/C2 · `pack_serial_service` · `test_pack_serials_m3` |
| Duplicate pack serial rejected (decree 804 — E-TT-002); serial↔batch match at dispense (E-TT-003)   | ✅             | `pack_serial_service` · `test_pack_serials_m3`                     |
| ETA e-receipt: 24h offline → queue accumulates → drains fully, no loss; QR carries the UUID          | ✅ (simulator) | M10 · `ereceipt_service` · `test_ereceipt_m10`                     |
| EDA track & trace: 2D scan → capture → **reported event**; offline backlog drains; pre-launch import | ✅ (simulator) | M11 · `tt_service` · `test_tt_events_m11`                          |
| Full permission-matrix test suite; CSRF on every mutation                                            | ✅             | `test_rbac` + per-milestone permission/CSRF tests                  |

## Compliance gate (committed architectural decision)

The ETA and EDA modules are built on a **Port/Adapter with a local simulator**.
All queue/build/sign/submit/report logic is complete and tested against the
simulator. **No real ETA/EDA acceptance is claimed without real credentials** —
the criteria above marked "(simulator)" are functionally complete and their
final production acceptance is explicitly **pending credentials**:

- ETA: digital taxpayer profile + per-POS `client_id`/`secret` + X.509 seal.
- EDA: facility registration + the approved reporting channel (portal/app/provider).

When credentials arrive, wire `HttpEtaAdapter` / `HttpEdaTtAdapter` (the stubs are
in place) and flip the adapter selector — no other logic changes.

## Ratified deviations (kept, per the Phase-2 plan)

- **Printing** is network ESC/POS from the API (TCP 9100), not Electron main
  (D8) — revisit after the on-device pilot.
- **Expired batches** use the distinct status `expired` (not `quarantined`),
  both blocking sale (D4).

## Remaining before production (unchanged from Phase 1)

On-device pilot (`docs/pilot-checklist.md`), Supabase link (D2), backup cloud
bucket (D5), and real ETA/EDA credentials.
