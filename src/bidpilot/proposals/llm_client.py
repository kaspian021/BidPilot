"""Minimal OpenAI-compatible LLM client (works with OpenAI, OpenRouter, Groq, local vLLM/Ollama).

Free tiers are limited by **tokens per minute**, not just requests: Groq's free
plan allows 8,000 TPM, and a single sloppy prompt can eat the whole minute. So
the client does two things a naive wrapper does not:

* it keeps a rolling one-minute token budget (input + reserved output) and waits
  *before* sending a call that would exceed it, instead of hammering the API and
  collecting 429s;
* when a 429 does happen anyway, it waits exactly as long as the provider asks
  (``retry-after`` / ``x-ratelimit-reset-tokens`` / ``try again in 7.66s``)
  rather than retrying in 1.5 seconds.

It also understands *reasoning* models (Groq's ``gpt-oss-*``, ``qwen3*``…):
they spend output tokens thinking, so a small ``max_tokens`` produces a
``finish_reason=length`` answer whose ``content`` is **empty** — which used to
surface as "the model returned no valid JSON" and made the whole AI search fall
back to CSS selectors. Here that case is detected, retried with more room, and
reported as what it is. Iranian users also hit ``403 Forbidden`` because Groq
blocks the country at the IP level; the client therefore accepts an explicit
proxy, so the key keeps working through the user's VPN.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

#: Persian text is ~2 characters per token; English ~4. Be conservative.
_CHARS_PER_TOKEN = 2.2

_RESET_RE = re.compile(r"(?:(\d+)m)?([\d.]+)s")
_TRY_AGAIN_RE = re.compile(r"try again in ([\d.]+)\s*(ms|s|m)", re.I)


def _now() -> float:
    """Monotonic clock — a module-level seam so tests can travel in time."""
    return time.monotonic()


def estimate_tokens(text: str) -> int:
    """Rough but deliberately pessimistic token count for a prompt."""
    return max(1, int(len(text or "") / _CHARS_PER_TOKEN))


class LLMError(RuntimeError):
    """Provider failure. ``retryable=False`` means "try another provider now"."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class EmptyCompletionError(LLMError):
    """The provider answered, but ``content`` was empty.

    With reasoning models this usually means the token budget was consumed by
    the hidden reasoning pass, so ``finish_reason`` is ``length``.
    """

    def __init__(self, message: str, *, finish_reason: str = "", truncated: bool = False) -> None:
        super().__init__(message, retryable=True)
        self.finish_reason = finish_reason
        self.truncated = truncated


class RateLimitError(LLMError):
    """HTTP 429 — the provider wants us to slow down (and says how long)."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message, retryable=True)
        self.retry_after = retry_after


@dataclass
class LLMResponse:
    text: str
    model: str = ""
    latency_ms: int = 0
    usage: dict = field(default_factory=dict)
    finish_reason: str = ""

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "length"


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Seconds to wait, from whichever hint the provider gave us."""
    header = response.headers.get("retry-after")
    if header:
        try:
            return max(0.0, float(header.strip()))
        except ValueError:
            pass
    reset = response.headers.get("x-ratelimit-reset-tokens") or ""
    match = _RESET_RE.fullmatch(reset.strip()) if reset else None
    if match:
        minutes = float(match.group(1) or 0)
        seconds = float(match.group(2))
        return minutes * 60 + seconds
    try:
        body = response.json()
    except ValueError:
        body = None
    message = ""
    if isinstance(body, dict):
        error = body.get("error") or {}
        message = str(error.get("message") if isinstance(error, dict) else error)
    found = _TRY_AGAIN_RE.search(message)
    if found:
        value = float(found.group(1))
        unit = found.group(2).lower()
        return value / 1000 if unit == "ms" else (value * 60 if unit == "m" else value)
    return None


@dataclass
class _Spend:
    at: float
    tokens: int


#: One rolling minute per (endpoint, model) — *shared* by every client for that
#: endpoint, because the provider counts tokens per organisation, not per client.
#: Discovery and the proposal generator must not each think they own the minute.
_BUDGETS: dict[str, deque[_Spend]] = {}


def reset_token_budgets() -> None:
    """Forget the rolling windows (tests, or after switching the API key)."""
    _BUDGETS.clear()


