"""Free LLM providers with *permanent* API tokens.

BidPilot needs one thing from the outside world: a language model. The user
should never have to pay or re-issue a token, so this module ships presets for
providers that offer an ongoing free tier and an OpenAI-compatible endpoint:

* **Groq** — fast open models, free tier, a key that stays valid
  (this is what most people mean by "grok"; it is *Groq*, not xAI's Grok).
* **Google AI Studio (Gemini)** — free tier, OpenAI-compatible endpoint.
* **OpenRouter** — many `:free` models; the ``openrouter/free`` router always
  points at whichever free model is currently available.
* **Mistral** — free (rate-limited) mode with no credit card.
* **Ollama** — fully local, no key, no limits (if you already run it).

Free tiers are rate-limited and model catalogues change over time. The presets
are a starting point: ``/test`` in the bot reports a retired model immediately,
and ``llm.model`` can be changed without touching any code.

The preset only supplies *where* and *which model*; the key is stored by
:mod:`bidpilot.settings_store` and never leaves the machine except to call the
provider itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

# NOTE: ``LLMClient`` is imported lazily inside ``build_client`` on purpose —
# ``bidpilot.proposals.__init__`` imports the generator, which imports this
# module's parent config. A top-level import here would be circular.


@dataclass(frozen=True)
class ProviderPreset:
    id: str
    title: str
    base_url: str
    model: str
    key_url: str = ""
    note: str = ""
    requires_key: bool = True
    #: free-tier tokens-per-minute cap (0 = unknown/unlimited). Used to pace calls
    #: instead of collecting HTTP 429s. Groq's free plan is 8,000 TPM.
    tokens_per_minute: int = 0
    extra_headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.title} — {self.model}"


PRESETS: dict[str, ProviderPreset] = {
    "groq": ProviderPreset(
        id="groq",
        title="Groq (رایگان و همیشگی)",
        base_url="https://api.groq.com/openai/v1",
        # free-tier model since Groq retired the Llama family for free accounts (2026)
        model="openai/gpt-oss-120b",
        key_url="https://console.groq.com/keys",
        # free plan: 30 RPM / 1,000 RPD / 8,000 TPM (the TPM one is the tight one)
        tokens_per_minute=7000,
        note="توکن رایگان و همیشگی؛ سهمیه ۸٬۰۰۰ توکن در دقیقه — برنامه خودش فاصله‌ها را رعایت می‌کند.",
    ),
    "gemini": ProviderPreset(
        id="gemini",
        title="Google Gemini (رایگان)",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        model="gemini-3.8-flash",
        key_url="https://aistudio.google.com/apikey",
        note="کلید از AI Studio؛ سهمیه‌ی رایگان روزانه. اگر مدل بازنشسته شد، llm.model را عوض کن.",
    ),
    "openrouter": ProviderPreset(
        id="openrouter",
        title="OpenRouter (مدل‌های :free)",
        base_url="https://openrouter.ai/api/v1",
        # the auto-router always picks a currently-available free model
        model="openrouter/free",
        key_url="https://openrouter.ai/keys",
        note="با یک کلید همیشگی به مدل‌های رایگان دسترسی دارید (روتری که خودش مدل آزاد را انتخاب می‌کند).",
        extra_headers={"HTTP-Referer": "https://github.com/kaspian021/BidPilot", "X-Title": "BidPilot"},
    ),
    "mistral": ProviderPreset(
        id="mistral",
        title="Mistral (رایگان)",
        base_url="https://api.mistral.ai/v1",
        model="mistral-small-latest",
        key_url="https://console.mistral.ai/api-keys",
        note="سهمیه‌ی آزمایشی رایگان.",
    ),
    "ollama": ProviderPreset(
        id="ollama",
        title="Ollama (کاملاً محلی، بدون کلید)",
        base_url="http://localhost:11434/v1",
        model="qwen2.5:14b",
        key_url="https://ollama.com/download",
        note="اگر روی همان سیستم Ollama دارید: بدون اینترنت، بدون محدودیت، بدون هزینه.",
        requires_key=False,
    ),
}

PROVIDER_ORDER: tuple[str, ...] = ("groq", "gemini", "openrouter", "mistral", "ollama")
DEFAULT_PROVIDER = "groq"
DEFAULT_FALLBACKS: tuple[str, ...] = ("gemini", "openrouter")


@dataclass
class LLMProvider:
    """A resolved provider + credentials, ready to become an LLMClient."""

    id: str
    base_url: str
    model: str
    api_key: str | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)
    note: str = ""
    tokens_per_minute: int = 0

    @property
    def free(self) -> bool:
        return self.id in PRESETS

    @property
    def label(self) -> str:
        return f"{PRESETS[self.id].title}" if self.id in PRESETS else self.id


def get_preset(provider: str | None) -> ProviderPreset:
    key = (provider or DEFAULT_PROVIDER).strip().lower()
    preset = PRESETS.get(key)
    if preset is None:
        raise KeyError(f"ارائه‌دهنده‌ی ناشناخته: {provider!r}. گزینه‌ها: {', '.join(PROVIDER_ORDER)}")
    return preset


def provider_choices() -> list[ProviderPreset]:
    return [PRESETS[key] for key in PROVIDER_ORDER]


def provider_label(provider: str) -> str:
    try:
        return get_preset(provider).title
    except KeyError:
        return provider


def resolve_chain(
    llm_config: Any,
    keys: Mapping[str, str] | None = None,
    legacy_key: str | None = None,
) -> list[LLMProvider]:
    """Build the ordered list of providers to try (primary first, then fallbacks).

    ``llm_config`` is a :class:`bidpilot.config.LLMConfig` (kept as ``Any`` to
    avoid a circular import). ``keys`` maps provider id -> API key; ``legacy_key``
    is the single ``LLM_API_KEY`` value from the environment.
    """
    keys = dict(keys or {})
    primary = (getattr(llm_config, "provider", None) or DEFAULT_PROVIDER).strip().lower()
    requested: list[str] = [primary]
    for fallback in getattr(llm_config, "fallbacks", []) or []:
        name = str(fallback).strip().lower()
        if name and name not in requested:
            requested.append(name)

    chain: list[LLMProvider] = []
    seen: set[tuple[str, str, str | None]] = set()
    for name in requested:
        preset = PRESETS.get(name)
        if preset is None:
            # Unknown provider: only usable if base_url/model were given explicitly.
            base_url = (getattr(llm_config, "base_url", "") or "").strip()
            model = (getattr(llm_config, "model", "") or "").strip()
            if not base_url or not model:
                continue
            provider = LLMProvider(
                id=name,
                base_url=base_url,
                model=model,
                api_key=keys.get(name) or legacy_key,
            )
        else:
            base_url = preset.base_url
            model = preset.model
            if name == primary:
                explicit_base = (getattr(llm_config, "base_url", "") or "").strip()
                explicit_model = (getattr(llm_config, "model", "") or "").strip()
                # The UI/CLI may switch provider without clearing the old endpoint.
                # A value that matches *another* preset is stale, not custom.
                if explicit_base and explicit_base != preset.base_url:
                    owner = next((p for p in PRESETS.values() if p.base_url == explicit_base), None)
                    if owner is not None:
                        explicit_base = ""
                        if explicit_model == owner.model:
                            explicit_model = ""
                base_url = explicit_base or base_url
                model = explicit_model or model
            api_key = keys.get(name) or (legacy_key if name == primary else None)
            if preset.requires_key and not api_key:
                continue
            provider = LLMProvider(
                id=name,
                base_url=base_url,
                model=model,
                api_key=api_key,
                extra_headers=dict(preset.extra_headers),
                note=preset.note,
                tokens_per_minute=preset.tokens_per_minute,
            )

        if name == primary and getattr(llm_config, "extra_headers", None):
            provider.extra_headers = {**provider.extra_headers, **dict(llm_config.extra_headers)}

        signature = (provider.base_url, provider.model, provider.api_key)
        if signature in seen:
            continue
        seen.add(signature)
        chain.append(provider)
    return chain


def build_client(provider: LLMProvider, llm_config: Any):
    from .proposals.llm_client import LLMClient

    # an explicit llm.tokens_per_minute always wins over the preset's default
    budget = int(getattr(llm_config, "tokens_per_minute", 0) or 0) or provider.tokens_per_minute
    return LLMClient(
        base_url=provider.base_url,
        api_key=provider.api_key,
        model=provider.model,
        temperature=float(getattr(llm_config, "temperature", 0.6)),
        max_tokens=int(getattr(llm_config, "max_tokens", 900)),
        timeout=float(getattr(llm_config, "timeout_seconds", 45.0)),
        extra_headers=provider.extra_headers,
        tokens_per_minute=budget,
    )


async def verify_provider(provider: LLMProvider, *, timeout: float = 25.0) -> tuple[bool, str]:
    """Send a tiny request so the wizard can tell the user *now* if a key works."""
    client = httpx.AsyncClient(timeout=timeout)
    try:
        response = await client.post(
            f"{provider.base_url.rstrip('/')}/chat/completions",
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {provider.api_key}"} if provider.api_key else {}),
                **provider.extra_headers,
            },
            json={
                "model": provider.model,
                "messages": [{"role": "user", "content": "فقط بنویس: سلام"}],
                "max_tokens": 64,
                "temperature": 0,
            },
        )
    except httpx.HTTPError as exc:
        return False, f"اتصال برقرار نشد: {exc}"
    finally:
        await client.aclose()

    if response.status_code in (401, 403):
        return False, "کلید رد شد (HTTP 401/403). مطمئن شوید کلید کامل و مربوط به همین سرویس است."
    if response.status_code == 429:
        return False, "کلید معتبر است اما سهمیه‌ی رایگان همین لحظه پر شده؛ کمی بعد امتحان کنید."
    if response.status_code >= 400:
        return False, f"سرویس خطا داد (HTTP {response.status_code}): {response.text[:180]}"
    try:
        data = response.json()
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    except ValueError:
        return False, "پاسخ سرویس قابل خواندن نبود."
    if not text:
        return False, "سرویس پاسخ خالی داد."
    return True, f"کلید سالم است ✅ ({provider.model})"


__all__ = [
    "DEFAULT_FALLBACKS",
    "DEFAULT_PROVIDER",
    "LLMProvider",
    "PRESETS",
    "PROVIDER_ORDER",
    "ProviderPreset",
    "build_client",
    "get_preset",
    "provider_choices",
    "provider_label",
    "resolve_chain",
    "verify_provider",
]
