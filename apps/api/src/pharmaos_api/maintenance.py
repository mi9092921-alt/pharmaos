"""Device-wide maintenance mutex (installer decision 13).

Exactly ONE maintenance operation at a time on the device: backup create /
backup restore / migrate / catalog-seed / first-run wizard all hold this
lock, so a 02:00 scheduled backup can never collide with a manual restore
or the first-run wizard.

- Windows: named mutex ``Global\\PharmaOS-Maintenance`` — the Global namespace
  spans sessions, so a Task Scheduler task and an interactive PharmaOS fight
  over the same lock.
- POSIX (dev/CI): non-blocking flock on a lock file next to the data dir.

Both platform backends are loaded dynamically (importlib/getattr) so mypy
analyzes this module identically on Windows and Linux CI. Acquiring is
non-blocking: a busy lock raises immediately.
"""

import importlib
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from pharmaos_api.config import default_data_dir

_WINDOWS_MUTEX_NAME = r"Global\PharmaOS-Maintenance"
_WAIT_TIMEOUT = 0x00000102

# Recursive acquisition inside ONE process is always a bug here (a CLI command
# nested inside another), whatever the OS — guard it explicitly, since Windows
# named mutexes are thread-recursive and would silently allow it.
_tls = threading.local()


class MaintenanceBusyError(RuntimeError):
    """Another maintenance operation (backup/restore/migrate/seed) is running."""


@contextmanager
def maintenance_lock() -> Iterator[None]:
    """Acquire the device maintenance lock or raise MaintenanceBusyError."""
    if getattr(_tls, "held", False):
        raise MaintenanceBusyError("recursive maintenance-lock acquisition in the same process")
    if os.name == "nt":
        with _windows_lock():
            yield
    else:
        with _posix_lock():
            yield


@contextmanager
def _windows_lock() -> Iterator[None]:
    import ctypes

    kernel32 = ctypes.windll.kernel32  # Windows-only; opaque to mypy
    handle = kernel32.CreateMutexW(None, False, _WINDOWS_MUTEX_NAME)
    if not handle:
        raise OSError("CreateMutexW failed for the maintenance mutex")
    # 0 ms wait: a busy lock must fail fast, not queue behind a long restore.
    result = kernel32.WaitForSingleObject(handle, 0)
    if result == _WAIT_TIMEOUT:
        kernel32.CloseHandle(handle)
        raise MaintenanceBusyError(
            "another maintenance operation is running on this device "
            "(backup/restore/migrate/seed) — try again after it finishes"
        )
    _tls.held = True
    try:
        # WAIT_OBJECT_0 or WAIT_ABANDONED — both mean we own the mutex now.
        yield
    finally:
        _tls.held = False
        kernel32.ReleaseMutex(handle)
        kernel32.CloseHandle(handle)


@contextmanager
def _posix_lock() -> Iterator[None]:
    fcntl = importlib.import_module("fcntl")  # POSIX-only; opaque to mypy
    lock_path = default_data_dir() / ".pharmaos-maintenance.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MaintenanceBusyError(
                "another maintenance operation is running on this device "
                "(backup/restore/migrate/seed) — try again after it finishes"
            ) from exc
        try:
            _tls.held = True
            try:
                yield
            finally:
                _tls.held = False
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        os.close(handle)
