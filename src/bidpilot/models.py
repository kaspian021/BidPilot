"""Domain models shared by every source adapter, strategy and notifier."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, computed_field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Budget(BaseModel):
    """A normalized budget range, always stored in Toman (IRT) as integer amounts."""

    model_config = ConfigDict(extra="ignore")

    min: int | None = None
    max: int | None = None
    negotiable: bool = False
    currency: str = "IRT"
    source_text: str = ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_known(self) -> bool:
        return self.min is not None or self.max is not None

    @property
    def lower(self) -> int | None:
        return self.min if self.min is not None else self.max

    @property
    def upper(self) -> int | None:
        return self.max if self.max is not None else self.min

    def clamp(self, amount: int) -> int:
        low, high = self.lower, self.upper
        if low is not None and amount < low:
            amount = low
        if high is not None and amount > high:
            amount = high
        return amount

    def as_text(self) -> str:
        if self.is_known and self.lower == self.upper:
            return f"{self.lower:,} تومان"
        if self.is_known:
            return f"{self.lower:,} تا {self.upper:,} تومان"
        return "توافقی"


class Project(BaseModel):
    """A normalized job posting from any marketplace."""

    model_config = ConfigDict(extra="ignore")

    source: str
    external_id: str
    title: str
    url: str = ""
    description: str = ""
    budget: Budget | None = None
    skills: list[str] = Field(default_factory=list)
    category: str | None = None
    employer: str | None = None
    #: the delivery time the employer asked for ("۱۰ روز", "۲ هفته") — never invented
    employer_deadline: str = ""
    key_requirements: list[str] = Field(default_factory=list)
    employer_questions: list[str] = Field(default_factory=list)
    deliverables: list[str] = Field(default_factory=list)
    published_at: datetime | None = None
    fetched_at: datetime = Field(default_factory=utcnow)
    raw: dict = Field(default_factory=dict)

    @property
    def uid(self) -> str:
        return f"{self.source}:{self.external_id}"

    @property
    def text_for_llm(self) -> str:
        parts = [f"عنوان: {self.title}"]
        if self.category:
            parts.append(f"دسته‌بندی: {self.category}")
        if self.skills:
            parts.append(f"مهارت‌های مورد نیاز: {', '.join(self.skills)}")
        if self.budget:
            parts.append(f"بودجه کارفرما: {self.budget.as_text()}")
        if self.employer_deadline:
            parts.append(f"زمان تحویل اعلامی کارفرما: {self.employer_deadline}")
        if self.deliverables:
            parts.append("خروجی‌ها و تحویلی‌های مورد انتظار کارفرما:\n- " + "\n- ".join(self.deliverables))
        if self.key_requirements:
            parts.append("نیازمندی‌ها و الزامات کلیدی کارفرما:\n- " + "\n- ".join(self.key_requirements))
        if self.employer_questions:
            parts.append(
                "سؤالات یا خواسته‌های صریح کارفرما از فریلنسر:\n- " + "\n- ".join(self.employer_questions)
            )
        if self.description:
            parts.append(f"توضیحات کامل کارفرما:\n{self.description}")
        return "\n".join(parts)


class Proposal(BaseModel):
    """A generated bid message plus provenance (useful for audits and A/B tests)."""

    model_config = ConfigDict(extra="ignore")

    text: str
    generated_by: str = "template"  # "llm:<model>" or "template"
    latency_ms: int | None = None
    usage: dict = Field(default_factory=dict)


class SubmissionStatus(str, Enum):
    DRY_RUN = "dry_run"
    SUBMITTED = "submitted"
    FAILED = "failed"
    SKIPPED = "skipped"


class SubmissionResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: SubmissionStatus
    message: str = ""
    external_bid_id: str | None = None
    http_status: int | None = None
    payload_debug: dict = Field(default_factory=dict)


class BidPlan(BaseModel):
    """Everything decided for one project, before/after submission."""

    model_config = ConfigDict(extra="ignore")

    project: Project
    amount: int | None = None
    delivery_days: int | None = None
    proposal: Proposal | None = None
    result: SubmissionResult | None = None
    rationale: str = ""


class SkipReason(str, Enum):
    DUPLICATE = "duplicate"
    FILTERED = "filtered"
    NO_PRICE = "no_price"
    PROPOSAL_INVALID = "proposal_invalid"
    RATE_LIMITED = "rate_limited"
    ERROR = "error"


class EventKind(str, Enum):
    NEW_PROJECT = "new_project"
    BID_SUBMITTED = "bid_submitted"
    BID_DRY_RUN = "bid_dry_run"
    BID_SKIPPED = "bid_skipped"
    ERROR = "error"
    DIGEST = "digest"


class Event(BaseModel):
    """Notification payload handed to notifiers (Telegram, console, webhook...)."""

    model_config = ConfigDict(extra="ignore")

    kind: EventKind
    title: str
    body: str = ""
    project: Project | None = None
    bid: BidPlan | None = None
    created_at: datetime = Field(default_factory=utcnow)
