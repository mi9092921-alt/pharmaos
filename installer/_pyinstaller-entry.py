"""PyInstaller entry point for the device API bundle (installer M2).

Dual-mode: no arguments -> start the uvicorn API server (the Electron
orchestrator's child); arguments -> the operational CLI (device-init,
migrate, backup ...). The launcher (M4) sets PHARMAOS_ENV=production and the
data-dir env; the device .env + keystore carry everything else.
"""

import sys

if len(sys.argv) > 1:
    from pharmaos_api.cli import main

    raise SystemExit(main())

from pharmaos_api.main import run

run()
