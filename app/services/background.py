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

    async def wait(self, timeout: float = 10.0) -> None:
        """Wait for spawned tasks instead of cancelling them.

        Serverless request handlers must let short follow-up work (for example a
        PostgreSQL catch-up enqueue) finish before the function instance is
        frozen. Long publishing work is still bounded by ``timeout`` and is
        cancelled rather than silently abandoning the request.
        """

        if not self._tasks:
            return
        tasks = list(self._tasks)
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        if not pending:
            return
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        logger.warning(
            "Background task timeout (%ss): cancelled %s task(s)", timeout, len(pending)
        )

    @property
    def active_count(self) -> int:
        return len(self._tasks)