class LLMClient:
    """Thin, dependency-free chat-completions client with pacing and retries."""

    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        model: str,
        *,
        temperature: float = 0.6,
        max_tokens: int = 900,
        timeout: float = 45.0,
        extra_headers: dict[str, str] | None = None,
        max_retries: int = 2,
        tokens_per_minute: int = 0,
        proxy: str = "",
        reasoning_effort: str = "",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.extra_headers = extra_headers or {}
        self.max_retries = max_retries
        #: 0 = unlimited (local models). Groq free tier = 8,000/min.
        self.tokens_per_minute = max(0, int(tokens_per_minute))
        #: e.g. http://127.0.0.1:10809 — needed where the provider blocks the country
        self.proxy = (proxy or "").strip()
        #: "low"/"medium"/"high" for reasoning models; empty = provider default
        self.reasoning_effort = (reasoning_effort or "").strip().lower()
        self._client = client
        self._budget_key = f"{self.base_url}|{self.model}"
        self._spent = _BUDGETS.setdefault(self._budget_key, deque())

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            kwargs: dict[str, Any] = {"timeout": self.timeout}
            if self.proxy:
                kwargs["proxy"] = self.proxy
            self._client = httpx.AsyncClient(**kwargs)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **self.extra_headers}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    # ------------------------------------------------------------------ budget
    def _window_used(self, now: float) -> int:
        while self._spent and now - self._spent[0].at > 60:
            self._spent.popleft()
        return sum(item.tokens for item in self._spent)

    async def _reserve(self, tokens: int) -> _Spend | None:
        """Wait until ``tokens`` more fit inside the rolling minute."""
        if not self.tokens_per_minute:
            return None
        tokens = min(tokens, self.tokens_per_minute)  # one call can never be bigger
        while True:
            now = _now()
            used = self._window_used(now)
            if used + tokens <= self.tokens_per_minute:
                spend = _Spend(now, tokens)
                self._spent.append(spend)
                return spend
            wait = 60 - (now - self._spent[0].at) + 0.2 if self._spent else 0.5
            wait = max(0.5, min(wait, 65.0))
            log.info(
                "LLM budget: %s/%s tokens used in this minute; waiting %.1fs",
                used,
                self.tokens_per_minute,
                wait,
            )
            await asyncio.sleep(wait)

    def _settle(self, spend: _Spend | None, response: LLMResponse, reserved: int) -> None:
        """Replace our estimate with the provider's own numbers, when given."""
        if spend is None:
            return
        usage = response.usage or {}
        actual = int(usage.get("total_tokens") or 0)
        if not actual:
            actual = int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)
        spend.tokens = actual or reserved

    # ------------------------------------------------------------------- calls
    async def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int | None = None,
        response_format: dict | None = None,
    ) -> LLMResponse:
        wants = int(max_tokens or self.max_tokens)
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": wants,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if response_format:
            payload["response_format"] = response_format
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        client = await self._get_client()
        last_error: Exception | None = None
        started = time.monotonic()
        reserved = estimate_tokens(system) + estimate_tokens(user) + wants
        spend: _Spend | None = None
        attempt = 0

        while True:
            try:
                spend = await self._reserve(reserved)
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers=self._headers(),
                )
                if response.status_code == 429 or response.status_code >= 500:
                    raise RateLimitError(
                        f"LLM HTTP {response.status_code}: {response.text[:200]}",
                        retry_after=_parse_retry_after(response),
                    )
                if response.status_code >= 400:
                    # bad key / unknown model: retrying the same provider is pointless
                    raise LLMError(
                        f"LLM rejected the request (HTTP {response.status_code}): {response.text[:300]}",
                        retryable=False,
                    )

                data = response.json()
                choice = (data.get("choices") or [{}])[0]
                message = choice.get("message") or {}
                text = message.get("content") or ""
                finish = str(choice.get("finish_reason") or "")
                if not text.strip():
                    thoughts = str(message.get("reasoning") or message.get("reasoning_content") or "")
                    if finish == "length" or thoughts:
                        raise EmptyCompletionError(
                            "مدل بودجه‌ی خروجی را با «استدلال» تمام کرد و پاسخ خالی ماند "
                            f"(finish_reason={finish or '—'}, max_tokens={wants})",
                            finish_reason=finish,
                            truncated=finish == "length" or bool(thoughts),
                        )
                    raise EmptyCompletionError("پاسخ مدل خالی بود", finish_reason=finish)
                result = LLMResponse(
                    text=text.strip(),
                    model=data.get("model", self.model),
                    latency_ms=int((time.monotonic() - started) * 1000),
                    usage=data.get("usage", {}) or {},
                    finish_reason=finish,
                )
                self._settle(spend, result, reserved)
                return result
            except (httpx.HTTPError, LLMError, ValueError) as exc:
                last_error = exc
                if isinstance(exc, EmptyCompletionError) and exc.truncated and wants < 3_200:
                    # a reasoning model ate the output budget: give it room and go
                    # again — this is not a network retry, so it has its own counter
                    wants = min(wants * 2, 4_000)
                    payload["max_tokens"] = wants
                    reserved = estimate_tokens(system) + estimate_tokens(user) + wants
                    log.info(
                        "LLM answered empty because it ran out of output tokens; retrying with max_tokens=%s",
                        wants,
                    )
                    continue
                if not getattr(exc, "retryable", True):
                    break
                if attempt < self.max_retries:
                    told = getattr(exc, "retry_after", None)
                    backoff = 4.0 * (3**attempt)
                    sleep_for = max(float(told), backoff) if told else backoff
                    sleep_for = min(sleep_for, 65.0)
                    log.warning("LLM call failed (%s); retrying in %.1fs", exc, sleep_for)
                    await asyncio.sleep(sleep_for)
                    attempt += 1
                    continue
                break

        raise LLMError(f"LLM call failed: {last_error}")


__all__ = [
    "EmptyCompletionError",
    "LLMClient",
    "LLMError",
    "LLMResponse",
    "RateLimitError",
    "estimate_tokens",
    "reset_token_budgets",
]
