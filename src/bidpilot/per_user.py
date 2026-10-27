"""Per-user settings — LLM API key isolation, onboarding state, and profile.

Each Telegram user (chat_id) gets isolated LLM credentials stored under
``users.{chat_id}.llm.*`` in the global SettingsStore. This avoids a DB
migration while satisfying the spec: "each user's supplied API key/model used
only for that user, never for all users". A thin DB sync is optional; the
file is the source of truth for the bot (0600, atomic writes).

Also tracks onboarding progress so the dashboard can be blocked until Steps
1-2 are complete.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

# Context var for per-user credential lookup during source creation
current_chat_id: ContextVar[str | None] = ContextVar("current_chat_id", default=None)

from .llm_providers import DEFAULT_PROVIDER, PRESETS, get_preset
from .settings_store import SettingsStore

# ---------------------------------------------------------------------------
# Key helpers
# ---------------------------------------------------------------------------

def user_key(chat_id: str | int, dotted: str) -> str:
    """Namespace a dotted key for a specific chat user.

    Example: user_key(123, "llm.provider") -> "users.123.llm.provider"
    """
    return f"users.{str(chat_id).strip()}.{dotted.strip()}"


def get_user(store: SettingsStore, chat_id: str | int, dotted: str, default: Any = None) -> Any:
    return store.get(user_key(chat_id, dotted), default)


def set_user(store: SettingsStore, chat_id: str | int, dotted: str, value: Any) -> None:
    store.set(user_key(chat_id, dotted), value)


def update_user(store: SettingsStore, chat_id: str | int, mapping: dict[str, Any]) -> None:
    """Atomic update of several per-user keys."""
    payload = {user_key(chat_id, k): v for k, v in mapping.items()}
    store.update(payload)


def delete_user(store: SettingsStore, chat_id: str | int, dotted: str) -> bool:
    return store.delete(user_key(chat_id, dotted))


# ---------------------------------------------------------------------------
# LLM per-user helpers
# ---------------------------------------------------------------------------

def get_user_llm_provider_id(store: SettingsStore, chat_id: str | int) -> str | None:
    v = get_user(store, chat_id, "llm.provider")
    if v:
        return str(v).strip().lower()
    return None


def get_user_llm_api_key(store: SettingsStore, chat_id: str | int) -> str | None:
    # per-provider key storage — strictly per-user, no global fallback (isolation)
    provider = get_user_llm_provider_id(store, chat_id) or DEFAULT_PROVIDER
    key = get_user(store, chat_id, "llm.api_key")
    if key:
        return str(key)
    per_provider = get_user(store, chat_id, f"llm.keys.{provider}")
    if per_provider:
        return str(per_provider)
    return None


def get_user_llm_model(store: SettingsStore, chat_id: str | int) -> str | None:
    v = get_user(store, chat_id, "llm.model")
    if v:
        return str(v)
    # fallback to preset default for provider — strictly per-user
    provider = get_user_llm_provider_id(store, chat_id)
    if provider and provider in PRESETS:
        return PRESETS[provider].model
    return None


def get_user_llm_base_url(store: SettingsStore, chat_id: str | int) -> str | None:
    v = get_user(store, chat_id, "llm.base_url")
    if v:
        return str(v)
    provider = get_user_llm_provider_id(store, chat_id)
    if provider and provider in PRESETS:
        return PRESETS[provider].base_url
    return None


def is_user_llm_verified(store: SettingsStore, chat_id: str | int) -> bool:
    """True if the user's API key has been live-verified."""
    return bool(get_user(store, chat_id, "llm.verified"))


