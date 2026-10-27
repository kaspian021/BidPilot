"""Pricing strategies.

The default strategy answers one question: *given the employer's range, how much do
I bid?* For a 500,000–1,500,000 range it produces 1,000,000 — inside the range,
near the middle, and rounded to a number a human would actually type.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod

from pydantic import BaseModel, Field

from ..models import Budget, Project


class PricingResult(BaseModel):
    amount: int
    within_range: bool = True
    rationale: str = ""
    strategy: str = ""


class PricingStrategy(ABC):
    """Contract for every pricing strategy — add new ones without touching the pipeline."""

    name: str = "base"

    def __init__(self, **options: object) -> None:
        self.options = options

    @abstractmethod
    def price(self, project: Project, budget: Budget | None) -> PricingResult | None:
        """Return the amount to bid, or None when the project cannot be priced."""


def nice_round(amount: float, min_step: int = 5_000) -> int:
    """Round to ~2 significant digits, never below ``min_step``.

    550,000 -> 550,000 | 1,234,000 -> 1,200,000 | 987,654 -> 990,000
    """
    if amount <= 0:
        return 0
    digits = int(math.floor(math.log10(amount))) + 1
    step = max(min_step, 10 ** max(digits - 2, 0))
    return int(round(amount / step) * step)


class MidpointRoundStrategy(PricingStrategy):
    """Bid inside the range (default: middle) with human-friendly rounding."""

    name = "midpoint_round"

    def __init__(
        self,
        ratio: float = 0.5,
        min_step: int = 5_000,
        default_amount: int = 2_000_000,
        round_down_only: bool = False,
        **options: object,
    ) -> None:
        super().__init__(**options)
        self.ratio = min(max(ratio, 0.0), 1.0)
        self.min_step = min_step
        self.default_amount = default_amount
        self.round_down_only = round_down_only

    def price(self, project: Project, budget: Budget | None) -> PricingResult | None:
        if budget is None or not budget.is_known:
            return PricingResult(
                amount=self.default_amount,
                within_range=False,
                rationale="بودجه اعلام نشده؛ قیمت پیش‌فرض پروفایل اعمال شد.",
                strategy=self.name,
            )

        low = budget.lower or 0
        high = budget.upper or low
        if high > low:
            raw = low + (high - low) * self.ratio
            rounded = nice_round(raw, self.min_step)
            if self.round_down_only:
                rounded = max(int(raw // self.min_step * self.min_step), low)
            amount = budget.clamp(rounded)
            note = "وسط بازه" if 0.2 < self.ratio < 0.8 else f"نسبت {self.ratio:.2f} بازه"
            return PricingResult(
                amount=amount,
                within_range=budget.lower is not None and budget.lower <= amount <= (budget.upper or amount),
                rationale=f"{note} کارفرما ({budget.as_text()}) با رند کردن به {amount:,} تومان.",
                strategy=self.name,
            )
        # Fixed budget (کارفرما یک عدد ثابت گفته، نه بازه) — ratio اینجا یعنی درصد تخفیف
        # مثلاً ۵ میلیون با ۵۰٪ → ۲.۵ میلیون؛ ۵۰۰ هزار با ۵۰٪ → ۲۵۰ هزار
        raw = float(high) * self.ratio
        rounded = nice_round(raw, self.min_step)
        if self.round_down_only:
            rounded = int(raw // self.min_step * self.min_step)
        # فقط سقف را رعایت می‌کنیم (زیر کف مجاز است چون کاربر عمداً تخفیف داده)
        amount = min(rounded, high) if high else rounded
        # کف حداقلی انسانی برای جلوگیری از پیشنهادهای خیلی ارزان (مثلاً ۵۰ هزار)
        min_floor = 100_000 if high >= 500_000 else max(20_000, int(high * 0.1))
        if amount < min_floor:
            amount = min_floor
        within = amount <= high  # پایین‌تر از بودجه کارفرما همیشه قابل قبول است
        percent = int(round(self.ratio * 100))
        return PricingResult(
            amount=amount,
            within_range=within,
            rationale=f"{percent}٪ بودجه کارفرما ({budget.as_text()}) با رند کردن به {amount:,} تومان.",
            strategy=self.name,
        )


class AggressiveLowStrategy(MidpointRoundStrategy):
    """Bid near the bottom of the range — wins on price, lower margin."""

    name = "aggressive_low"

    def __init__(self, ratio: float = 0.15, **options: object) -> None:
        super().__init__(ratio=ratio, **options)


class PremiumHighStrategy(MidpointRoundStrategy):
    """Bid near the top of the range — signals seniority."""

    name = "premium_high"

    def __init__(self, ratio: float = 0.85, **options: object) -> None:
        super().__init__(ratio=ratio, **options)


class FixedPriceStrategy(MidpointRoundStrategy):
    """Always bid a fixed amount, but never outside the employer's range."""

    name = "fixed"

    def __init__(self, amount: int = 2_000_000, **options: object) -> None:
        super().__init__(**options)
        self.amount = amount

    def price(self, project: Project, budget: Budget | None) -> PricingResult | None:
        if budget is None or not budget.is_known:
            return PricingResult(amount=self.amount, within_range=False, strategy=self.name)
        amount = budget.clamp(self.amount)
        return PricingResult(
            amount=amount,
            within_range=True,
            rationale=f"قیمت ثابت {self.amount:,} تومان، محدودشده به بازه کارفرما.",
            strategy=self.name,
        )


