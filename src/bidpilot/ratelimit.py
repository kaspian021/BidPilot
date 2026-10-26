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
    """Per-user rate limiter backed by subscription plan.

    Free = 5/hour, 25/day. Paid plans raise the caps — stored in
    subscriptions.plans and resolved via SubscriptionManager at runtime.
    If no user context is known, falls back to global RuntimeConfig.
    """

    def __init__(
        self,
        config: RuntimeConfig,
        repo: Repository,
        *,
        user_chat_id: str | None = None,
        subscription_manager=None,
    ) -> None:
        self.config = config
        self.repo = repo
        self.user_chat_id = str(user_chat_id) if user_chat_id is not None else None
        self.subscription_manager = subscription_manager
        self._plan_cache: tuple[object, ...] | None = None  # (plan, expires)
        self._last_bid_monotonic: float | None = None
        self._lock = asyncio.Lock()

    async def _effective_limits(self) -> tuple[int, int, int, int]:
        """(per_hour, per_day, per_month, min_gap) respecting subscription."""
        # default from YAML
        ph, pd, pm, gap = (
            self.config.max_bids_per_hour,
            self.config.max_bids_per_day,
            999_999,
            self.config.min_seconds_between_bids,
        )
        # try subscription plan if available
        if self.subscription_manager and self.user_chat_id:
            try:
                _, plan = await self.subscription_manager.effective_plan(self.user_chat_id)
                ph = plan.limits.max_bids_per_hour
                pd = plan.limits.max_bids_per_day
                pm = plan.limits.max_bids_per_month
                gap = plan.limits.min_seconds_between_bids
            except Exception:
                pass
        return ph, pd, pm, gap

    async def check(self) -> RateDecision:
        async with self._lock:
            ph, pd, pm, gap = await self._effective_limits()
            # per-hour / per-day / per-month are now per-user when chat is known
            per_hour = await self.repo.bids_per_hour(user_chat_id=self.user_chat_id)
            if per_hour >= ph:
                return RateDecision(False, f"سقف ساعتی ({ph}) پر شده است — پلن فعلی: {ph}/ساعت")

            per_day = await self.repo.bids_per_day(user_chat_id=self.user_chat_id)
            if per_day >= pd:
                return RateDecision(False, f"سقف روزانه ({pd}) پر شده است — با ارتقای پلن بیشتر ارسال کن")

            if pm < 999_999:
                per_month = await self.repo.bids_per_month(user_chat_id=self.user_chat_id)
                if per_month >= pm:
                    return RateDecision(False, f"سقف ماهانه ({pm}) پر شده است — پلن سازمانی نامحدود است")

            if gap > 0:
                last = await self.repo.last_bid_at(user_chat_id=self.user_chat_id)
                if last is not None:
                    elapsed = time.time() - last.timestamp()
                    if elapsed < gap:
                        wait = int(gap - elapsed)
                        return RateDecision(False, f"فاصله حداقلی بین درخواست‌ها؛ {wait} ثانیه صبر کنید.")
            return RateDecision(True)

    def for_user(self, chat_id: str | int) -> "RateLimiter":
        """Create a sibling limiter bound to a specific Telegram user."""
        return RateLimiter(
            self.config,
            self.repo,
            user_chat_id=str(chat_id),
            subscription_manager=self.subscription_manager,
        )

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
