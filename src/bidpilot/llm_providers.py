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


# ── Provider model catalog (user picks exactly one, persisted per-user) ──
# Each provider exposes 4-6 representative models; the preset's default is
# always first.  These are *known-good* free-tier models as of 2026-10; if a
# model retires, /test will report it and the user can pick another.
PROVIDER_MODELS: dict[str, list[str]] = {
    "groq": [
        "openai/gpt-oss-120b",
        "openai/gpt-oss-20b",
        "qwen/qwen3.6-27b",
        "qwen/qwen3.8-27b",
        "llama-3.3-70b-versatile",
        "llama-3.1-8b-instant",
        "gemma2-9b-it",
    ],
    "gemini": [
        "gemini-3.8-flash",
        "gemini-2.5-flash",
        "gemini-2.0-flash",
        "gemini-2.0-flash-lite",
    ],
    "openrouter": [
        "openrouter/free",
        "google/gemini-flash-1.5",
        "meta-llama/llama-3.3-70b-instruct:free",
        "qwen/qwen-2.5-7b-instruct:free",
        "deepseek/deepseek-r1:free",
    ],
    "mistral": [
        "mistral-small-latest",
        "mistral-large-latest",
        "mistral-nemo",
        "open-mistral-7b",
    ],
    "ollama": [
        "qwen2.5:14b",
        "llama3.2:3b",
        "gemma2:9b",
        "mistral:7b",
    ],
    # Generic OpenAI-compatible (user supplies base_url)
    "openai": [
        "gpt-4o-mini",
        "gpt-4o",
        "gpt-3.5-turbo",
    ],
}


def models_for_provider(provider_id: str) -> list[str]:
    pid = (provider_id or DEFAULT_PROVIDER).strip().lower()
    if pid in PROVIDER_MODELS:
        return list(PROVIDER_MODELS[pid])
    # Unknown provider -> at least its preset default
    preset = PRESETS.get(pid)
    if preset:
        return [preset.model]
    return [preset.model for preset in PRESETS.values()][:1]


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
    # Aliases so user can type \"openai\" / \"grok\"
    "openai": ProviderPreset(
        id="openai",
        title="OpenAI",
        base_url="https://api.openai.com/v1",
        model="gpt-4o-mini",
        key_url="https://platform.openai.com/api-keys",
        note="کلید OpenAI (پولی).",
    ),
    "grok": ProviderPreset(
        id="groq",
        title="Groq (رایگان و همیشگی)",
        base_url="https://api.groq.com/openai/v1",
        model="openai/gpt-oss-120b",
        key_url="https://console.groq.com/keys",
        note="توکن رایگان و همیشگی؛ سهمیه ۸٬۰۰۰ توکن در دقیقه.",
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


#: models that "think" before answering: they need output room and a low effort
REASONING_MARKERS: tuple[str, ...] = ("gpt-oss", "qwen3", "o1", "o3", "o4", "deepseek-r1", "think")


def is_reasoning_model(model: str) -> bool:
    lowered = str(model or "").lower()
    return any(marker in lowered for marker in REASONING_MARKERS)


def build_client(provider: LLMProvider, llm_config: Any):
    from .proposals.llm_client import LLMClient

    # an explicit llm.tokens_per_minute always wins over the preset's default
    budget = int(getattr(llm_config, "tokens_per_minute", 0) or 0) or provider.tokens_per_minute
    effort = str(getattr(llm_config, "reasoning_effort", "") or "").strip().lower()
    if not effort and is_reasoning_model(provider.model):
        effort = "low"  # the decision/extraction prompts are short by design
    return LLMClient(
        base_url=provider.base_url,
        api_key=provider.api_key,
        model=provider.model,
        temperature=float(getattr(llm_config, "temperature", 0.6)),
        max_tokens=int(getattr(llm_config, "max_tokens", 900)),
        timeout=float(getattr(llm_config, "timeout_seconds", 45.0)),
        extra_headers=provider.extra_headers,
        tokens_per_minute=budget,
        proxy=str(getattr(llm_config, "proxy", "") or ""),
        reasoning_effort=effort,
    )


async def verify_provider(
    provider: LLMProvider,
    *,
    timeout: float = 25.0,
    proxy: str = "",
    reasoning_effort: str = "",
) -> tuple[bool, str]:
    """Send a tiny request so the wizard can tell the user *now* if a key works."""
    kwargs: dict[str, Any] = {"timeout": timeout}
    if proxy:
        kwargs["proxy"] = proxy
    client = httpx.AsyncClient(**kwargs)
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
                "max_tokens": 256,
                "temperature": 0,
                **({"reasoning_effort": reasoning_effort} if reasoning_effort else {}),
            },
        )
    except httpx.HTTPError as exc:
        return False, f"اتصال برقرار نشد: {exc}"
    finally:
        await client.aclose()

    if response.status_code == 401:
        return False, "❌ API key معتبر نیست — کلید رد شد (HTTP 401). مطمئن شوید کلید کامل و مربوط به همین سرویس است."
    if response.status_code == 403:
        return (
            False,
            "سرویس ۴۰۳ Forbidden داد — کلید خراب نیست، اتصال شما رد شده "
            "(Groq برای برخی کشورها از جمله ایران این کار را می‌کند). "
            "با VPN/پروکسی تست کن: /proxy",
        )
    if response.status_code == 429:
        return False, "کلید معتبر است اما سهمیه‌ی رایگان همین لحظه پر شده؛ کمی بعد امتحان کنید."
    if response.status_code == 404 or (response.status_code >= 400 and "model_not_found" in response.text.lower()) or (response.status_code >= 400 and "does not exist" in response.text.lower()):
        # Groq deprecated qwen/qwen3-32b on 2026-07-17 / 2026-08-16 — guide user to current catalog
        return False, f"❌ مدل یافت نشد (model_not_found) — این مدل در {provider.id} منسوخ/در دسترس نیست (HTTP {response.status_code}): {response.text[:180]}. لطفاً مدل دیگری انتخاب کن (مثلاً openai/gpt-oss-120b یا qwen/qwen3.6-27b) با /llm"
    if response.status_code >= 400:
        return False, f"❌ API key معتبر نیست — سرویس خطا داد (HTTP {response.status_code}): {response.text[:180]}"
    try:
        data = response.json()
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    except ValueError:
        return False, "پاسخ سرویس قابل خواندن نبود."
    if not text:
        finished = (data.get("choices") or [{}])[0].get("finish_reason")
        return (
            False,
            "سرویس پاسخ خالی داد"
            + (f" (finish_reason={finished} — مدل استدلالی بودجه‌ی خروجی را خورد)" if finished else "")
            + "؛ با /llm مدل دیگری مثل gpt-oss-20b را امتحان کن.",
        )
    return True, f"کلید سالم است ✅ ({provider.model})"


