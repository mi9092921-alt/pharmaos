-- Returned-stock disposition policy (P2 review fix C6, applies plan decision D3).
--
-- Prior behaviour (P2-M7) returned stock straight back into the ORIGINAL batch
-- as sellable — the plan's D3 ALTERNATIVE, not its recommendation. D3 recommends
-- returned stock enter QUARANTINE by default (pharmacist review before resale —
-- pharmacologically safer), with an optional per-branch setting to allow
-- returning it directly to sellable. This flag backs that policy; the return
-- service now lands returned units in a distinct batch whose status is
-- 'quarantined' by default (safe) or 'active' when the branch opts in.
--
-- Default FALSE = quarantine (the safe default). No data backfill needed.

ALTER TABLE settings
    ADD COLUMN returned_stock_to_active BOOLEAN NOT NULL DEFAULT FALSE;
