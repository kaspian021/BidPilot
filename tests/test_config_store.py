from pathlib import Path

from bidpilot.config import (
    Secrets,
    apply_store_to_raw,
    get_secrets,
    load_config,
    reset_secret_cache,
    secrets_from_store,
)
from bidpilot.llm_providers import PRESETS
from bidpilot.settings_store import SettingsStore, reset_store_cache

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "config.yaml"


def make_store(tmp_path) -> SettingsStore:
    return SettingsStore(tmp_path / "data" / "settings.json")


def test_store_overrides_profile_and_style(tmp_path):
    store = make_store(tmp_path)
    store.update(
        {
            "profile.first_name": "سارا",
            "profile.full_name": "سارا محمدی",
            "profile.role": "برنامه‌نویس بک‌اند",
            "profile.stack": ["Python", "FastAPI"],
            "profile.highlights": ["بک‌اند یک فروشگاه‌ساز"],
            "profile.years_experience": 6,
            "proposal.tone": "خودمانی و گرم",
            "proposal.style_instructions": "همیشه یک سؤال شفاف‌کننده بپرس.",
            "proposal.max_chars": 1200,
            "runtime.submit_mode": "live",
        }
    )
    config = load_config(CONFIG, store=store)

    assert config.profile.full_name == "سارا محمدی"
    assert config.profile.stack == ["Python", "FastAPI"]
    assert config.profile.years_experience == 6
    assert config.proposal.tone == "خودمانی و گرم"
    assert config.proposal.style_instructions == "همیشه یک سؤال شفاف‌کننده بپرس."
    assert config.proposal.max_chars == 1200
    assert config.runtime.live is True


def test_store_selects_free_provider_and_marketplace(tmp_path):
    store = make_store(tmp_path)
    store.update(
        {
            "llm.provider": "gemini",
            "llm.model": "gemini-2.0-flash",
            "llm.base_url": PRESETS["gemini"].base_url,
            "marketplace.name": "mock",
            "telegram.chat_ids": ["123", "456"],
        }
    )
    config = load_config(CONFIG, store=store)

    assert config.llm.provider == "gemini"
    assert config.llm.base_url == PRESETS["gemini"].base_url
    assert config.notifications.chat_ids == ["123", "456"]
    assert [src.type for src in config.sources if src.enabled] == ["mock"]
    assert config.source("mock") is not None


def test_store_enables_unknown_configured_source(tmp_path):
    store = make_store(tmp_path)
    store.set("marketplace.name", "another-site")
    raw = apply_store_to_raw({"sources": [{"type": "parscoders", "enabled": True}]}, store)
    types = [(src["type"], src["enabled"]) for src in raw["sources"]]
    assert ("another-site", True) in types
    assert ("parscoders", False) in types


def test_secrets_come_from_store(tmp_path):
    store = make_store(tmp_path)
    store.update(
        {
            "telegram.bot_token": "123456789:AAHabcdefghijklmnopqrstuvwxyz",
            "telegram.chat_ids": ["42"],
            "llm.provider": "groq",
            "llm.api_key": "gsk_test_key",
            "credentials.parscoders.username": "user",
            "credentials.parscoders.password": "pass",
            "credentials.parscoders.cookie": "PHPSESSID=abc",
        }
    )
    secrets = secrets_from_store(Secrets(_env_file=None), store)

    assert secrets.telegram_bot_token.startswith("123456789:")
    assert secrets.chat_ids_list() == ["42"]
    assert secrets.llm_keys["groq"] == "gsk_test_key"
    assert secrets.llm_api_key == "gsk_test_key"
    assert secrets.parscoders_username == "user"
    assert secrets.parscoders_password == "pass"
    assert secrets.parscoders_cookie == "PHPSESSID=abc"


def test_get_secrets_reads_the_store(tmp_path, monkeypatch):
    monkeypatch.setenv("BIDPILOT_SETTINGS_FILE", str(tmp_path / "settings.json"))
    reset_store_cache()
    reset_secret_cache()
    store = SettingsStore(tmp_path / "settings.json")
    store.set("llm.api_key", "from-store")
    try:
        assert get_secrets().llm_api_key == "from-store"
    finally:
        reset_secret_cache()
        reset_store_cache()


def test_without_store_the_yaml_defaults_are_used(tmp_path):
    config = load_config(CONFIG, store=make_store(tmp_path))
    assert config.runtime.submit_mode in ("dry_run", "live")
    assert config.profile.full_name
    assert config.llm.provider in PRESETS


def test_switching_provider_ignores_the_previous_preset_endpoint(tmp_path):
    from bidpilot.llm_providers import PRESETS, resolve_chain

    store = make_store(tmp_path)
    store.update(
        {
            "llm.provider": "gemini",
            # stale Groq values, as if only the provider was switched in a shell
            "llm.base_url": PRESETS["groq"].base_url,
            "llm.model": PRESETS["groq"].model,
        }
    )
    config = load_config(CONFIG, store=store)
    chain = resolve_chain(config.llm, {"gemini": "key"}, legacy_key=None)

    assert chain[0].id == "gemini"
    assert chain[0].base_url == PRESETS["gemini"].base_url
    assert chain[0].model == PRESETS["gemini"].model


def test_env_aliases_for_parscoders_accept_both_spellings(monkeypatch):
    """Both "PARSOCODERS_COOKIE" and "PARSCODERS_COOKIE" must reach the secrets model."""
    from bidpilot.config import Secrets

    monkeypatch.setenv("PARSOCODERS_COOKIE", "PHPSESSID=documented")
    monkeypatch.setenv("PARSOCODERS_USERNAME", "ali")
    monkeypatch.delenv("PARSCODERS_COOKIE", raising=False)
    monkeypatch.delenv("PARSCODERS_USERNAME", raising=False)
    documented = Secrets(_env_file=None)
    assert documented.parscoders_cookie == "PHPSESSID=documented"
    assert documented.parscoders_username == "ali"

    monkeypatch.delenv("PARSOCODERS_COOKIE")
    monkeypatch.delenv("PARSOCODERS_USERNAME")
    monkeypatch.setenv("PARSCODERS_COOKIE", "PHPSESSID=field-name")
    monkeypatch.setenv("PARSCODERS_PASSWORD", "pw")
    typed = Secrets(_env_file=None)
    assert typed.parscoders_cookie == "PHPSESSID=field-name"
    assert typed.parscoders_password == "pw"
