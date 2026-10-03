#!/usr/bin/env bash
# Migration reversibility check (CI gate — CLAUDE.md: "Database migrations تعمل وتُعكس up/down").
#
# Procedure, against a DISPOSABLE database:
#   1. UP    : apply every migration in order.
#   2. DOWN  : apply every paired down-migration in strict REVERSE order.
#   3. CLEAN : assert no application objects remain (schema returns to baseline).
#   4. RE-UP : apply every migration again (proves downs leave a re-appliable state).
#
# Every up migration MUST have a paired down file:
#   supabase/migrations/<version>.sql  <->  packages/db/downs/<version>_down.sql
#
# Usage: DATABASE_URL=postgresql://... packages/db/scripts/verify-up-down.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MIGRATIONS_DIR="$REPO_ROOT/supabase/migrations"
DOWNS_DIR="$REPO_ROOT/packages/db/downs"
: "${DATABASE_URL:?DATABASE_URL is required}"

PSQL=(psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q)

# Glob expansion is lexicographically sorted — matches Supabase CLI apply order.
shopt -s nullglob
ups=("$MIGRATIONS_DIR"/*.sql)
[[ ${#ups[@]} -gt 0 ]] || { echo "FAIL: no migrations found in $MIGRATIONS_DIR"; exit 1; }

echo "== pairing check =="
for up in "${ups[@]}"; do
  version="$(basename "$up" .sql)"
  down="$DOWNS_DIR/${version}_down.sql"
  [[ -f "$down" ]] || { echo "FAIL: missing down migration for $version"; exit 1; }
  echo "pair ok: $version"
done

echo "== UP =="
for up in "${ups[@]}"; do
  echo "up     : $(basename "$up")"
  "${PSQL[@]}" -1 -f "$up"
done

echo "== DOWN (reverse) =="
for ((i = ${#ups[@]} - 1; i >= 0; i--)); do
  version="$(basename "${ups[$i]}" .sql)"
  echo "down   : $version"
  "${PSQL[@]}" -1 -f "$DOWNS_DIR/${version}_down.sql"
done

echo "== CLEAN check =="
leftover="$("${PSQL[@]}" -tAc "
  SELECT COUNT(*) FROM pg_tables
  WHERE schemaname = 'public' AND tablename <> '_pharmaos_migrations';")"
[[ "$leftover" == "0" ]] || { echo "FAIL: $leftover table(s) left after full down"; exit 1; }

echo "== RE-UP =="
for up in "${ups[@]}"; do
  "${PSQL[@]}" -1 -f "$up"
done

# Record applied versions so a subsequent apply-migrations.sh run composes
# cleanly (skips re-application, applies seeds only).
"${PSQL[@]}" -c "CREATE TABLE IF NOT EXISTS _pharmaos_migrations (
  version     TEXT PRIMARY KEY,
  applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);"
for up in "${ups[@]}"; do
  version="$(basename "$up" .sql)"
  "${PSQL[@]}" -c "INSERT INTO _pharmaos_migrations(version) VALUES ('$version')
                   ON CONFLICT (version) DO NOTHING;"
done

echo "OK: up/down/re-up verified for ${#ups[@]} migration(s)."
