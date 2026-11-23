"""Business plan -> technical enforcement (Pro: 30/hour, 3 sites, 60 s gap …)."""

import asyncio
from datetime import datetime, timedelta, timezone

from bidpilot.config import RuntimeConfig
from bidpilot.ratelimit import RateLimiter, UsageGate
from bidpilot.subscriptions.entitlements import entitlements_for_plan
from bidpilot.subscriptions.plans import get_plan


class _Repo:
    def __init__(self, *, hour=0, day=0, month=0, last=None):
        self._counts = (hour, day, month)
        self._last = last

    async def bids_per_hour(self, user_chat_id=None):
        return self._counts[0]

    async def bids_per_day(self, user_chat_id=None):
        return self._counts[1]

    async def bids_per_month(self, user_chat_id=None):
        return self._counts[2]

    async def oldest_bid_since(self, since, user_chat_id=None):
        return datetime.now(timezone.utc)

    async def last_bid_at(self, user_chat_id=None, source=None):
        return self._last


class _Manager:
    def __init__(self, plan_id):
        self._plan = get_plan(plan_id)

    async def effective_plan(self, chat_id):
        return None, self._plan


def limiter(plan_id: str, repo: _Repo) -> RateLimiter:
    return RateLimiter(
        RuntimeConfig(),
        repo,  # type: ignore[arg-type]
        user_chat_id="42",
        subscription_manager=_Manager(plan_id),
        gate=UsageGate(),
    )


def test_plan_table_matches_the_business_offer():
    pro = get_plan("pro")
    assert (pro.limits.max_bids_per_hour, pro.limits.max_bids_per_day) == (30, 200)
    assert pro.limits.max_active_sites == 3
    assert pro.limits.min_seconds_between_bids == 60
    ent = entitlements_for_plan(pro)
    assert (ent.per_hour, ent.max_sites, ent.max_projects_per_poll) == (30, 3, 60)


async def test_pro_hourly_cap_is_30_not_the_yaml_default():
    assert (await limiter("pro", _Repo(hour=29)).check("parscoders")).allowed
    decision = await limiter("pro", _Repo(hour=30)).check("parscoders")
    assert not decision.allowed and decision.code == "hour" and decision.limit == 30


async def test_free_hourly_cap_is_5():
    decision = await limiter("free", _Repo(hour=5)).check("parscoders")
    assert not decision.allowed and decision.limit == 5


async def test_gap_is_per_user_across_all_sites():
    """A bid on Ponisha 10 s ago must block Karlancer too (Pro gap = 60 s)."""
    repo = _Repo(last=datetime.now(timezone.utc) - timedelta(seconds=10))
    decision = await limiter("pro", repo).check("karlancer")
    assert not decision.allowed and decision.code == "gap"


async def test_simultaneous_sites_share_the_gap_and_the_caps():
    gate = UsageGate()
    repo = _Repo()
    first = RateLimiter(RuntimeConfig(), repo, user_chat_id="7", subscription_manager=_Manager("pro"), gate=gate)  # type: ignore[arg-type]
    second = RateLimiter(RuntimeConfig(), repo, user_chat_id="7", subscription_manager=_Manager("pro"), gate=gate)  # type: ignore[arg-type]

    async with first.slot("parscoders") as decision:
        assert decision.allowed
        other = await second.check("ponisha")
        assert not other.allowed and other.code == "gap"

    await asyncio.sleep(0)  # slot released: the next bid may proceed (no last bid stored)
    assert (await second.check("ponisha")).allowed
