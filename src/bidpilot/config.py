"""Typed configuration.

Priority (highest first):
  1. ``data/settings.json`` — everything the user answered in ``bidpilot setup`` or
     in the Telegram bot. No ``.env`` file is needed.
  2. environment variables / ``.env`` — optional legacy fallback (Docker, CI).
  3. ``config/config.yaml`` + ``config/profile.yaml`` — committed defaults.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, ClassVar

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .llm_providers import DEFAULT_FALLBACKS, DEFAULT_PROVIDER, get_preset
from .settings_store import SettingsStore, get_store

log = logging.getLogger(__name__)


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
                issues.append(f"{field} در پروفایل پر نشده است (bidpilot setup یا config/profile.yaml).")
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
    """What the bid should *sound* like — set by the user, not by the developer."""

    language: str = "fa"
    min_chars: int = 320
    max_chars: int = 1_500
    max_emoji: int = 2
    greeting: str = "سلام و وقت بخیر"
    #: free-text description of the voice the user wants (e.g. "خودمانی و گرم").
    #: When empty, ``profile.tone`` is used.
    tone: str = ""
    #: the user's own rules for how to pitch, e.g. "همیشه اول یک سؤال بپرس و
    #: روی تحویل مرحله‌ای تأکید کن؛ از تعریف از کارفرما استفاده نکن."
    style_instructions: str = ""
    on_generation_failure: str = "skip"  # "skip" | "template"
    banned_phrases: list[str] = Field(
        default_factory=lambda: ["تضمین رتبه", "ارزان‌ترین قیمت", "۱۰۰٪ تضمینی", "بهترین در ایران"]
    )


class TelegramConfig(BaseModel):
    enabled: bool = True
    chat_ids: list[str] = Field(default_factory=list)
    send_full_proposal: bool = True


class LLMConfig(BaseModel):
    """OpenAI-compatible model settings.

    The default is **Groq** because it offers a free tier with a permanent token
    (see :mod:`bidpilot.llm_providers` for the full list of free presets).
    ``base_url`` / ``model`` are filled from the preset when left empty.
    """

    enabled: bool = True
    provider: str = DEFAULT_PROVIDER
    base_url: str = ""
    model: str = ""
    #: tried in order when the primary provider is unavailable or rate-limited
    fallbacks: list[str] = Field(default_factory=lambda: list(DEFAULT_FALLBACKS))
    temperature: float = 0.6
    max_tokens: int = 900
    timeout_seconds: float = 45.0
    extra_headers: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _fill_from_preset(self) -> LLMConfig:
        try:
            preset = get_preset(self.provider)
        except KeyError:
            return self
        if not self.base_url.strip():
            self.base_url = preset.base_url
        if not self.model.strip():
            self.model = preset.model
        return self

    @property
    def provider_title(self) -> str:
        try:
            return get_preset(self.provider).title
        except KeyError:
            return self.provider


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
    """Everything sensitive.

    Values come from environment / ``.env`` (optional) and are then overridden by
    the persisted settings store — i.e. whatever the user typed in the wizard or
    in the Telegram bot. Nothing here has to be provided via ``.env``.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str | None = None
    telegram_chat_ids: str | None = None  # comma separated: "12345,-100987654"
    llm_api_key: str | None = None
    #: provider id -> permanent key (Groq, Gemini, OpenRouter, …)
    llm_keys: dict[str, str] = Field(default_factory=dict)
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


#: Store sections whose values are plain (non-secret) configuration.
STORE_CONFIG_SECTIONS: dict[str, tuple[str, ...]] = {
    "profile": (
        "first_name",
        "full_name",
        "role",
        "years_experience",
        "stack",
        "highlights",
        "portfolio_url",
        "signature",
        "tone",
    ),
    "proposal": (
        "language",
        "tone",
        "style_instructions",
        "greeting",
        "min_chars",
        "max_chars",
        "max_emoji",
    ),
    "llm": ("provider", "base_url", "model", "fallbacks"),
    "runtime": ("submit_mode", "poll_interval_seconds", "max_bids_per_hour", "max_bids_per_day"),
}


def _stored_section(store: SettingsStore, section: str) -> dict[str, Any]:
    stored: dict[str, Any] = {}
    for field in STORE_CONFIG_SECTIONS.get(section, ()):
        value = store.get(f"{section}.{field}")
        if value is not None and value != "":
            stored[field] = value
    return stored


