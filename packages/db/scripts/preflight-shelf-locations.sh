#!/usr/bin/env bash
# M0 pre-upgrade classifier — READ-ONLY. Classifies every
# branch_inventory.shelf_location into: valid / normalizable / empty->NULL /
# invalid (migration-time NULL+AUDIT, never a boot-blocking abort).
#
# Location in repo (ratified): packages/db/scripts/preflight-shelf-locations.sh
# Usage: DATABASE_URL=postgresql://... packages/db/scripts/preflight-shelf-locations.sh
#
# Normalization mirrors normalize_shelf_code() in
# supabase/migrations/20260711003100_shelf_location_norm.sql EXACTLY
# (ASCII translate, no locale UPPER; NBSP via CHR(160), never a pasted byte).
set -euo pipefail
export PGCLIENTENCODING=UTF8

: "${DATABASE_URL:?DATABASE_URL is required}"
PSQL=(psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q -P pager=off)

NORM="NULLIF(regexp_replace(regexp_replace(translate(coalesce(v,''), 'abcdefghijklmnopqrstuvwxyz', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'), CHR(160), '', 'g'), '[\s\-_]+', '', 'g'), '')"

echo "== shelf_location classification (read-only) =="
"${PSQL[@]}" -c "
WITH raw AS (
  SELECT branch_id, medication_id, shelf_location AS v FROM branch_inventory WHERE NOT is_deleted
),
norm AS (
  SELECT branch_id, medication_id, v,
         NULLIF(regexp_replace(regexp_replace(translate(coalesce(v,''),
           'abcdefghijklmnopqrstuvwxyz٠١٢٣٤٥٦٧٨٩',
           'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'), CHR(160), '', 'g'), '[\s\-_]+', '', 'g'), '') AS n
  FROM raw
)
SELECT
  COUNT(*)                                          AS total_rows,
  COUNT(*) FILTER (WHERE v IS NULL)                 AS null_rows,
  COUNT(*) FILTER (WHERE v IS NOT NULL AND n IS NULL) AS blank_to_null,
  COUNT(*) FILTER (WHERE n ~ '^[A-Z][1-9][0-9]?\$')  AS valid_or_normalizable,
  COUNT(*) FILTER (WHERE n IS NOT NULL AND n !~ '^[A-Z][1-9][0-9]?\$') AS invalid_to_null_with_audit
FROM norm;
"

echo "== distinct invalid values (will become NULL + audit_logs, migration never aborts boot) =="
"${PSQL[@]}" -c "
WITH norm AS (
  SELECT DISTINCT shelf_location AS v,
         NULLIF(regexp_replace(regexp_replace(translate(coalesce(shelf_location,''),
           'abcdefghijklmnopqrstuvwxyz٠١٢٣٤٥٦٧٨٩',
           'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'), CHR(160), '', 'g'), '[\s\-_]+', '', 'g'), '') AS n
  FROM branch_inventory WHERE NOT is_deleted AND shelf_location IS NOT NULL
)
SELECT v AS original, n AS normalized
FROM norm WHERE n IS NOT NULL AND n !~ '^[A-Z][1-9][0-9]?\$'
ORDER BY v LIMIT 100;
"

echo "OK: preflight complete. Invalid rows (if any) convert to NULL + audit at migration time."
