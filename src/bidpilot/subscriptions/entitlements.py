"""What a user's plan *actually* lets the bot do.

A plan used to be only a marketing table: the numbers were printed in Telegram but
the running bot never looked at them (the scheduler polled with the global YAML
limits, every user had one site, and the per-poll project cap was ignored).
:class:`Entitlements` is the single, typed answer to "what may this chat do right
now?" and every enforcement point (rate limiter, application cycle, site picker,
submit queue) reads it instead of re-deriving it from a plan.

It is a plain value object so it can be unit-tested without a database; the async
helper :func:`resolve_entitlements` is the only part that touches storage.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from .plans import Plan, get_plan

log = logging.getLogger(__name__)

#: ``max_bids_per_month`` at or above this value means "no monthly cap".
UNLIMITED = 999_999

#: higher = more valuable plan (used for upgrade/downgrade wording and queue priority)
PLAN_RANK: dict[str, int] = {"free": 0, "starter": 1, "pro": 2, "elite": 3}


def plan_rank(plan_id: str) -> int:
    return PLAN_RANK.get(str(plan_id or "free").lower(), 0)


@dataclass(frozen=True)
class Entitlements:
    plan_id: str = "free"
    plan_title: str = "رایگان"
    plan_badge: str = "🆓"
    per_hour: int = 5
    per_day: int = 20
    per_month: int = 100
    min_gap_seconds: int = 180
    max_sites: int = 1
    max_projects_per_poll: int = 25
    #: submit-queue priority: 0 is served first (elite) … 3 last (free)
    priority: int = 3
    expires_at: datetime | None = None
    #: the plan could not be read (no DB yet, broken table): limits are the safe free-tier ones
    degraded: bool = False
    features: tuple[str, ...] = field(default_factory=tuple)

    # ------------------------------------------------------------------ views
    @property
    def rank(self) -> int:
        return plan_rank(self.plan_id)

    @property
    def is_free(self) -> bool:
        return self.plan_id == "free"

    @property
    def monthly_unlimited(self) -> bool:
        return self.per_month >= UNLIMITED

    @property
    def signature(self) -> tuple:
        """Changes whenever something the running bot must react to changes."""
        return (
            self.plan_id,
            self.per_hour,
            self.per_day,
            self.per_month,
            self.min_gap_seconds,
            self.max_sites,
            self.max_projects_per_poll,
        )

    # ------------------------------------------------------------------ sites
    def allowed_sites(self, selected: list[str]) -> tuple[list[str], list[str]]:
        """Split the user's selection into (kept, paused) by the plan's site cap.

        Order is the user's priority order: the first sites win, the rest wait
        until the plan grows (they are paused, never deleted).
        """
        cap = max(1, int(self.max_sites))
        unique: list[str] = []
        for name in selected:
            if name and name not in unique:
                unique.append(name)
        return unique[:cap], unique[cap:]


def entitlements_for_plan(plan: Plan, *, expires_at: datetime | None = None, degraded: bool = False) -> Entitlements:
    limits = plan.limits
    # priority 0 (best) for the most expensive plan, 3 for free
    priority = max(0, 3 - plan_rank(plan.id))
    return Entitlements(
        plan_id=plan.id,
        plan_title=plan.title,
        plan_badge=plan.badge or "",
        per_hour=int(limits.max_bids_per_hour),
        per_day=int(limits.max_bids_per_day),
        per_month=int(limits.max_bids_per_month),
        min_gap_seconds=int(limits.min_seconds_between_bids),
        max_sites=max(1, int(limits.max_active_sites)),
        max_projects_per_poll=max(1, int(limits.max_projects_per_poll)),
        priority=priority,
        expires_at=expires_at,
        degraded=degraded,
        features=tuple(plan.features),
    )


def free_entitlements(*, degraded: bool = False) -> Entitlements:
    return entitlements_for_plan(get_plan("free"), degraded=degraded)


async def entitlements_from_session(session, chat_id: str | int) -> Entitlements:
    """Resolve a chat's entitlements through an open DB session (expiry handled)."""
    from .manager import SubscriptionManager

    manager = SubscriptionManager(session)
    user, plan = await manager.effective_plan(chat_id)
    return entitlements_for_plan(plan, expires_at=getattr(user, "plan_expires_at", None))


async def resolve_entitlements(database_url: str, chat_id: str | int) -> Entitlements:
    """Open a short-lived connection and resolve the chat's entitlements.

    Never raises: when the account table cannot be read the user gets the
    free-tier limits and ``degraded=True`` (callers log it; nothing crashes).
    """
    from ..db import Database

    db = Database(database_url)
    try:
        await db.init()
        async with db.session_factory() as session:
            found = await entitlements_from_session(session, chat_id)
            await session.commit()
            return found
    except Exception as exc:  # noqa: BLE001 - plan lookup must never stop the bot
        log.warning("could not read plan for chat %s (%s); using free limits", chat_id, type(exc).__name__)
        return free_entitlements(degraded=True)
    finally:
        try:
            await db.dispose()
        except Exception:  # pragma: no cover
            pass


def describe_change(old: Entitlements, new: Entitlements) -> str | None:
    """A clear Persian sentence for a plan change, or ``None`` when nothing changed."""
    if old.signature == new.signature:
        return None
    sites = f"{new.max_sites} سایت همزمان"
    month = "ماهانه نامحدود" if new.monthly_unlimited else f"{new.per_month} در ماه"
    caps = f"{new.per_hour} پیشنهاد در ساعت · {new.per_day} در روز · {month} · {sites}"
    if new.rank > old.rank:
        return f"🎉 <b>پلن شما به «{new.plan_title}» ارتقا یافت.</b>\nظرفیت جدید: {caps}.\nاز همین دور بعدی اعمال می‌شود."
    if new.rank < old.rank:
        return (
            f"⏳ <b>اشتراک «{old.plan_title}» به پایان رسید</b> و حساب به «{new.plan_title}» برگشت.\n"
            f"ظرفیت فعلی: {caps}.\n"
            "برای برگشت به ظرفیت قبلی: /plans — یا با دعوت دوستان رایگان ارتقا بگیر: /invite"
        )
    return f"ℹ️ ظرفیت پلن «{new.plan_title}» تغییر کرد: {caps}."


__all__ = [
    "Entitlements",
    "PLAN_RANK",
    "UNLIMITED",
    "describe_change",
    "entitlements_for_plan",
    "entitlements_from_session",
    "free_entitlements",
    "plan_rank",
    "resolve_entitlements",
]
