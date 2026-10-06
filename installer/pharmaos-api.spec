# -*- mode: python ; coding: utf-8 -*-
# installer/pharmaos-api.spec — PyInstaller ONEDIR bundle of the FastAPI API
# (installer M2). The device has no Python; this folder IS the runtime.
#
# Deliberate exclusions (decision 2): celery/redis are cloud-worker only and
# MUST NOT enter the desktop bundle; pytest/ruff/black/mypy are dev tools.
# uvloop is Unix-only. onedir (not onefile): faster startup, fewer
# antivirus/Defender false positives (plan risk note).

import os
from pathlib import Path

REPO_ROOT = Path(SPECPATH).resolve().parent

block_cipher = None

a = Analysis(
    ["_pyinstaller-entry.py"],
    pathex=[str(REPO_ROOT / "apps" / "api" / "src")],
    binaries=[],
    datas=[
        # SQL migrations + RBAC seed ride inside the bundle; the API resolves
        # them via PHARMAOS_MIGRATIONS_DIR / PHARMAOS_SEEDS_DIR set by the
        # launcher (M4) or PHARMAOS_DATA_DIR-relative defaults.
        (
            str(REPO_ROOT / "supabase" / "migrations"),
            "pharmaos-data" + os.sep + "migrations",
        ),
        (
            str(REPO_ROOT / "packages" / "db" / "seeds"),
            "pharmaos-data" + os.sep + "seeds",
        ),
    ],
    hiddenimports=[
        # uvicorn's programmatic surface (uvicorn.run with an import string)
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.asyncio",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        # pydantic v2 compiled core + keyring Windows backend
        "keyring.backends.Windows",
        "keyring.backends.kwallet",
        "keyring.backends.macOS",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "celery",
        "kombu",
        "billiard",
        "vine",
        "amqp",
        "redis",
        "pytest",
        "ruff",
        "black",
        "mypy",
        "uvloop",
        "tkinter",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="pharmaos-api",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="pharmaos-api",
)
