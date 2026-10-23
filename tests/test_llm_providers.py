from bidpilot.config import LLMConfig
from bidpilot.llm_providers import (
    DEFAULT_PROVIDER,
    PRESETS,
    get_preset,
    provider_choices,
    resolve_chain,
)


def test_free_providers_are_available():
    ids = [preset.id for preset in provider_choices()]
    assert DEFAULT_PROVIDER == "groq"
    assert {"groq", "gemini", "openrouter", "mistral", "ollama"} <= set(ids)
    for preset in provider_choices():
        assert preset.base_url.startswith("http")
        assert preset.model


def test_default_llm_config_uses_groq():
    config = LLMConfig()
    assert config.provider == "groq"
    assert config.base_url == PRESETS["groq"].base_url
    assert config.model == PRESETS["groq"].model


def test_explicit_base_url_wins_over_preset():
    config = LLMConfig(provider="groq", base_url="https://llm.test/v1", model="my-model")
    assert config.base_url == "https://llm.test/v1"
    assert config.model == "my-model"


def test_chain_uses_primary_then_configured_fallbacks():
    config = LLMConfig(provider="groq", fallbacks=["gemini", "openrouter"])
    chain = resolve_chain(config, {"gemini": "g-key"}, legacy_key="groq-key")
    assert [provider.id for provider in chain] == ["groq", "gemini"]
    assert chain[0].api_key == "groq-key"
    assert chain[1].api_key == "g-key"
    assert chain[1].base_url == get_preset("gemini").base_url


def test_chain_skips_providers_without_keys():
    config = LLMConfig(provider="groq", fallbacks=["gemini"])
    assert resolve_chain(config, {}, legacy_key=None) == []


def test_local_ollama_needs_no_key():
    config = LLMConfig(provider="ollama", model="qwen2.5:14b")
    chain = resolve_chain(config, {}, legacy_key=None)
    assert len(chain) == 1
    assert chain[0].id == "ollama"
    assert chain[0].api_key is None
    assert chain[0].base_url == "http://localhost:11434/v1"


def test_unknown_provider_with_explicit_endpoint_is_supported():
    config = LLMConfig(
        provider="my-gateway",
        base_url="https://gateway.test/v1",
        model="custom",
        fallbacks=[],
    )
    chain = resolve_chain(config, {"my-gateway": "k"}, legacy_key=None)
    assert chain and chain[0].base_url == "https://gateway.test/v1"
