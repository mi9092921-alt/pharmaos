-- Link cash expenses to the cashier session (P2 review fix C5).
--
-- A cash expense paid out of the drawer must reduce the session's expected
-- cash — otherwise close_session shows a phantom OVERAGE equal to the cash
-- taken out for expenses (the same class of bug the P2-M7→M10 fix closed for
-- refunds). expenses.cash_session_id ties a cash expense to the open session at
-- the moment it is recorded; the cashier Z-report then subtracts in-session cash
-- expenses from expected_cash. Nullable: an expense recorded with no open
-- session (or a non-cash expense) simply carries no session link.

ALTER TABLE expenses
    ADD COLUMN cash_session_id UUID REFERENCES cash_sessions(id);

CREATE INDEX idx_expenses_session ON expenses(cash_session_id)
    WHERE cash_session_id IS NOT NULL AND NOT is_deleted;
