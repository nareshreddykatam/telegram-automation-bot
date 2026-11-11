"""Tracked background tasks for long-running publish commands."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)


class BackgroundTasks:
    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    def spawn(self, coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)

        def _done(completed: asyncio.Task[Any]) -> None:
            self._tasks.discard(completed)
            if completed.cancelled():
                return
            exc = completed.exception()
            if exc is not None:
                logger.error(
                    "Background task %s failed: %s", completed.get_name(), exc, exc_info=exc
                )

        task.add_done_callback(_done)
        return task

    async def shutdown(self, timeout: float = 20.0) -> None:
        if not self._tasks:
            return
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @property
    def active_count(self) -> int:
        return len(self._tasks)
