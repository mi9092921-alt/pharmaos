"""In-process maintenance state used by the HTTP gate.

The named/file maintenance lock protects the device across processes. This
companion state closes the gap inside the API process itself: once a long
restore starts, new HTTP requests are rejected instead of racing the cluster
swap.
"""

import threading
from collections.abc import Iterator
from contextlib import contextmanager

_lock = threading.Lock()
_active = False
_operation = ""


def is_active() -> bool:
    with _lock:
        return _active


def operation_name() -> str:
    with _lock:
        return _operation


@contextmanager
def maintenance_operation(name: str) -> Iterator[None]:
    """Mark the API as unavailable to concurrent requests for one operation."""

    global _active, _operation
    with _lock:
        if _active:
            raise RuntimeError("another API maintenance operation is already running")
        _active = True
        _operation = name
    try:
        yield
    finally:
        with _lock:
            _active = False
            _operation = ""
