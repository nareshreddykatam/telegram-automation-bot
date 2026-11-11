"""Conservative pacing for Telegram API writes."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable


class RateLimiter:
    def __init__(
        self,
        delay_seconds: float,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.delay_seconds = max(0.0, delay_seconds)
        self._sleep = sleep
        self._clock = clock
        self._last_call = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        if self.delay_seconds <= 0:
            return
        async with self._lock:
            now = self._clock()
            remaining = self._last_call + self.delay_seconds - now
            if remaining > 0:
                await self._sleep(remaining)
            self._last_call = self._clock()
