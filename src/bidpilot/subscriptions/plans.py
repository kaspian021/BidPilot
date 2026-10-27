"""Professional subscription tiers for BidPilot.

Designed for Iranian freelancers: tomans, Jalali billing, and limits that map
directly to real bidding behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta


@dataclass(frozen=True)
class PlanLimits:
    max_bids_per_hour: int
    max_bids_per_day: int
    max_bids_per_month: int
    min_seconds_between_bids: int
    max_projects_per_poll: int = 30
    max_active_sites: int = 1
    priority_queue: bool = False


@dataclass(frozen=True)
class Plan:
    id: str  # slug: free, starter, pro, elite
    title: str
    title_en: str
    tagline: str
    price_toman: int  # 0 = free
    period_days: int
    limits: PlanLimits
    features: list[str] = field(default_factory=list)
    badge: str = ""
    color: str = ""  # for UI
    recommended: bool = False
    zarinpal_plan_id: str | None = None  # optional mapping for recurring

    @property
    def formatted_price(self) -> str:
        if self.price_toman == 0:
            return "رایگان"
        if self.price_toman >= 1_000_000:
            return f"{self.price_toman / 1_000_000:g} میلیون تومان"
        return f"{self.price_toman:,} تومان".replace(",", "٬")

    @property
    def price_per_day(self) -> str:
        if self.price_toman == 0:
            return "۰"
        daily = self.price_toman / self.period_days
        if daily >= 100_000:
            return f"{int(daily):,} تومان/روز".replace(",", "٬")
        return f"{int(daily):,} تومان/روز".replace(",", "٬")

    def is_free(self) -> bool:
        return self.price_toman == 0


PLANS: dict[str, Plan] = {
    "free": Plan(
        id="free",
        title="رایگان",
        title_en="Free",
        tagline="برای شروع و تست ربات",
        price_toman=0,
        period_days=30,
        limits=PlanLimits(
            max_bids_per_hour=5,
            max_bids_per_day=20,
            max_bids_per_month=100,
            min_seconds_between_bids=180,
            max_projects_per_poll=25,
            max_active_sites=1,
        ),
        features=[
            "۵ پیشنهاد در ساعت / ۲۰ در روز",
            "۱ سایت فعال (Parscoders)",
            "هوش مصنوعی پایه + قالب آماده",
            "فیلتر مهارت و بودجه",
            "گزارش روزانه تلگرام",
            "پیش‌نمایش نامحدود",
        ],
        badge="🆓",
        color="#9CA3AF",
    ),
    "starter": Plan(
        id="starter",
        title="پایه",
        title_en="Starter",
        tagline="پرفروش برای فریلنسرهای فعال",
        price_toman=290_000,
        period_days=30,
        limits=PlanLimits(
            max_bids_per_hour=15,
            max_bids_per_day=80,
            max_bids_per_month=600,
            min_seconds_between_bids=90,
            max_projects_per_poll=40,
            max_active_sites=3,
            priority_queue=True,
        ),
        features=[
            "۱۵ در ساعت / ۸۰ در روز / ۶۰۰ در ماه",
            "۳ سایت همزمان (Parscoders + Ponisha + Karlancer)",
            "هوش مصنوعی پیشرفته + لحن سفارشی",
            "فیلتر پیشرفته و کلمات کلیدی نامحدود",
            "گزارش تحلیلی روزانه + هفتگی",
            "اولویت در صف ارسال",
            "پشتیبانی تلگرام (۲۴ ساعته)",
        ],
        badge="⭐",
        color="#3B82F6",
        recommended=True,
    ),
    "pro": Plan(
        id="pro",
        title="حرفه‌ای",
        title_en="Pro",
        tagline="برای حرفه‌ای‌ها و تیم‌های کوچک",
        price_toman=590_000,
        period_days=30,
        limits=PlanLimits(
            max_bids_per_hour=30,
            max_bids_per_day=200,
            max_bids_per_month=1500,
            min_seconds_between_bids=60,
            max_projects_per_poll=60,
            max_active_sites=99,
            priority_queue=True,
        ),
        features=[
            "۳۰ در ساعت / ۲۰۰ در روز / ۱۵۰۰ در ماه",
            "تمام سایت‌ها + سایت سفارشی",
            "GPT-4o / Claude + تحلیل پروفایل هوشمند",
            "داشبورد تحلیلی کامل + نمودار موفقیت",
            "زمان‌بندی هوشمند و پیشنهاد خودکار قیمت",
            "وب‌هوک و خروجی API",
            "پشتیبانی اولویت‌دار + تماس",
        ],
        badge="💎",
        color="#8B5CF6",
    ),
    "elite": Plan(
        id="elite",
        title="سازمانی",
        title_en="Elite",
        tagline="آژانس‌ها و تیم‌های بزرگ",
        price_toman=1_190_000,
        period_days=30,
        limits=PlanLimits(
            max_bids_per_hour=80,
            max_bids_per_day=500,
            max_bids_per_month=999_999,
            min_seconds_between_bids=30,
            max_projects_per_poll=100,
            max_active_sites=99,
            priority_queue=True,
        ),
        features=[
            "۸۰ در ساعت / ۵۰۰ در روز / نامحدود ماهانه",
            "همه قابلیت‌های Pro +",
            "چند اکانت ParsCoders همزمان",
            "مدیر اختصاصی + onboarding حضوری",
            "گزارش سازمانی + مدیریت تیم (۵ کاربر)",
            "SLA ۹۹٪ + پشتیبانی تلفنی",
            "قابلیت وایت‌لیبل و API کامل",
        ],
        badge="👑",
        color="#F59E0B",
    ),
}


def _apply_overrides(plan: Plan) -> Plan:
    """If admin changed price/limits via /admin_set, reflect it instantly."""
    try:
        from ..settings_store import get_store
        store = get_store()
        # price override
        price = store.get(f"admin.plans.{plan.id}.price_toman")
        period = store.get(f"admin.plans.{plan.id}.period_days")
        rec = store.get(f"admin.plans.{plan.id}.recommended")
        # limits overrides
        limits_kwargs: dict = {}
        for f in ["max_bids_per_hour","max_bids_per_day","max_bids_per_month","min_seconds_between_bids","max_active_sites","max_projects_per_poll"]:
            v = store.get(f"admin.plans.{plan.id}.limits.{f}")
            if v is not None:
                try:
                    limits_kwargs[f] = int(v)
                except Exception:
                    pass
        if price is None and period is None and rec is None and not limits_kwargs:
            return plan
        # build new Plan with overrides
        new_limits = plan.limits
        if limits_kwargs:
            new_limits = PlanLimits(
                max_bids_per_hour=limits_kwargs.get("max_bids_per_hour", plan.limits.max_bids_per_hour),
                max_bids_per_day=limits_kwargs.get("max_bids_per_day", plan.limits.max_bids_per_day),
                max_bids_per_month=limits_kwargs.get("max_bids_per_month", plan.limits.max_bids_per_month),
                min_seconds_between_bids=limits_kwargs.get("min_seconds_between_bids", plan.limits.min_seconds_between_bids),
                max_projects_per_poll=limits_kwargs.get("max_projects_per_poll", plan.limits.max_projects_per_poll),
                max_active_sites=limits_kwargs.get("max_active_sites", plan.limits.max_active_sites),
                priority_queue=plan.limits.priority_queue,
            )
        return Plan(
            id=plan.id, title=plan.title, title_en=plan.title_en, tagline=plan.tagline,
            price_toman=int(price) if price is not None else plan.price_toman,
            period_days=int(period) if period is not None else plan.period_days,
            limits=new_limits, features=plan.features, badge=plan.badge, color=plan.color,
            recommended=bool(rec) if rec is not None else plan.recommended,
            zarinpal_plan_id=plan.zarinpal_plan_id,
        )
    except Exception:
        return plan

def get_plan(plan_id: str) -> Plan:
    key = (plan_id or "free").strip().lower()
    if key not in PLANS:
        aliases = {"basic": "starter", "standard": "starter", "premium": "pro", "gold": "elite", "enterprise": "elite"}
        key = aliases.get(key, "free")
    base = PLANS[key]
    return _apply_overrides(base)


def all_plans() -> list[Plan]:
    order = ["free", "starter", "pro", "elite"]
    return [_apply_overrides(PLANS[k]) for k in order]


def paid_plans() -> list[Plan]:
    return [p for p in all_plans() if not p.is_free()]


def compare_features() -> list[dict]:
    """Row-based comparison for the UI table."""
    rows = [
        ("پیشنهاد در ساعت", {p.id: p.limits.max_bids_per_hour for p in all_plans()}),
        ("پیشنهاد در روز", {p.id: p.limits.max_bids_per_day for p in all_plans()}),
        ("پیشنهاد در ماه", {p.id: p.limits.max_bids_per_month for p in all_plans()}),
        ("سایت فعال", {p.id: ("نامحدود" if p.limits.max_active_sites > 3 else str(p.limits.max_active_sites)) for p in all_plans()}),
        ("هوش مصنوعی", {"free": "پایه", "starter": "پیشرفته", "pro": "GPT-4o+", "elite": "همه + اختصاصی"}),
        ("پشتیبانی", {"free": "جامعه", "starter": "۲۴h", "pro": "اولویت‌دار", "elite": "اختصاصی"}),
    ]
    return [{"label": label, "values": values} for label, values in rows]
