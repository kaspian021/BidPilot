"""A mutex that serves the most valuable waiter first.

Browser submissions share one Chromium at a time. With many users that makes a queue,
and "priority in the submit queue" (a paid-plan feature) is simply this lock: whoever
waits holds a *priority* (0 = elite … 3 = free). Equal priorities are served in
arrival order, and a waiter slowly *ages* — every ``aging_seconds`` of waiting is
worth one priority level — so a free user is delayed by paying users but never starves.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar

#: priority of the bid being submitted by the current task (set by Application.cycle)
submit_priority: ContextVar[int] = ContextVar("submit_priority", default=3)


class PriorityLock:
    def __init__(self, *, aging_seconds: float = 90.0) -> None:
        self._locked = False
        self._waiters: list[tuple[int, int, float, asyncio.Future]] = []
        self._seq = itertools.count()
        self._aging = max(1.0, float(aging_seconds))

    @property
    def locked(self) -> bool:
        return self._locked

    @property
    def waiting(self) -> int:
        return len(self._waiters)

    async def acquire(self, priority: int = 3) -> None:
        if not self._locked and not self._waiters:
            self._locked = True
            return
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        entry = (int(priority), next(self._seq), time.monotonic(), future)
        self._waiters.append(entry)
        try:
            await future
        except BaseException:
            # cancelled while waiting: leave the queue; if the lock was already
            # handed to us, pass it on so nobody deadlocks
            if entry in self._waiters:
                self._waiters.remove(entry)
            elif future.done() and not future.cancelled():
                self.release()
            raise

    def release(self) -> None:
        if not self._locked:
            raise RuntimeError("release of an unlocked PriorityLock")
        now = time.monotonic()
        live = [w for w in self._waiters if not w[3].done()]
        self._waiters = live
        if not live:
            self._locked = False
            return
        best = min(live, key=lambda w: (w[0] - (now - w[2]) / self._aging, w[1]))
        self._waiters.remove(best)
        best[3].set_result(None)  # ownership is handed over: stays locked

    @asynccontextmanager
    async def hold(self, priority: int = 3) -> AsyncIterator[None]:
        await self.acquire(priority)
        try:
            yield
        finally:
            self.release()
