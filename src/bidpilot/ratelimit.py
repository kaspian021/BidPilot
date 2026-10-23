"""Rate limiting / safety valve.

Automated bidding must behave like a careful human: a minimum gap between bids and
hard caps per hour and per day. Caps are enforced from the database so they survive
restarts and work with multiple processes.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from .config import RuntimeConfig
from .repositories import Repository


@dataclass
class RateDecision:
    allowed: bool
    reason: str = ""


class RateLimiter:
    def __init__(self, config: RuntimeConfig, repo: Repository) -> None:
        self.config = config
        self.repo = repo
        self._last_bid_monotonic: float | None = None
        self._lock = asyncio.Lock()

    async def check(self) -> RateDecision:
        async with self._lock:
            per_hour = await self.repo.bids_per_hour()
            if per_hour >= self.config.max_bids_per_hour:
                return RateDecision(False, f"سقف ساعتی ({self.config.max_bids_per_hour}) پر شده است.")

            per_day = await self.repo.bids_per_day()
            if per_day >= self.config.max_bids_per_day:
                return RateDecision(False, f"سقف روزانه ({self.config.max_bids_per_day}) پر شده است.")

            min_gap = self.config.min_seconds_between_bids
            if min_gap > 0:
                last = await self.repo.last_bid_at()
                if last is not None:
                    elapsed = time.time() - last.timestamp()
                    if elapsed < min_gap:
                        wait = int(min_gap - elapsed)
                        return RateDecision(False, f"فاصله حداقلی بین درخواست‌ها؛ {wait} ثانیه صبر کنید.")
            return RateDecision(True)

    async def acquire(self, seconds: float) -> None:
        """Optional cooperative pacing hook used by the scheduler."""
        if seconds > 0:
            await asyncio.sleep(seconds)

    async def wait_until_allowed(self, timeout: float = 600) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if (await self.check()).allowed:
                return True
            await asyncio.sleep(15)
        return False
