"""Read-only probes that can be abandoned when the terminal closes.

Textual's thread workers use asyncio's default executor, which waits for its threads at
process exit. A stalled network read must not keep Quit waiting for that executor.
Callers coalesce refreshes to one probe per tier. Never use this for mutating actions.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextvars import copy_context
from threading import Thread
from typing import TypeVar

T = TypeVar("T")


async def read_in_background(read: Callable[[], T]) -> T:
    loop = asyncio.get_running_loop()
    result = loop.create_future()
    context = copy_context()

    def deliver(value: T | None, error: Exception | None) -> None:
        if not result.done():
            if error is not None:
                result.set_exception(error)
            else:
                result.set_result(value)

    def run() -> None:
        try:
            value, error = context.run(read), None
        except Exception as exc:  # noqa: BLE001 - propagate to the UI worker
            value, error = None, exc
        try:
            loop.call_soon_threadsafe(deliver, value, error)
        except RuntimeError:
            pass  # The app has closed; there is no UI left to update.

    Thread(target=run, name="tui-read", daemon=True).start()
    return await result