def apply_store_to_raw(raw: dict[str, Any], store: SettingsStore) -> dict[str, Any]:
    """Overlay persisted user settings on top of the YAML configuration."""
    raw = dict(raw or {})
    for section in ("profile", "proposal", "llm", "runtime"):
        stored = _stored_section(store, section)
        if stored:
            raw[section] = deep_merge(raw.get(section) or {}, stored)

    # If the user gave a new name but no signature, never keep the placeholder
    # signature from profile.yaml (it would sign the bid with someone else's name).
    stored_profile = _stored_section(store, "profile")
    if stored_profile.get("full_name") and not stored_profile.get("signature"):
        profile = dict(raw.get("profile") or {})
        profile["signature"] = ""
        raw["profile"] = profile

    chat_ids = store.get("telegram.chat_ids")
    if chat_ids:
        notifications = dict(raw.get("notifications") or {})
        notifications["chat_ids"] = chat_ids
        raw["notifications"] = notifications

    marketplace = store.get("marketplace.name")
    if marketplace:
        sources = [dict(src) for src in (raw.get("sources") or [])]
        found = False
        for source in sources:
            source["enabled"] = source.get("type") == marketplace
            if source.get("type") == marketplace:
                found = True
        if not found:
            sources.insert(0, {"type": str(marketplace), "enabled": True, "settings": {}})
        base_url = store.get("marketplace.base_url")
        if base_url:
            for source in sources:
                if source.get("type") == marketplace:
                    source["settings"] = {**(source.get("settings") or {}), "base_url": base_url}
        raw["sources"] = sources

    return raw


def secrets_from_store(base: Secrets, store: SettingsStore) -> Secrets:
    """Return a copy of ``base`` with every stored credential applied (store wins)."""
    updates: dict[str, Any] = {}

    token = store.get("telegram.bot_token")
    if token:
        updates["telegram_bot_token"] = token
    chat_ids = store.get("telegram.chat_ids")
    if chat_ids:
        updates["telegram_chat_ids"] = ",".join(str(c) for c in chat_ids)

    keys = {str(k): str(v) for k, v in (store.get("llm.keys") or {}).items() if v}
    api_key = store.get("llm.api_key")
    provider = str(store.get("llm.provider") or DEFAULT_PROVIDER).strip().lower()
    if api_key:
        keys[str(provider)] = str(api_key)
    if keys:
        updates["llm_keys"] = {**base.llm_keys, **keys}
        primary = keys.get(str(provider)) or api_key
        if primary:
            updates["llm_api_key"] = primary
    elif api_key:
        updates["llm_api_key"] = api_key

    for field in ("username", "password", "cookie"):
        value = store.get(f"credentials.parscoders.{field}")
        if value:
            updates[f"parscoders_{field}"] = value

    return base.model_copy(update=updates) if updates else base


def load_config(
    config_path: str | Path = "config/config.yaml",
    *,
    store: SettingsStore | None = None,
) -> AppConfig:
    """Load YAML defaults, local overrides and finally the persisted user settings."""
    store = store or get_store()
    raw = load_yaml(config_path)
    local_path = Path(config_path).with_name("config.local.yaml")
    if local_path.exists():
        raw = deep_merge(raw, load_yaml(local_path))

    profile_path = Path(raw.get("profile_path") or Path(config_path).parent / "profile.yaml")
    if profile_path.exists():
        raw["profile"] = deep_merge(load_yaml(profile_path), raw.get("profile", {}) or {})

    raw = apply_store_to_raw(raw, store)
    cfg = AppConfig(**raw)

    # Derive secrets from *this* store (plus env/.env), not from the process cache,
    # so an explicit store cannot be contaminated by an earlier caller.
    secrets = secrets_from_store(Secrets(), store)
    if secrets.database_url:
        cfg.database_url = secrets.database_url
    chat_ids = secrets.chat_ids_list()
    if chat_ids:
        cfg.notifications.chat_ids = chat_ids
    return cfg


@lru_cache(maxsize=1)
def get_secrets() -> Secrets:
    """Environment / .env values, overridden by the persisted settings store."""
    base = Secrets()
    try:
        stored = secrets_from_store(base, get_store())
    except Exception as exc:  # a broken settings file must not brick the app
        log.warning("Could not read %s: %s", get_store().path, exc)
        stored = base
    return stored


def load_profile(path: str | Path) -> Profile:
    return Profile(**load_yaml(path))


def reset_secret_cache() -> None:
    """Test helper."""
    get_secrets.cache_clear()
