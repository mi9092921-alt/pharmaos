-- 20260711002800_notifications_down.sql
-- Paired down for the notifications table (P3-M7). Pure addition — drop in reverse.

DROP INDEX IF EXISTS idx_notifications_unread;
DROP INDEX IF EXISTS idx_notifications_branch_created;
DROP TABLE IF EXISTS notifications;
