"""Device-wide maintenance mutex (installer decision 13)."""

import pytest

from pharmaos_api.maintenance import MaintenanceBusyError, maintenance_lock


def _try_acquire() -> None:
    with maintenance_lock():
        pass  # pragma: no cover - must not be reached


def test_lock_is_exclusive() -> None:
    with (
        maintenance_lock(),
        pytest.raises(MaintenanceBusyError, match="maintenance"),
    ):
        _try_acquire()


def test_lock_reacquirable_after_release() -> None:
    with maintenance_lock():
        pass
    # Released — the next holder (e.g. the 02:00 scheduled backup) acquires.
    with maintenance_lock():
        pass


def test_busy_lock_does_not_block_the_caller() -> None:
    """Acquisition is fail-fast: a long restore must not queue other commands."""
    import threading

    acquired = threading.Event()
    release = threading.Event()

    def _holder() -> None:
        with maintenance_lock():
            acquired.set()
            release.wait(timeout=10)

    thread = threading.Thread(target=_holder)
    thread.start()
    try:
        assert acquired.wait(timeout=10)
        with pytest.raises(MaintenanceBusyError), maintenance_lock():
            pass  # pragma: no cover
    finally:
        release.set()
        thread.join(timeout=10)