# ── Per-user verification helper (spec: "API key معتبر نیست" report) ──
async def verify_user_key(
    provider_id: str,
    api_key: str,
    model: str | None = None,
    *,
    proxy: str = "",
    timeout: float = 25.0,
) -> tuple[bool, str]:
    """Verify a user's API key for a provider/model; returns (ok, message).

    The message is Persian and safe to show. On failure it always contains
    "API key معتبر نیست" so the bot can block progress and ask to retry.
    """
    preset = get_preset(provider_id)
    chosen_model = (model or preset.model).strip()
    provider = LLMProvider(
        id=preset.id,
        base_url=preset.base_url,
        model=chosen_model,
        api_key=api_key.strip(),
        extra_headers=dict(preset.extra_headers),
        tokens_per_minute=preset.tokens_per_minute,
    )
    ok, msg = await verify_provider(
        provider,
        proxy=proxy,
        timeout=timeout,
        reasoning_effort="low" if is_reasoning_model(chosen_model) else "",
    )
    if not ok:
        # Model not found should not be labelled as API key invalid
        if "مدل یافت نشد" in msg or "model_not_found" in msg.lower() or "does not exist" in msg.lower():
            pass
        elif "API key معتبر نیست" not in msg:
            # Ensure spec phrase is present for blocking logic (key errors)
            msg = f"❌ API key معتبر نیست — {msg}"
    return ok, msg


__all__ = [
    "DEFAULT_FALLBACKS",
    "DEFAULT_PROVIDER",
    "LLMProvider",
    "PRESETS",
    "PROVIDER_ORDER",
    "PROVIDER_MODELS",
    "ProviderPreset",
    "build_client",
    "get_preset",
    "models_for_provider",
    "provider_choices",
    "provider_label",
    "resolve_chain",
    "verify_provider",
    "verify_user_key",
]
