-- Down migration for 20260711001900_expenses.sql
-- Reverse order: the operational table (references expense_categories) first.
DROP TABLE IF EXISTS expenses;
DROP TABLE IF EXISTS expense_categories;
