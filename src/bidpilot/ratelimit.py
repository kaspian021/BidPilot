"""Rate limiting / safety valve.

Automated bidding must behave like a careful human: a minimum gap between bids and
hard caps per hour, day and month. Caps come from the user's *plan* (see
:mod:`bidpilot.subscriptions.entitlements`) and are enforced from the database so
they survive restarts and work with several processes.

Several marketplaces can now run **at the same time** for one user, each with its
own database session. A bid that was checked but not yet written would be invisible
to the other sessions, so two sites could both take the last free slot. Every
limiter of one user therefore shares a :class:`UsageGate`: a slot is *reserved*
under a lock (counting the reservations already in flight) and released once the
bid has been stored.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .config import RuntimeConfig
from .repositories import Repository

#: reason codes whose block lasts long enough that polling the site is pointless
HARD_CODES = frozenset({"hour", "day", "month"})


def format_wait(seconds: float) -> str:
    """«۱۲ دقیقه» / «۲ ساعت و ۵ دقیقه» / «۴۰ ثانیه» — short, human, Persian."""
    seconds = max(1, int(seconds))
    if seconds < 90:
        return f"{seconds} ثانیه"
    minutes = (seconds + 59) // 60
    if minutes < 60:
        return f"{minutes} دقیقه"
    hours, rest = divmod(minutes, 60)
    return f"{hours} ساعت" + (f" و {rest} دقیقه" if rest else "")


@dataclass
class RateDecision:
    allowed: bool
    reason: str = ""
    #: ``hour`` | ``day`` | ``month`` | ``gap`` | "" (allowed)
    code: str = ""
    #: seconds until this block clears (best estimate, 0 = unknown)
    retry_after: int = 0
    limit: int = 0

    @property
    def hard(self) -> bool:
        """A cap (not the short safety gap) — nothing can be sent until it clears."""
        return not self.allowed and self.code in HARD_CODES


@dataclass
class Limits:
    per_hour: int
    per_day: int
    per_month: int
    gap: int
    plan_title: str = ""
    plan_id: str = ""
    from_plan: bool = False


@dataclass
class UsageGate:
    """Shared by all limiters of one user (one process, one event loop)."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    inflight: int = 0
    inflight_by_source: dict[str, int] = field(default_factory=dict)


_GATES: dict[tuple[str, int], UsageGate] = {}


def gate_for(user_chat_id: str | None) -> UsageGate:
    """One gate per (user, event loop) so tests with fresh loops never share locks."""
    try:
        loop_id = id(asyncio.get_running_loop())
    except RuntimeError:  # pragma: no cover - sync caller
        loop_id = 0
    key = (str(user_chat_id or ""), loop_id)
    gate = _GATES.get(key)
    if gate is None:
        if len(_GATES) > 512:  # long-lived daemons: drop gates of dead loops / idle users
            for stale in [k for k, g in _GATES.items() if g.inflight == 0][:256]:
                _GATES.pop(stale, None)
        gate = _GATES[key] = UsageGate()
    return gate


