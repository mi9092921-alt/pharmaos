"""Run a coroutine to completion from synchronous code.

``asyncio.run`` raises when the calling thread already has a running event
loop — which happens whenever sync service functions are exercised from
async contexts (the FastAPI app, the pytest-asyncio session loop). In that
case we execute the coroutine on a worker thread with its own loop; the
calling thread blocks until it finishes, preserving sync semantics.
"""

import asyncio
import concurrent.futures
from collections.abc import Coroutine
from typing import Any


def run_coro_sync[T](coro: Coroutine[Any, Any, T]) -> T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()
