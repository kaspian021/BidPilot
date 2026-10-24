"""Free tiers are limited by tokens-per-minute, not by good intentions.

These tests pin the two behaviours that made real Groq usage fail with 429:
the client must *pace itself*, and it must wait as long as the provider says
when a limit is nevertheless hit.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from bidpilot.proposals.llm_client import (
    LLMClient,
    LLMError,
    RateLimitError,
    estimate_tokens,
    reset_token_budgets,
)


@pytest.fixture(autouse=True)
def _clean_budget():
    reset_token_budgets()
    yield
    reset_token_budgets()


@pytest.fixture()
def sleeps(monkeypatch):
    """Record waits and move the client's clock forward instead of really waiting."""
    import bidpilot.proposals.llm_client as llm_client

    recorded: list[float] = []
    clock = {"now": 1_000.0}

    async def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(llm_client, "_now", lambda: clock["now"])
    return recorded


def make_client(handler, **kwargs) -> LLMClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return LLMClient(
        "https://llm.test/v1",
        "key",
        "test-model",
        client=http,
        max_retries=kwargs.pop("max_retries", 2),
        **kwargs,
    )


async def test_429_respects_the_retry_after_header(sleeps):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="slow down", headers={"retry-after": "9"})
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "choices": [{"message": {"content": "سلام"}, "finish_reason": "stop"}],
                "usage": {"total_tokens": 12},
            },
        )

    client = make_client(handler)
    response = await client.complete("sys", "user")

    assert response.text == "سلام"
    # 1.5s/3s retries could never clear a per-minute limit
    assert sleeps and sleeps[0] >= 9
    await client.aclose()


async def test_429_without_a_header_reads_the_body_hint(sleeps):
    seen: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if not seen:
            seen.append(1)
            return httpx.Response(
                429,
                json={"error": {"message": "Rate limit reached ... try again in 7.66s"}},
            )
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = make_client(handler)
    response = await client.complete("sys", "user")

    assert response.text == "ok"
    assert any(7.5 <= waited <= 8.5 for waited in sleeps)
    await client.aclose()


async def test_the_rolling_minute_budget_delays_the_next_call(sleeps):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"total_tokens": 400},
            },
        )

    client = make_client(handler, tokens_per_minute=500)
    await client.complete("x" * 200, "y" * 200, max_tokens=100)
    first_sleeps = len(sleeps)

    await client.complete("x" * 200, "y" * 200, max_tokens=100)

    assert len(sleeps) > first_sleeps, "the second call should wait for the window to free up"
    await client.aclose()


async def test_a_second_client_for_the_same_endpoint_shares_the_budget(sleeps):
    """Discovery and the proposal generator must not each own the minute."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}], "usage": {"total_tokens": 450}},
        )

    first = make_client(handler, tokens_per_minute=500)
    await first.complete("a", "b", max_tokens=50)

    second = make_client(handler, tokens_per_minute=500)
    await second.complete("a", "b", max_tokens=50)

    assert sleeps, "the second client reused the first client's spend"
    await first.aclose()
    await second.aclose()


async def test_finish_reason_is_exposed_so_truncation_is_visible():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "نیمه‌کاره"}, "finish_reason": "length"}]},
        )

    client = make_client(handler)
    response = await client.complete("s", "u", max_tokens=10)

    assert response.truncated is True
    await client.aclose()


async def test_a_non_retryable_error_does_not_loop(sleeps):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, text="bad key")

    client = make_client(handler)
    with pytest.raises(LLMError):
        await client.complete("s", "u")

    assert calls["n"] == 1
    assert not sleeps
    await client.aclose()


def test_token_estimate_is_pessimistic_for_persian():
    persian = "سلام " * 400  # ~2,000 characters
    assert estimate_tokens(persian) >= 800


def test_rate_limit_error_carries_the_hint():
    error = RateLimitError("429", retry_after=12.5)
    assert error.retry_after == 12.5
    assert error.retryable is True
