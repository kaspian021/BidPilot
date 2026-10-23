"""Minimal OpenAI-compatible LLM client (works with OpenAI, OpenRouter, Groq, local vLLM/Ollama)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


@dataclass
class LLMResponse:
    text: str
    model: str = ""
    latency_ms: int = 0
    usage: dict = field(default_factory=dict)


class LLMClient:
    """Thin, dependency-free chat-completions client with retries."""

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
        self._client = client

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
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

    async def complete(self, system: str, user: str, *, max_tokens: int | None = None) -> LLMResponse:
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": max_tokens or self.max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        client = await self._get_client()
        last_error: Exception | None = None
        started = time.monotonic()

        for attempt in range(self.max_retries + 1):
            try:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers=self._headers(),
                )
                if response.status_code >= 500 or response.status_code == 429:
                    raise LLMError(f"LLM HTTP {response.status_code}: {response.text[:200]}")
                if response.status_code >= 400:
                    raise LLMError(
                        f"LLM rejected the request (HTTP {response.status_code}): {response.text[:300]}"
                    )

                data = response.json()
                text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
                if not text or not text.strip():
                    raise LLMError("LLM returned an empty completion")
                return LLMResponse(
                    text=text.strip(),
                    model=data.get("model", self.model),
                    latency_ms=int((time.monotonic() - started) * 1000),
                    usage=data.get("usage", {}) or {},
                )
            except (httpx.HTTPError, LLMError, ValueError) as exc:
                last_error = exc
                if attempt < self.max_retries:
                    sleep_for = 1.5 * (2**attempt)
                    log.warning("LLM call failed (%s); retrying in %.1fs", exc, sleep_for)
                    import asyncio

                    await asyncio.sleep(sleep_for)

        raise LLMError(f"LLM call failed after {self.max_retries + 1} attempts: {last_error}")
