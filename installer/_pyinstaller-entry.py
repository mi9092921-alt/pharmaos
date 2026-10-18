"""PyInstaller entry point for the device API bundle (installer M2).

Dual-mode: no arguments -> start the uvicorn API server (the Electron
orchestrator's child); arguments -> the operational CLI (device-init,
migrate, backup ...). The launcher (M4) sets PHARMAOS_ENV=production and the
data-dir env; the device .env + keystore carry everything else.
"""

import sys

# Build-time smoke probe (no DB, no keystore, no server): importing
# pharmaos_api.main pulls the FULL router surface, so any missing
# runtime-lazy dependency (e.g. python-multipart, 2026-10-14) fails here
# instead of killing the device boot 90s later. build-api.ps1 runs it.
if len(sys.argv) > 1 and sys.argv[1] == "--check-imports":
    import pharmaos_api.main  # noqa: F401

    print("imports ok")
    raise SystemExit(0)

if len(sys.argv) > 1:
    from pharmaos_api.cli import main

    raise SystemExit(main())

from pharmaos_api.main import run

run()
