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

from typing import Any

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
    # per-provider key storage
    provider = get_user_llm_provider_id(store, chat_id) or DEFAULT_PROVIDER
    # try namespaced key first, then per-provider slot
    key = get_user(store, chat_id, "llm.api_key")
    if key:
        return str(key)
    per_provider = get_user(store, chat_id, f"llm.keys.{provider}")
    if per_provider:
        return str(per_provider)
    # fallback: legacy global key (migration path)
    global_key = store.get(f"llm.keys.{provider}") or store.get("llm.api_key")
    if global_key:
        return str(global_key)
    return None


def get_user_llm_model(store: SettingsStore, chat_id: str | int) -> str | None:
    v = get_user(store, chat_id, "llm.model")
    if v:
        return str(v)
    # fallback to preset default for provider
    provider = get_user_llm_provider_id(store, chat_id)
    if provider and provider in PRESETS:
        return PRESETS[provider].model
    # legacy global model
    gm = store.get("llm.model")
    if gm:
        return str(gm)
    return None


def get_user_llm_base_url(store: SettingsStore, chat_id: str | int) -> str | None:
    v = get_user(store, chat_id, "llm.base_url")
    if v:
        return str(v)
    provider = get_user_llm_provider_id(store, chat_id)
    if provider and provider in PRESETS:
        return PRESETS[provider].base_url
    return store.get("llm.base_url")  # global fallback


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
    """Step 2: profile source chosen and (if not skip) fields present."""
    # Check skip flag first (per-user skip marker)
    skipped = get_user(store, chat_id, "onboarding.profile_skipped")
    if skipped:
        return True
    source = get_user(store, chat_id, "profile.source")
    if not source:
        source = store.get("profile.source")
    if not source:
        full_name = get_user(store, chat_id, "profile.full_name") or store.get("profile.full_name")
        if full_name:
            return True
        return False
    s = str(source).strip().lower()
    if s == "skip":
        return True
    imported = get_user(store, chat_id, "profile.imported_at") or store.get("profile.imported_at")
    if imported:
        return True
    full_name = get_user(store, chat_id, "profile.full_name") or store.get("profile.full_name")
    if full_name:
        return True
    # If source is set but not skip and no fields yet, need to complete import/manual
    # For per-user, if source chosen but still pending, not done
    pending = get_user(store, chat_id, "profile.import_pending")
    if pending:
        return False
    return False


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
    "is_llm_onboarding_done",
    "is_profile_onboarding_done",
    "is_onboarding_blocked",
    "onboarding_progress",
]
