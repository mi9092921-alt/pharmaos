#!/usr/bin/env bash
# Apply all pending PharmaOS migrations, in filename order, each in ONE transaction
# (parity with Supabase CLI behavior). Tracks applied versions in _pharmaos_migrations.
#
# Canonical production path is the Supabase CLI (`supabase db push` / `supabase db reset`).
# This runner exists for CI and scratch-harness verification with identical semantics.
#
# Usage: DATABASE_URL=postgresql://... packages/db/scripts/apply-migrations.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MIGRATIONS_DIR="$REPO_ROOT/supabase/migrations"
: "${DATABASE_URL:?DATABASE_URL is required}"

PSQL=(psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q)

"${PSQL[@]}" -c "CREATE TABLE IF NOT EXISTS _pharmaos_migrations (
  version     TEXT PRIMARY KEY,
  applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);"

shopt -s nullglob
for file in "$MIGRATIONS_DIR"/*.sql; do
  version="$(basename "$file" .sql)"
  applied="$("${PSQL[@]}" -tAc "SELECT 1 FROM _pharmaos_migrations WHERE version = '$version'")"
  if [[ "$applied" == "1" ]]; then
    echo "skip   : $version (already applied)"
    continue
  fi
  echo "apply  : $version"
  "${PSQL[@]}" -1 -f "$file"
  "${PSQL[@]}" -c "INSERT INTO _pharmaos_migrations(version) VALUES ('$version');"
done

# Code-defined seeds run on EVERY migration run (CLAUDE.md RBAC v1.1:
# "تُبذَر من الكود عند كل migration" — code always wins). Seeds are idempotent.
SEEDS_DIR="$REPO_ROOT/packages/db/seeds"
if [[ -d "$SEEDS_DIR" ]]; then
  for seed in "$SEEDS_DIR"/*.sql; do
    echo "seed   : $(basename "$seed")"
    "${PSQL[@]}" -f "$seed"
  done
fi
echo "OK: all migrations applied."