def set_user_llm_verified(store: SettingsStore, chat_id: str | int, verified: bool = True) -> None:
    set_user(store, chat_id, "llm.verified", bool(verified))
    if verified:
        from datetime import datetime, timezone
        set_user(store, chat_id, "llm.verified_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))


def build_user_llm_provider(store: SettingsStore, chat_id: str | int):
    """Build an LLMProvider for this user, or None if not configured."""
    from .llm_providers import LLMProvider

    provider_id = get_user_llm_provider_id(store, chat_id)
    if not provider_id:
        return None
    try:
        preset = get_preset(provider_id)
    except KeyError:
        return None
    api_key = get_user_llm_api_key(store, chat_id)
    if preset.requires_key and not api_key:
        return None
    model = get_user_llm_model(store, chat_id) or preset.model
    base_url = get_user_llm_base_url(store, chat_id) or preset.base_url
    return LLMProvider(
        id=preset.id,
        base_url=base_url,
        model=model,
        api_key=api_key,
        extra_headers=dict(preset.extra_headers),
        note=preset.note,
        tokens_per_minute=preset.tokens_per_minute,
    )


# ---------------------------------------------------------------------------
# Onboarding state
# ---------------------------------------------------------------------------

def is_llm_onboarding_done(store: SettingsStore, chat_id: str | int) -> bool:
    """Step 1: provider + verified key + model selected."""
    provider = get_user_llm_provider_id(store, chat_id)
    if not provider:
        return False
    key = get_user_llm_api_key(store, chat_id)
    if not key:
        return False
    if not is_user_llm_verified(store, chat_id):
        return False
    model = get_user_llm_model(store, chat_id)
    if not model:
        return False
    return True


def is_profile_onboarding_done(store: SettingsStore, chat_id: str | int) -> bool:
    """Step 2: profile source chosen and (if not skip) fields present — strictly per-user."""
    skipped = get_user(store, chat_id, "onboarding.profile_skipped")
    if skipped:
        return True
    source = get_user(store, chat_id, "profile.source")
    if not source:
        full_name = get_user(store, chat_id, "profile.full_name")
        if full_name:
            return True
        return False
    s = str(source).strip().lower()
    if s == "skip":
        return True
    imported = get_user(store, chat_id, "profile.imported_at")
    if imported:
        return True
    full_name = get_user(store, chat_id, "profile.full_name")
    if full_name:
        return True
    pending = get_user(store, chat_id, "profile.import_pending")
    if pending:
        return False
    return False


def is_marketplace_onboarding_done(store: SettingsStore, chat_id: str | int) -> bool:
    """Step 3: marketplace + tone + pricing — all per-user set."""
    mname = get_user(store, chat_id, "marketplace.name")
    if not mname:
        return False
    tone = get_user(store, chat_id, "proposal.tone")
    if not tone:
        return False
    bp = get_user(store, chat_id, "pricing.bid_percent")
    if bp is None:
        return False
    return True


def is_onboarding_blocked(store: SettingsStore, chat_id: str | int) -> bool:
    """True if dashboard should be hidden until steps 1-2 pass."""
    # Global setup (bot token) takes precedence: if bot token not set, don't block (old wizard handles it)
    if not store.get("telegram.bot_token"):
        return False
    # If chat_ids empty (first owner), also don't block — let ownership claim flow handle it
    ids = store.get_list("telegram.chat_ids") if hasattr(store, "get_list") else store.get("telegram.chat_ids")
    if not ids:
        return False
    return not (is_llm_onboarding_done(store, chat_id) and is_profile_onboarding_done(store, chat_id))


def onboarding_progress(store: SettingsStore, chat_id: str | int) -> dict[str, Any]:
    return {
        "llm_done": is_llm_onboarding_done(store, chat_id),
        "profile_done": is_profile_onboarding_done(store, chat_id),
        "blocked": is_onboarding_blocked(store, chat_id),
        "provider": get_user_llm_provider_id(store, chat_id),
        "model": get_user_llm_model(store, chat_id),
        "verified": is_user_llm_verified(store, chat_id),
    }

__all__ = [
    "user_key",
    "get_user",
    "set_user",
    "update_user",
    "delete_user",
    "get_user_llm_provider_id",
    "get_user_llm_api_key",
    "get_user_llm_model",
    "get_user_llm_base_url",
    "is_user_llm_verified",
    "set_user_llm_verified",
    "build_user_llm_provider",
    "apply_per_user_config",
    "user_status_text",
    "current_chat_id",
    "is_llm_onboarding_done",
    "is_profile_onboarding_done",
    "is_marketplace_onboarding_done",
    "is_onboarding_blocked",
    "onboarding_progress",
]

def apply_per_user_config(store: SettingsStore, chat_id: str | int, cfg, secrets=None):
    """Inject per-user LLM, pricing, profile into cfg/secrets (mutates cfg, returns secrets)."""
    from .llm_providers import DEFAULT_PROVIDER
    from .config import Secrets, secrets_from_store
    # Set context for per-user credential lookup during source creation
    try:
        current_chat_id.set(str(chat_id))
    except Exception:
        pass

    # LLM
    provider = build_user_llm_provider(store, chat_id)
    if provider is not None:
        cfg.llm.provider = provider.id
        cfg.llm.base_url = provider.base_url
        cfg.llm.model = provider.model
        # pricing per-user
        strat = get_user(store, chat_id, "pricing.strategy")
        if strat:
            cfg.pricing["strategy"] = str(strat)
        hide = get_user(store, chat_id, "pricing.hide_price_in_text")
        if hide is not None:
            cfg.pricing["hide_price_in_text"] = bool(hide)
        bp = get_user(store, chat_id, "pricing.bid_percent")
        if bp is not None:
            try:
                cfg.pricing["ratio"] = float(int(bp) / 100)
            except Exception:
                pass
        # delivery days override?
        # profile
        for field in ("stack", "full_name", "first_name", "role", "years_experience", "highlights", "portfolio_url", "signature"):
            v = get_user(store, chat_id, f"profile.{field}")
            if v is not None:
                try:
                    setattr(cfg.profile, field, v if field not in ("stack","highlights") else list(v))
                except Exception:
                    pass
        # marketplace per-user: override sources
        mname = get_user(store, chat_id, "marketplace.name")
        if mname:
            from .config import SourceConfig
            # replace sources with user's chosen site
            cfg.sources = [SourceConfig(type=str(mname), enabled=True, settings={})]
            # base_url per-user?
            base_url = get_user(store, chat_id, "marketplace.base_url")
            if base_url:
                for src in cfg.sources:
                    if src.type == str(mname):
                        src.settings = {**(src.settings or {}), "base_url": str(base_url)}
        # proposal tone/style per-user? If stored per-user, override
        tone = get_user(store, chat_id, "proposal.tone")
        if tone:
            cfg.proposal.tone = str(tone)
        style = get_user(store, chat_id, "proposal.style_instructions")
        if style is not None:
            cfg.proposal.style_instructions = str(style)
        max_chars = get_user(store, chat_id, "proposal.max_chars")
        if max_chars:
            try:
                cfg.proposal.max_chars = int(max_chars)
            except Exception:
                pass
        # secrets
        if secrets is None:
            secrets = Secrets()
            secrets = secrets_from_store(secrets, store)
        else:
            secrets = secrets_from_store(secrets, store) if hasattr(secrets, "model_copy") else secrets
        # Override with per-user key
        secrets.llm_keys[provider.id] = provider.api_key or ""
        secrets.llm_api_key = provider.api_key
        return cfg, secrets
    return cfg, secrets


def user_status_text(store: SettingsStore, chat_id: str | int) -> str:
    """Per-user dashboard status — no server globals."""
    from .onboarding import bid_percent as _bid_percent_global

    parts = []
    # LLM per-user
    provider = get_user_llm_provider_id(store, chat_id)
    model = get_user_llm_model(store, chat_id)
    if provider and model:
        try:
            from .llm_providers import get_preset
            title = get_preset(provider).title
        except Exception:
            title = provider
        parts.append(f"✅ هوش مصنوعی: {title} — {model}")
    else:
        parts.append("❌ هوش مصنوعی: تنظیم نشده (/llm)")
    # Marketplace per-user
    mname = get_user(store, chat_id, "marketplace.name")
    if mname:
        from .onboarding import site_label
        try:
            label = site_label(str(mname))
        except Exception:
            label = str(mname)
        parts.append(f"✅ سایت فعال: {label}")
    else:
        parts.append("❌ سایت فعال: انتخاب نشده (/market)")
    # Tone per-user
    tone = get_user(store, chat_id, "proposal.tone")
    if tone:
        parts.append(f"✅ لحن: {tone}")
    else:
        parts.append("❌ لحن: ثبت نشده")
    # Profile per-user
    full = get_user(store, chat_id, "profile.full_name")
    if full:
        parts.append(f"✅ پروفایل: {full}")
    else:
        pending = get_user(store, chat_id, "profile.import_pending")
        if pending:
            parts.append("⏳ پروفایل: در حال پردازش…")
        else:
            parts.append("❌ پروفایل: کامل نشده")
    # Pricing per-user
    bp = get_user(store, chat_id, "pricing.bid_percent")
    if bp is not None:
        try:
            parts.append(f"💰 قیمت: {int(bp)}٪ بودجه‌ی کارفرما")
        except Exception:
            pass
    else:
        # check global fallback for pricing? but per-user isolation means show not set
        parts.append("💰 قیمت: پیش‌فرض ۵۰٪")
    # Strategy
    strat = get_user(store, chat_id, "pricing.strategy")
    if strat:
        strat_label = "هوشمند (AI سرچ)" if str(strat) == "ai_search" else "درصدی از بودجه"
        parts.append(f"🧠 استراتژی قیمت: {strat_label}")
    hide = get_user(store, chat_id, "pricing.hide_price_in_text")
    if hide:
        parts.append("🔒 قیمت در متن: مخفی (فقط فرم)")
    return "\n".join(parts)

