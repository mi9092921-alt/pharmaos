# @pharmaos/db — migration workflow

**Single source of schema truth:** `supabase/migrations/*.sql`, applied identically to the
local PostgreSQL 17 (Docker) and the Supabase cloud project. SQLAlchemy models **mirror** this
schema — they never generate it (no Alembic). Never edit `/supabase` by hand; add migrations only.

## Rules (from CLAUDE.md)

- Schema changes happen **only** through new migration files — never modify or delete an
  existing migration, never rewrite history.
- Every migration ships with: the migration itself, a **rollback** (paired down file), and
  **validation** (the CI up/down gate).
- Creation migrations use plain `CREATE INDEX` (tables are empty; `CONCURRENTLY` cannot run
  inside the transaction each migration is wrapped in). On a live production table, use
  `CREATE INDEX CONCURRENTLY` outside a transaction as a deliberate, scheduled operation.

## Layout

```
supabase/migrations/<version>_<name>.sql        # up migrations (Supabase CLI format)
packages/db/downs/<version>_<name>_down.sql     # paired rollback for each migration
packages/db/scripts/apply-migrations.sh         # ordered, transactional, tracked applier
packages/db/scripts/verify-up-down.sh           # CI gate: up → down (reverse) → clean → re-up
packages/db/seeds/                              # idempotent seed SQL (generated + static)
```

## Everyday commands

```bash
# Local dev (canonical path — Supabase CLI):
supabase db reset          # recreate local DB from all migrations
supabase db push           # apply pending migrations to the linked project

# CI / scratch verification (identical semantics, plain psql):
DATABASE_URL=postgresql://... packages/db/scripts/apply-migrations.sh
DATABASE_URL=postgresql://... packages/db/scripts/verify-up-down.sh
```

## Cloud project

Linking the Supabase cloud project (`supabase link --project-ref <ref>`) requires the project
credentials and is performed when they are provided. The cloud project **must** run PostgreSQL 17
(same major as local — one migration stream for both).
