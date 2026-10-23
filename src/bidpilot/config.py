"""Typed configuration: YAML for behaviour, environment variables for secrets."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, ClassVar

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Profile(BaseModel):
    """Who the bid speaks for. Everything the LLM is allowed to claim lives here."""

    first_name: str = "نام شما"
    full_name: str = "نام و نام خانوادگی شما"
    role: str = "برنامه‌نویس فول‌استک"
    years_experience: int = 5
    stack: list[str] = Field(default_factory=lambda: ["Python", "Django", "FastAPI", "React"])
    highlights: list[str] = Field(default_factory=list)
    signature: str = ""
    portfolio_url: str = ""
    tone: str = "حرفه‌ای، صمیمی، بدون اغراق"
    forbidden_claims: list[str] = Field(
        default_factory=lambda: ["تضمین رتبه یک گوگل", "پشتیبانی ۲۴ ساعته رایگان", "تخفیف ویژه"]
    )

    PLACEHOLDERS: ClassVar[tuple[str, ...]] = (
        "نام شما",
        "نام و نام خانوادگی شما",
        "change me",
        "your name",
        "programmer",
    )

    def problems(self) -> list[str]:
        """Human-readable list of things that must be fixed before going live."""
        issues: list[str] = []
        for field in ("first_name", "full_name", "role"):
            value = (getattr(self, field) or "").strip()
            if not value or any(pl in value.lower() for pl in self.PLACEHOLDERS):
                issues.append(f"{field} در config/profile.yaml پر نشده است.")
        if not self.stack:
            issues.append("حداقل یک تکنولوژی در stack بنویسید.")
        if not self.highlights:
            issues.append("حداقل یک نمونه‌کار/دستاورد در highlights بنویسید (متن پیشنهاد به آن نیاز دارد).")
        if self.years_experience <= 0:
            issues.append("years_experience باید بزرگ‌تر از صفر باشد.")
        return issues

    @property
    def stack_short(self) -> str:
        return "، ".join(self.stack[:4])

    def intro_line(self) -> str:
        years = (
            f"{self.years_experience} سال تجربه" if self.years_experience else "تجربه اجرای پروژه‌های مشابه"
        )
        return f"من {self.first_name} هستم، {self.role} با {years} در {self.stack_short}."


class RuntimeConfig(BaseModel):
    submit_mode: str = "dry_run"  # "live" = actually POST the bid
    poll_interval_seconds: int = 120
    jitter_seconds: int = 30
    max_bids_per_hour: int = 10
    max_bids_per_day: int = 60
    min_seconds_between_bids: int = 90
    max_projects_per_poll: int = 30
    notify_on_skip: bool = True
    notify_error_cooldown_seconds: int = 1_800
    request_timeout_seconds: float = 25.0
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )

    @property
    def live(self) -> bool:
        return self.submit_mode.strip().lower() == "live"


class FiltersConfig(BaseModel):
    include_keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    min_budget: int | None = None
    max_budget: int | None = None
    categories_allow: list[str] = Field(default_factory=list)
    skip_negotiable: bool = False


class ProposalConfig(BaseModel):
    language: str = "fa"
    min_chars: int = 320
    max_chars: int = 1_500
    max_emoji: int = 2
    on_generation_failure: str = "skip"  # "skip" | "template"
    banned_phrases: list[str] = Field(
        default_factory=lambda: ["تضمین رتبه", "ارزان‌ترین قیمت", "۱۰۰٪ تضمینی", "بهترین در ایران"]
    )


class TelegramConfig(BaseModel):
    enabled: bool = True
    chat_ids: list[str] = Field(default_factory=list)
    send_full_proposal: bool = True


class LLMConfig(BaseModel):
    enabled: bool = True
    provider: str = "openai_compatible"
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    temperature: float = 0.6
    max_tokens: int = 900
    timeout_seconds: float = 45.0
    extra_headers: dict[str, str] = Field(default_factory=dict)


class SourceConfig(BaseModel):
    type: str
    enabled: bool = True
    settings: dict[str, Any] = Field(default_factory=dict)


class AppConfig(BaseModel):
    log_level: str = "INFO"
    database_url: str = "sqlite+aiosqlite:///data/bidpilot.sqlite3"
    data_dir: str = "data"
    profile: Profile = Field(default_factory=Profile)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    pricing: dict[str, Any] = Field(default_factory=dict)
    filters: FiltersConfig = Field(default_factory=FiltersConfig)
    proposal: ProposalConfig = Field(default_factory=ProposalConfig)
    notifications: TelegramConfig = Field(default_factory=TelegramConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    sources: list[SourceConfig] = Field(default_factory=list)

    def source(self, type_name: str) -> SourceConfig | None:
        for src in self.sources:
            if src.type == type_name:
                return src
        return None


class Secrets(BaseSettings):
    """Everything sensitive — loaded from environment / .env only."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str | None = None
    telegram_chat_ids: str | None = None  # comma separated: "12345,-100987654"
    llm_api_key: str | None = None
    parscoders_cookie: str | None = None
    parscoders_username: str | None = None
    parscoders_password: str | None = None
    database_url: str | None = None

    def chat_ids_list(self) -> list[str]:
        raw = self.telegram_chat_ids or ""
        return [c.strip() for c in raw.replace(";", ",").split(",") if c.strip()]


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_yaml(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    with p.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_config(config_path: str | Path = "config/config.yaml") -> AppConfig:
    """Load main config, then merge optional local overrides, then env overrides."""
    raw = load_yaml(config_path)
    local_path = Path(config_path).with_name("config.local.yaml")
    if local_path.exists():
        raw = deep_merge(raw, load_yaml(local_path))

    profile_path = Path(raw.get("profile_path") or Path(config_path).parent / "profile.yaml")
    if profile_path.exists():
        raw["profile"] = deep_merge(load_yaml(profile_path), raw.get("profile", {}) or {})

    cfg = AppConfig(**raw)

    secrets = get_secrets()
    if secrets.database_url:
        cfg.database_url = secrets.database_url
    chat_ids = secrets.chat_ids_list()
    if chat_ids:
        cfg.notifications.chat_ids = chat_ids
    return cfg


@lru_cache(maxsize=1)
def get_secrets() -> Secrets:
    return Secrets()


def load_profile(path: str | Path) -> Profile:
    return Profile(**load_yaml(path))


def reset_secret_cache() -> None:
    """Test helper."""
    get_secrets.cache_clear()
