# PharmaOS

Hybrid (offline-first + cloud) pharmacy management system. Production-grade, multi-country / multi-currency, with **Egypt (EGP)** as the primary market. Built by a single developer.

> The authoritative project specifications are **`CLAUDE.md`** and **`pharmaos.md`**.
> On any conflict, **`CLAUDE.md` wins**. This README is a convenience entry point, not a source of truth.

## Monorepo layout

```
PharmaOS/
├── apps/
│   ├── desktop/   # Electron + Next.js (local device app)
│   ├── web/       # Next.js web dashboard (cloud)
│   └── api/       # FastAPI backend (local + cloud)
├── packages/
│   ├── ui/        # Shared UI components (shadcn/ui + Tailwind 4, RTL-first)
│   ├── db/        # Migration helpers & schema utilities
│   └── shared/    # Shared types, permission matrix, error-code registry
├── supabase/      # Migrations only — do NOT edit directly
└── docs/          # versions.md and project docs
```

## Toolchain (targets)

| Tool       | Target      |
| ---------- | ----------- |
| Node.js    | 22 LTS      |
| pnpm       | 10.x        |
| Python     | 3.12        |
| PostgreSQL | 17 (Docker) |
| Redis      | 7 (Docker)  |

Exact pins live in `docs/versions.md` and in the lockfiles. See the version policy in `CLAUDE.md`.

## Local development — quickstart

```bash
# 1) Toolchain + infrastructure
pnpm install
cp .env.example .env            # set POSTGRES_PASSWORD (and DATABASE_URL to match)
docker compose up -d            # PostgreSQL 17 + Redis 7 (localhost-only)

# 2) Schema + seeds (single migration stream; RBAC is seeded from code)
DATABASE_URL=postgresql://pharmaos:<password>@localhost:5432/pharmaos \
  packages/db/scripts/apply-migrations.sh

# 3) Backend (Python 3.12)
python3.12 -m pip install -e "apps/api[dev]"
python3.12 -m pharmaos_api.cli bootstrap-admin --username admin --full-name "مالك النظام"
python3.12 -m uvicorn pharmaos_api.main:app --host 127.0.0.1 --port 8000

# 4) Web login shell
pnpm --filter @pharmaos/web dev   # http://localhost:3000 → RTL login
```

### Walking-skeleton hardware test (M12)

Scan → FEFO sale → ESC/POS receipt (+ cash-drawer pulse), fully offline:

```bash
python3.12 -m pharmaos_api.cli bootstrap-branch --name "الفرع الرئيسي"
python3.12 -m pharmaos_api.cli skeleton-demo-data
# with a network thermal printer (port 9100):
python3.12 -m pharmaos_api.cli skeleton-sale --barcode 6224000000017 --qty 2 --print-host <PRINTER_IP>
# without a printer (writes raw ESC/POS bytes for inspection):
python3.12 -m pharmaos_api.cli skeleton-sale --barcode 6224000000017 --qty 2
```

### Verification gates (same as CI)

```bash
DATABASE_URL=... packages/db/scripts/verify-up-down.sh     # migrations up/down/re-up
cd apps/api && python3.12 -m pytest tests -q               # 31 tests (needs TEST_DATABASE_URL)
python3.12 -m ruff check src tests && python3.12 -m black --check src tests && python3.12 -m mypy src
```

Root scripts: `pnpm build`, `pnpm lint`, `pnpm typecheck`, `pnpm test`, `pnpm format`.

## Status

**Phase 0 (Foundations) complete** — 12/12 milestones including the walking skeleton.
**Phase 1 (Core MVP) code-complete** — M1..M11: auth/RBAC/users, branch settings,
medication catalog (Arabic FTS + GS1), 25k CC0 seed, inventory (FEFO batches +
derived cache + drift self-heal + expiry sweep), full POS (unit switching,
mouse-free flow), ESC/POS receipt printing + cash drawer, and cash sessions with
the end-of-day Z-report.

**Phase 2 (Business + Egyptian compliance) code-complete** — M1..M12: full
supplier management and purchase orders; 2D pack serials (captured on receive,
batch-matched at dispense); deep batch tracking (expiry alerts, auto-quarantine,
reports); customers and loyalty (earn and redeem); VAT per tax profile; returns
as credit notes (quarantine-by-default returned stock) with a payments ledger;
prescriptions and an append-only controlled-substance register; expenses
(reconciled with the cashier Z-report); and the ETA e-receipt and EDA
track-and-trace outbox modules (Port/Adapter with a local simulator; real
acceptance **pending credentials**). See **`docs/phase2-acceptance.md`** for the
acceptance-criteria harvest.

Remaining before pilot sign-off: the on-device hardware pass —
follow **`docs/pilot-checklist.md`** on the pharmacy machine.
See `docs/versions.md` for the pinned version matrix and upgrade log.
