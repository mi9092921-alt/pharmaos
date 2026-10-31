# Findings — License ACL failure + SPA gate gap (2026-10-30, partial round on dev machine)

## Finding A (blocker): fresh per-machine install → license external store unwritable → all business APIs 503

**Sequence (all evidence in this round's folders):**

1. Installer (NSIS `perMachine: true`, elevated) creates `C:\ProgramData\PharmaOS` with ACL
   `BUILTIN\Users:(RX)` / `Administrators:(F)` / `SYSTEM:(F)` — standard users get READ ONLY.
   (verified: `icacls C:\ProgramData\PharmaOS` output in this file, below)
2. Installer then launches the app **as the normal user** (correct practice), so the whole app
   tree (`pharmaos-api.exe` included) runs non-elevated.
3. First-run setup + license activation (09:33) succeed — license state persisted to PostgreSQL.
4. The licensing state machine (`apps/api/src/pharmaos_api/licensing/state.py` line ~136:
   "authenticate externals → reconcile → decide → persist") tries to persist the LOCK-2
   clock-MAC envelope to the external store `WindowsFileStore` =
   `%PROGRAMDATA%\PharmaOS\license-store.json` (`external_stores.py:95-112`).
5. `write_raw` → `self._path.write_bytes(data)` →
   `PermissionError: [Errno 13] Permission denied: 'C:\\ProgramData\\PharmaOS\\license-store.json'`
   (api.log traceback captured) → evaluation returns `structure_error` → `decide_status()` →
   status `"error"` (`state.py:94`).
6. Middleware gate: `LICENSE_STATE_ERROR: 503` (`errors.py:17`, `middleware.py:133/143/180`) —
   EVERY business endpoint (medications, customers, reports, notifications, /auth/me…) returns
   503 while `/license/status` alone returns 200 with `"status":"error"`.

**Consequence:** a fresh install on a standard-user pharmacy machine self-bricks within minutes
of successful activation. The launcher's ACL step fixes `pgdata` only
(`boot: PostgreSQL data ACL ready for …\mosta` in pharmaos.log) — it does not cover
`license-store.json` (nor, presumably, `backups/` writes — same RX inheritance; to be verified
in Step 2).

**Why the 2026-10-20 round did not hit it:** that install's `C:\ProgramData\PharmaOS` tree had
existed since an earlier run (first created 2026-10-20 19:02 per pre-round inventory), likely by
a non-elevated process, so the creator had write access. A truly clean machine reproduces it.

**Environment workaround applied to continue this round (NOT a product fix):**
`icacls "C:\ProgramData\PharmaOS" /grant "BUILTIN\Users:(OI)(CI)(M)"`
Proper product-side fix belongs to the release owner (installer/launcher must set data-dir ACLs
for standard users, as it already does for pgdata).

## Finding B (operator report): SPA does not hard-gate pages when license degrades mid-session

- `apps/web/src/app/(app)/layout.tsx:69-73`: the full-screen `LicenseRequiredScreen` renders only
  when the **initial `/auth/me` query fails with an `E-LIC-*` error code** (cold boot).
- When the license degrades active → error **while a session is already loaded**, react-query
  keeps the cached `/me` success data, `isError` stays false, and the layout keeps rendering
  normal pages (`{children}`); only individual API calls fail (503) with a transient toast
  ("حالة الترخيص غير متاحة مؤقتاً — جارٍ إعادة المحاولة").
- Observed live: catalog page rendered fully with the add-medication form while every business
  call was already 503. A fresh reload (cold cache) DOES show the license screen — hence the
  operator seeing both behaviors.
- Expected per operator + per the layout's own comment ("unlicensed / tamper / key_lost /
  clock_error / error … instead of hanging"): ANY page should show the license screen in those
  states. Product fix: gate on the polled `license-status` query, not only the initial `/me`
  error code.

## Raw evidence appended below

## ACL evidence (captured 2026-10-30T10:14:45+02:00)

### icacls C:\ProgramData\PharmaOS

C:\ProgramData\PharmaOS BUILTIN\Users:(RX)
BUILTIN\Administrators:(F)
NT AUTHORITY\SYSTEM:(F)

Successfully processed 1 files; Failed processing 0 files

### license-store.json existence

ls: cannot access '/c/ProgramData/PharmaOS/license-store.json': No such file or directory

### non-elevated write test (same user as the app)

touch: cannot touch '/c/ProgramData/PharmaOS/write-test.txt': Permission denied

### api.log PermissionError traceback

### license/status at failure time

{"success":true,"data":{"status":"error","hwid":"PHAR-RTTJ-AR6R-ZTPL-35ID","valid_until":null,"days_left":null,"needs_activation":true}}