class RateLimiter:
    """Per-user rate limiter backed by subscription plan.

    Free = 5/hour, 20/day. Paid plans raise the caps — resolved through the
    subscription manager at runtime. If no user context is known, falls back to the
    global :class:`RuntimeConfig` (CLI / single-user installs).
    """

    def __init__(
        self,
        config: RuntimeConfig,
        repo: Repository,
        *,
        user_chat_id: str | None = None,
        subscription_manager=None,
        gate: UsageGate | None = None,
    ) -> None:
        self.config = config
        self.repo = repo
        self.user_chat_id = str(user_chat_id) if user_chat_id is not None else None
        self.subscription_manager = subscription_manager
        self.gate = gate or gate_for(self.user_chat_id)
        self._last_bid_monotonic: float | None = None
        self._lock = asyncio.Lock()

    # ----------------------------------------------------------------- limits
    async def limits(self) -> Limits:
        """Plan limits for this user (YAML runtime limits when no plan is known)."""
        found = Limits(
            self.config.max_bids_per_hour,
            self.config.max_bids_per_day,
            999_999,
            self.config.min_seconds_between_bids,
        )
        if self.subscription_manager and self.user_chat_id:
            try:
                _, plan = await self.subscription_manager.effective_plan(self.user_chat_id)
                found = Limits(
                    plan.limits.max_bids_per_hour,
                    plan.limits.max_bids_per_day,
                    plan.limits.max_bids_per_month,
                    plan.limits.min_seconds_between_bids,
                    plan_title=plan.title,
                    plan_id=plan.id,
                    from_plan=True,
                )
            except Exception:  # noqa: BLE001 - a broken accounts table must not stop bidding
                pass
        return found

    async def _effective_limits(self) -> tuple[int, int, int, int]:
        """(per_hour, per_day, per_month, min_gap) — kept for older callers."""
        limits = await self.limits()
        return limits.per_hour, limits.per_day, limits.per_month, limits.gap

    # ------------------------------------------------------------------ check
    async def _decide(self, source: str | None, *, extra: int, extra_source: int) -> RateDecision:
        limits = await self.limits()
        who = f"پلن «{limits.plan_title}»" if limits.plan_title else "تنظیمات فعلی"
        now = datetime.now(timezone.utc)
        user = self.user_chat_id

        per_hour = await self.repo.bids_per_hour(user_chat_id=user) + extra
        if per_hour >= limits.per_hour:
            oldest = await self.repo.oldest_bid_since(now - timedelta(hours=1), user_chat_id=user)
            wait = int((oldest + timedelta(hours=1) - now).total_seconds()) if oldest else 600
            return RateDecision(
                False,
                f"سقف ساعتی {who} ({limits.per_hour} پیشنهاد) پر شده؛ حدود {format_wait(wait)} دیگر ظرفیت آزاد می‌شود.",
                "hour",
                max(1, wait),
                limits.per_hour,
            )

        per_day = await self.repo.bids_per_day(user_chat_id=user) + extra
        if per_day >= limits.per_day:
            oldest = await self.repo.oldest_bid_since(now - timedelta(days=1), user_chat_id=user)
            wait = int((oldest + timedelta(days=1) - now).total_seconds()) if oldest else 3600
            return RateDecision(
                False,
                f"سقف روزانهٔ {who} ({limits.per_day} پیشنهاد) پر شده؛ حدود {format_wait(wait)} دیگر ظرفیت آزاد می‌شود. "
                "برای ظرفیت بیشتر: /plans",
                "day",
                max(1, wait),
                limits.per_day,
            )

        if limits.per_month < 999_999:
            per_month = await self.repo.bids_per_month(user_chat_id=user) + extra
            if per_month >= limits.per_month:
                oldest = await self.repo.oldest_bid_since(now - timedelta(days=30), user_chat_id=user)
                wait = int((oldest + timedelta(days=30) - now).total_seconds()) if oldest else 86_400
                return RateDecision(
                    False,
                    f"سقف ماهانهٔ {who} ({limits.per_month} پیشنهاد) پر شده؛ حدود {format_wait(wait)} دیگر ظرفیت آزاد می‌شود. "
                    "پلن سازمانی ماهانه نامحدود است: /plans",
                    "month",
                    max(1, wait),
                    limits.per_month,
                )

        if limits.gap > 0:
            if extra_source:
                return RateDecision(
                    False,
                    f"پیشنهاد دیگری روی همین سایت در حال ارسال است؛ فاصلهٔ ایمن {limits.gap} ثانیه رعایت می‌شود.",
                    "gap",
                    limits.gap,
                    limits.gap,
                )
            last = await self.repo.last_bid_at(user_chat_id=user, source=source)
            if last is not None:
                elapsed = time.time() - last.timestamp()
                if elapsed < limits.gap:
                    wait = int(limits.gap - elapsed) + 1
                    return RateDecision(
                        False,
                        f"فاصلهٔ ایمن بین پیشنهادها ({limits.gap} ثانیه) هنوز تمام نشده؛ {format_wait(wait)} دیگر.",
                        "gap",
                        wait,
                        limits.gap,
                    )
        return RateDecision(True)

    async def check(self, source: str | None = None) -> RateDecision:
        """Read-only verdict (counts reservations in flight, reserves nothing)."""
        async with self.gate.lock:
            return await self._decide(
                source,
                extra=self.gate.inflight,
                extra_source=self.gate.inflight_by_source.get(source or "", 0) if source else 0,
            )

    # ------------------------------------------------------------ reservation
    @asynccontextmanager
    async def slot(self, source: str | None = None) -> AsyncIterator[RateDecision]:
        """Reserve one bid slot for the duration of the block.

        The reservation counts against every other site's cap until the bid has
        been stored (or abandoned), which is what makes simultaneous sites safe.
        """
        async with self.gate.lock:
            decision = await self._decide(
                source,
                extra=self.gate.inflight,
                extra_source=self.gate.inflight_by_source.get(source or "", 0) if source else 0,
            )
            if decision.allowed:
                self.gate.inflight += 1
                if source:
                    self.gate.inflight_by_source[source] = self.gate.inflight_by_source.get(source, 0) + 1
        try:
            yield decision
        finally:
            if decision.allowed:
                async with self.gate.lock:
                    self.gate.inflight = max(0, self.gate.inflight - 1)
                    if source:
                        left = max(0, self.gate.inflight_by_source.get(source, 0) - 1)
                        if left:
                            self.gate.inflight_by_source[source] = left
                        else:
                            self.gate.inflight_by_source.pop(source, None)

    def for_user(self, chat_id: str | int) -> RateLimiter:
        """Create a sibling limiter bound to a specific Telegram user."""
        return RateLimiter(
            self.config,
            self.repo,
            user_chat_id=str(chat_id),
            subscription_manager=self.subscription_manager,
        )

    def for_repo(self, repo: Repository, subscription_manager=None) -> RateLimiter:
        """Same user, same gate, another DB session (one session per marketplace)."""
        return RateLimiter(
            self.config,
            repo,
            user_chat_id=self.user_chat_id,
            subscription_manager=subscription_manager or self.subscription_manager,
            gate=self.gate,
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
