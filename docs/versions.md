# Version Matrix & Upgrade Log

> Governing rule (CLAUDE.md): **mutual compatibility first, then newest.** All versions are
> pinned in lockfiles; no open ranges (`^`/`~`) for critical packages. Upgrades happen as one
> tested wave, between phases only. Every upgrade is recorded here with its rationale.

## Approved matrix (from CLAUDE.md v1.1 — verified & pinned at project start)

### Runtime

| Package | Target | Notes                                    |
| ------- | ------ | ---------------------------------------- |
| Node.js | 22 LTS | Next.js 16 requires Node 20+.            |
| Python  | 3.12   | Stable across the full dependency chain. |

### Frontend

| Package               | Target                               |
| --------------------- | ------------------------------------ |
| Next.js               | 16.x (App Router, Turbopack default) |
| React                 | 19.x                                 |
| TypeScript            | 5.x (strict)                         |
| Tailwind CSS          | 4.x                                  |
| shadcn/ui             | latest compatible with Tailwind 4    |
| Zustand               | 5.x                                  |
| TanStack Query        | 5.x                                  |
| React Hook Form + Zod | latest stable                        |
| Recharts              | latest stable                        |

### Desktop

| Package          | Target                                               |
| ---------------- | ---------------------------------------------------- |
| Electron         | latest stable line (>=33), pinned after verification |
| electron-builder | compatible with the pinned Electron line             |

### Backend

| Package                                | Target                             |
| -------------------------------------- | ---------------------------------- |
| FastAPI                                | >=0.115 (Pydantic v2 compatible)   |
| SQLAlchemy                             | 2.0 (async)                        |
| Pydantic                               | v2                                 |
| Celery                                 | 5.x                                |
| WeasyPrint                             | latest stable (A4/A5 reports only) |
| python-barcode + GS1 DataMatrix parser | latest stable                      |

### Database / infra

| Package      | Target                                                   |
| ------------ | -------------------------------------------------------- |
| PostgreSQL   | 17 (local Docker; must match the Supabase project major) |
| Supabase CLI | latest stable                                            |
| Redis        | 7.x                                                      |

Migrations: SQL via Supabase CLI only (`supabase/migrations/`). No Alembic — SQLAlchemy models
mirror the schema, they are not its source.

## Toolchain pins provisionally applied in P0-M1 (root devDependencies)

These are the dev toolchain pins committed in `package.json`. Per the version policy, the first
`pnpm install` on a networked machine is the **verification gate**: it resolves the lockfile and,
where a newer compatible stable exists, the pin is bumped as one wave and recorded below.

| Package                         | Provisional pin |
| ------------------------------- | --------------- |
| typescript                      | 5.7.2           |
| turbo                           | 2.3.3           |
| eslint / @eslint/js             | 9.17.0          |
| typescript-eslint               | 8.18.1          |
| prettier                        | 3.4.2           |
| husky                           | 9.1.7           |
| lint-staged                     | 15.2.11         |
| @commitlint/cli                 | 19.6.1          |
| @commitlint/config-conventional | 19.6.0          |

## Environment reconciliation (open — tracked under decision D0)

The current authoring sandbox differs from the pinned targets and must be reconciled where Phase 0
is actually built/verified:

| Item               | Target    | Sandbox           | Action                                                 |
| ------------------ | --------- | ----------------- | ------------------------------------------------------ |
| Node.js            | 22 LTS    | 24.x              | CI pins Node 22; `.nvmrc` = 22.                        |
| Python             | 3.12      | 3.9               | Provision 3.12 on the build/verify machine.            |
| Docker + Compose   | required  | absent            | DB-dependent steps run on a Docker-enabled machine/CI. |
| PostgreSQL / Redis | 17 / 7    | absent            | Provided via Docker Compose (P0-M2).                   |
| Supabase CLI       | latest    | absent            | Installed on the build/verify machine (P0-M3).         |
| Package registries | reachable | egress restricted | Allowlist required before `pnpm install`.              |

## Python pins — resolved & verified on Python 3.12.13 / PostgreSQL 17.8 (2026-07)

Installed, tested (24 tests green incl. live login + backup/restore drill), and pinned in
`apps/api/pyproject.toml`: fastapi 0.139.0 · uvicorn 0.51.0 · sqlalchemy 2.0.51 ·
asyncpg 0.31.0 · pydantic 2.13.4 · pydantic-settings 2.14.2 · argon2-cffi 25.1.0 ·
PyJWT 2.13.0 · cryptography 49.0.0 · keyring 25.7.0 · celery 5.6.3 · redis 8.0.1 ·
httpx 0.28.1 · pytest 9.1.1 · pytest-asyncio 1.4.0 · ruff 0.15.21 · black 26.5.1 · mypy 2.2.0.
All satisfy the CLAUDE.md matrix (FastAPI ≥0.115 / Pydantic v2 / SQLAlchemy 2.0 async).

## JS pins — RESOLVED & verified (2026-07, first networked install, P1-M2)

`pnpm install` succeeded once `registry.npmjs.org` was allowlisted; `pnpm-lock.yaml` is committed
(the version-policy gate). Verified by `pnpm typecheck` (shared/ui/web) + a clean **Next.js 16
production build** of apps/web.

next 16.0.0 · react/react-dom 19.0.0 · tailwindcss + @tailwindcss/postcss 4.3.2 · zustand 5.0.0 ·
@tanstack/react-query 5.59.0 · react-hook-form 7.53.0 · zod 3.23.8 · @hookform/resolvers 3.9.1 ·
@fontsource-variable/cairo 5.2.7 · @fontsource-variable/inter 5.2.8 · typescript 5.7.2 ·
eslint 9.17.0 · typescript-eslint 8.18.1 · prettier 3.4.2 · turbo 2.3.3.

Wave adjustments at this gate (compatibility-first, then newest):

- **tailwindcss / @tailwindcss/postcss 4.0.0 → 4.3.2**: 4.0.0's oxide scanner is incompatible with
  Next 16 Turbopack (`Missing field 'negated' on ScannerOptions.sources`); 4.3.2 builds clean.
- **Fonts: next/font/google → self-hosted @fontsource-variable (Cairo + Inter)**: Google Fonts is
  fetched at build time, breaking offline/reproducible builds (CLAUDE.md offline-first). Self-hosting
  the same design-system fonts removes the build- and run-time Google dependency.
- Dropped `erasableSyntaxOnly` from packages/shared tsconfig (needs TS 5.8; pinned TS is 5.7). Strip
  safety stays enforced at runtime by the RBAC generator running under Node type-stripping.

## Upgrade log

- **2026-07 — Initial matrix (v1.1).** Adopted the CLAUDE.md v1.1 matrix as the project baseline.
  Provisional JS toolchain pins committed in P0-M1 (validation gate: first networked install).
  Python matrix resolved, installed and verified against PostgreSQL 17.8 in P0-M6..M9.
