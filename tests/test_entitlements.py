"""Plans are enforced, not just printed: entitlements, shared cap gate, sites, queue."""

import asyncio

from bidpilot.credentials import site_credentials
from bidpilot.priority_lock import PriorityLock
from bidpilot.ratelimit import RateLimiter, format_wait
from bidpilot.repositories import Repository
from bidpilot.settings_store import SettingsStore
from bidpilot.subscriptions.entitlements import (
    Entitlements,
    describe_change,
    entitlements_for_plan,
    free_entitlements,
)
from bidpilot.subscriptions.plans import get_plan
from bidpilot.user_sites import plan_sites, toggle_user_site, user_sites
from tests.test_pipeline import build_app, make_config

OWNER, GUEST = "100", "200"


def ent(plan_id: str) -> Entitlements:
    return entitlements_for_plan(get_plan(plan_id))


def make_store(tmp_path) -> SettingsStore:
    store = SettingsStore(tmp_path / "settings.json")
    store.set("telegram.chat_ids", [OWNER, GUEST])
    return store


# ------------------------------------------------------------- entitlements
def test_site_caps_follow_the_plan():
    assert [ent(p).max_sites for p in ("free", "starter", "pro", "elite")] == [1, 2, 3, 3]
    assert ent("elite").priority < ent("free").priority


def test_allowed_sites_keeps_priority_order_and_pauses_the_rest():
    kept, paused = ent("starter").allowed_sites(["ponisha", "karlancer", "parscoders", "ponisha"])
    assert kept == ["ponisha", "karlancer"]
    assert paused == ["parscoders"]
    assert ent("free").allowed_sites([]) == ([], [])


def test_describe_change_upgrade_downgrade_and_noop():
    free, pro = free_entitlements(), ent("pro")
    assert describe_change(free, free) is None
    assert "ارتقا" in describe_change(free, pro)
    assert "به پایان رسید" in describe_change(pro, free)


# --------------------------------------------------------------- rate limit
def test_format_wait_is_human():
    assert format_wait(40) == "40 ثانیه"
    assert format_wait(12 * 60) == "12 دقیقه"
    assert format_wait(125 * 60) == "2 ساعت و 5 دقیقه"


async def test_simultaneous_sites_cannot_overshoot_the_hourly_cap(tmp_path):
    app, _ = await build_app(make_config(tmp_path, max_bids_per_hour=2))
    session = app.db.session()
    limiter = RateLimiter(app.config.runtime, Repository(session), user_chat_id="u-gate")
    allowed = 0

    async def take(source: str) -> None:
        nonlocal allowed
        async with limiter.slot(source) as decision:
            if decision.allowed:
                allowed += 1
                await asyncio.sleep(0.05)  # bid is "in flight", not stored yet

    await asyncio.gather(*(take(s) for s in ["parscoders", "ponisha", "karlancer", "parscoders", "ponisha"]))
    assert allowed == 2
    await session.close()
    await app.close()


async def test_hard_cap_decision_has_wait_and_clear_text(tmp_path):
    app, _ = await build_app(make_config(tmp_path, max_bids_per_hour=1))
    await app.cycle()  # stores bids up to the cap
    session = app.db.session()
    decision = await RateLimiter(app.config.runtime, Repository(session)).check("mock")
    assert decision.hard and decision.code == "hour"
    assert decision.retry_after > 0
    assert "سقف ساعتی" in decision.reason
    await session.close()
    await app.close()


# ------------------------------------------------------------------- sites
def test_toggle_respects_plan_and_offers_switch_for_one_site_plans(tmp_path):
    store = make_store(tmp_path)
    assert toggle_user_site(store, GUEST, "ponisha", ent("free")).action == "switched"
    assert user_sites(store, GUEST) == ["ponisha"]
    assert toggle_user_site(store, GUEST, "ponisha", ent("free")).action == "refused_last"

    assert toggle_user_site(store, GUEST, "karlancer", ent("starter")).action == "added"
    refused = toggle_user_site(store, GUEST, "parscoders", ent("starter"))
    assert (refused.ok, refused.action) == (False, "refused_limit")
    assert toggle_user_site(store, GUEST, "parscoders", ent("pro")).action == "added"
    assert user_sites(store, GUEST) == ["ponisha", "karlancer", "parscoders"]


def test_plan_sites_pauses_sites_above_the_cap_and_needs_accounts(tmp_path):
    store = make_store(tmp_path)
    store.set(f"users.{GUEST}.marketplace.names", ["ponisha", "karlancer", "parscoders"])
    store.set(f"users.{GUEST}.credentials.ponisha.cookie", "sid=abc")
    plan = plan_sites(store, GUEST, ent("starter"))
    assert plan.running == ["ponisha"]
    assert plan.missing_account == ["karlancer"]
    assert plan.paused_by_plan == ["parscoders"]


def test_non_owner_never_borrows_the_owner_login(tmp_path):
    store = make_store(tmp_path)
    store.set("credentials.ponisha.cookie", "owner-cookie")
    assert site_credentials("ponisha", store, chat_id=OWNER).ready
    assert not site_credentials("ponisha", store, chat_id=GUEST).ready
    store.set(f"users.{GUEST}.credentials.ponisha.cookie", "mine")
    assert site_credentials("ponisha", store, chat_id=GUEST).ready


# ------------------------------------------------------------ priority lock
async def test_priority_lock_serves_higher_plans_first_but_ages_waiters():
    lock = PriorityLock(aging_seconds=1000)
    order: list[str] = []
    await lock.acquire(3)  # busy

    async def waiter(name: str, prio: int) -> None:
        async with lock.hold(prio):
            order.append(name)

    tasks = [asyncio.create_task(waiter("free", 3)), asyncio.create_task(waiter("elite", 0))]
    await asyncio.sleep(0.01)
    lock.release()
    await asyncio.gather(*tasks)
    assert order == ["elite", "free"]


async def test_priority_lock_aging_prevents_starvation():
    lock = PriorityLock()
    lock._aging = 0.02  # the public minimum is 1s; shrink it so the test stays fast
    order: list[str] = []
    await lock.acquire(0)

    async def waiter(name: str, prio: int) -> None:
        async with lock.hold(prio):
            order.append(name)

    old = asyncio.create_task(waiter("old-free", 3))
    await asyncio.sleep(0.2)  # waits long enough to outrank a fresh elite
    new = asyncio.create_task(waiter("new-elite", 0))
    await asyncio.sleep(0.01)
    lock.release()
    await asyncio.gather(old, new)
    assert order[0] == "old-free"