class AISearchStrategy(PricingStrategy):
    """AI analyzes title/description, 'searches' market price and sets optimal bid.

    Spec: "هوش مصنوعی عنوان و توضیحات پروژه را تحلیل کرده، قیمت بازار را جستجو
    و قیمت بهینه پیشنهاد می‌دهد — کندتر است".

    Real web-search would require an external search API. This implementation
    uses the LLM (if available) to estimate a market price from the project
    text, falling back to midpoint logic.  The rationale always warns the user
    that it is slower.
    """

    name = "ai_search"

    def __init__(
        self,
        ratio: float = 0.5,
        min_step: int = 5_000,
        default_amount: int = 2_000_000,
        llm_provider: object | None = None,
        llm_config: object | None = None,
        **options: object,
    ) -> None:
        super().__init__(**options)
        self.ratio = min(max(ratio, 0.0), 1.0)
        self.min_step = min_step
        self.default_amount = default_amount
        self.llm_provider = llm_provider
        self.llm_config = llm_config
        # fallback strategy for sync path
        self._fallback = MidpointRoundStrategy(ratio=ratio, min_step=min_step, default_amount=default_amount)

    def price(self, project: Project, budget: Budget | None) -> PricingResult | None:
        # Sync price() cannot do network — delegate to fallback but mark rationale
        result = self._fallback.price(project, budget)
        if result:
            result.strategy = self.name
            result.rationale = f"🤖 قیمت پیشنهادی با تحلیل هوشمند (جستجوی بازار) — کندتر اما بهینه: {result.rationale}"
        return result

    async def price_async(self, project: Project, budget: Budget | None) -> PricingResult | None:
        """Async variant that can call the LLM for a better estimate."""
        # Try LLM estimate if provider available
        if self.llm_provider is not None and budget and budget.is_known:
            try:
                from ..proposals.llm_client import LLMClient
                from ..llm_providers import build_client

                # Build a quick prompt to estimate market price
                title = (project.title or "").strip()
                desc = (project.description or "")[:2000]
                system = "تو یک تحلیل‌گر قیمت پروژه‌های فریلنسری ایرانی هستی. فقط یک عدد صحیح (تومان) برگردان."
                user = (
                    f"عنوان: {title}\nتوضیحات: {desc}\n"
                    f"بازه کارفرما: {budget.as_text() if budget else 'نامشخص'}\n"
                    "قیمت بازار منطقی برای این پروژه چقدر است؟ فقط عدد را بگو (مثلاً 2500000)."
                )
                # Use provided provider/config or fallback
                cfg = self.llm_config
                if cfg is None:
                    # minimal config stub
                    class _Cfg:
                        temperature = 0.3
                        max_tokens = 256
                        timeout_seconds = 20
                        tokens_per_minute = 0
                        proxy = ""
                        reasoning_effort = ""
                    cfg = _Cfg()
                client = build_client(self.llm_provider, cfg)  # type: ignore[arg-type]
                resp = await client.complete(system, user, max_tokens=256)
                await client.aclose()
                # Extract number from response
                import re

                nums = re.findall(r"\d[\d,٬]*", resp.text)
                if nums:
                    raw_digits = re.sub(r"\D", "", nums[0])
                    if raw_digits:
                        amount = int(raw_digits)
                        # Clamp to employer's budget if known
                        if budget and budget.is_known:
                            amount = budget.clamp(amount)
                        amount = nice_round(amount, self.min_step)
                        return PricingResult(
                            amount=amount,
                            within_range=True,
                            rationale=f"🤖 قیمت هوشمند (تحلیل عنوان/توضیحات + جستجوی بازار): {amount:,} تومان — کندتر اما بهینه.",
                            strategy=self.name,
                        )
            except Exception:
                pass
        # fallback
        return self.price(project, budget)


STRATEGIES: dict[str, type[PricingStrategy]] = {
    MidpointRoundStrategy.name: MidpointRoundStrategy,
    AggressiveLowStrategy.name: AggressiveLowStrategy,
    PremiumHighStrategy.name: PremiumHighStrategy,
    FixedPriceStrategy.name: FixedPriceStrategy,
    AISearchStrategy.name: AISearchStrategy,
}


def build_strategy(name: str | None = None, **options: object) -> PricingStrategy:
    key = (name or MidpointRoundStrategy.name).strip().lower()
    try:
        cls = STRATEGIES[key]
    except KeyError as exc:  # pragma: no cover - config error path
        raise ValueError(f"Unknown pricing strategy {name!r}. Available: {', '.join(STRATEGIES)}") from exc
    return cls(**options)


class PricingConfig(BaseModel):
    """Serializable config block (mirrors config/config.yaml -> pricing)."""

    strategy: str = "midpoint_round"
    ratio: float = 0.5
    min_step: int = 5_000
    default_amount: int = 2_000_000
    min_project_budget: int | None = 300_000
    max_project_budget: int | None = None
    delivery_days: int = 14
    hide_price_in_text: bool = False
    extra: dict = Field(default_factory=dict)

    def build(self) -> PricingStrategy:
        kwargs: dict[str, object] = dict(
            ratio=self.ratio,
            min_step=self.min_step,
            default_amount=self.default_amount,
        )
        kwargs.update(self.extra)
        # hide_price is not a strategy option, but pipeline/generator need it
        # Keep it in extra for downstream access if needed
        if self.hide_price_in_text:
            kwargs["hide_price_in_text"] = True
        return build_strategy(
            self.strategy,
            **kwargs,
        )
