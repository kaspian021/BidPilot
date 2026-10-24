"""Telegram is blocked in Iran and Groq answers 403 — one proxy, two symptoms.

These tests pin the behaviour the user actually needs:

* the proxy is stored in one place and used by **both** Telegram and the LLM,
* the Iranian marketplace stays direct (a foreign exit node breaks it),
* "it does not connect" becomes a sentence with a next step, not a traceback,
* a bot started without a connection keeps retrying instead of dying.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from bidpilot import network
from bidpilot.bot import BotAPIError, TelegramAPI


class FakeStore:
    def __init__(self, values: dict | None = None) -> None:
        self.values = dict(values or {})

    def get(self, key: str):
        return self.values.get(key)

    def update(self, values: dict) -> None:
        self.values.update({k: v for k, v in values.items() if v is not None})
        for key, value in values.items():
            if value is None:
                self.values.pop(key, None)

    def set(self, key: str, value) -> None:
        self.values[key] = value


# --------------------------------------------------------------------- proxy
def test_proxy_is_read_from_both_keys_and_saved_to_both(tmp_path):
    store = FakeStore({"llm.proxy": "http://127.0.0.1:10809"})
    assert network.proxy_from_store(store) == "http://127.0.0.1:10809"

    saved = network.set_proxy(store, "127.0.0.1:2080")

    assert saved == "http://127.0.0.1:2080"
    assert store.get("network.proxy") == "http://127.0.0.1:2080"
    assert store.get("llm.proxy") == "http://127.0.0.1:2080"


def test_proxy_can_be_switched_off():
    store = FakeStore({"network.proxy": "http://127.0.0.1:10809"})

    assert network.set_proxy(store, "off") == ""
    assert network.proxy_from_store(store) == ""
    assert store.get("llm.proxy") is None


# ------------------------------------------------------------- reachability
class _FakeResponse:
    def __init__(self, status: int, payload: dict) -> None:
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    """Stands in for ``httpx.AsyncClient`` — records the proxies it was built with."""

    instances: list[_FakeClient] = []
    replies: dict[str, _FakeResponse] = {}

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        _FakeClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def get(self, url: str):
        for needle, response in _FakeClient.replies.items():
            if needle in url:
                return response
        raise httpx.ConnectError("All connection attempts failed")


@pytest.fixture()
def fake_http(monkeypatch):
    _FakeClient.instances = []
    _FakeClient.replies = {}
    monkeypatch.setattr(network.httpx, "AsyncClient", _FakeClient)
    return _FakeClient


async def test_telegram_probe_uses_the_proxy(fake_http):
    fake_http.replies = {
        "api.telegram.org": _FakeResponse(200, {"ok": True, "result": {"username": "BidPilotIranbot"}})
    }

    ok, detail = await network.check_telegram("123:abc", proxy="http://127.0.0.1:10809")

    assert ok and "BidPilotIranbot" in detail
    assert fake_http.instances[0].kwargs.get("proxy") == "http://127.0.0.1:10809"


async def test_a_rejected_token_is_not_blamed_on_the_network(fake_http):
    fake_http.replies = {"api.telegram.org": _FakeResponse(401, {"ok": False, "description": "Unauthorized"})}

    ok, detail = await network.check_telegram("bad", proxy="")

    assert ok is False
    assert "توکن" in detail


async def test_offline_diagnosis_points_at_the_vpn(fake_http):
    async def llm_check():
        return False, "۴۰۳ Forbidden — کشور بلاک است"

    reach = await network.diagnose(token="123:abc", llm_check=llm_check, proxy="")

    assert reach.offline is True
    summary = reach.summary()
    assert "VPN" in summary or "پروکسی" in summary
    assert "کلید و توکن" in summary  # explicitly says the credentials are fine


async def test_only_the_llm_failing_is_a_different_message(fake_http):
    fake_http.replies = {"api.telegram.org": _FakeResponse(200, {"ok": True, "result": {"username": "x"}})}

    async def llm_check():
        return False, "۴۰۳"

    reach = await network.diagnose(token="123:abc", llm_check=llm_check, proxy="")
    summary = reach.summary()

    assert reach.offline is False
    assert "فقط هوش مصنوعی" in summary
    assert "/proxy" in summary or "/llm" in summary


async def test_the_marketplace_is_probed_without_the_proxy(fake_http):
    fake_http.replies = {
        "api.telegram.org": _FakeResponse(200, {"ok": True, "result": {}}),
        "parscoders.com": _FakeResponse(200, {}),
    }

    reach = await network.diagnose(
        token="123:abc",
        proxy="http://127.0.0.1:10809",
        check_marketplace=True,
        marketplace_url="https://parscoders.com/",
    )

    marketplace_client = [client for client in fake_http.instances if client.kwargs.get("proxy") is None]
    assert marketplace_client, "the Iranian site must not go through a foreign proxy"
    assert "سایت فریلنسری" in reach.summary()


# ------------------------------------------------------------------ the bot
def test_telegram_client_is_built_with_the_proxy():
    api = TelegramAPI("123:abc", proxy="http://127.0.0.1:10809")

    assert api.proxy == "http://127.0.0.1:10809"


async def test_switching_the_proxy_rebuilds_the_connection():
    api = TelegramAPI("123:abc", proxy="http://127.0.0.1:10809")
    api._client = httpx.AsyncClient()  # a live client that must be dropped

    await api.set_proxy("http://127.0.0.1:2080")

    assert api.proxy == "http://127.0.0.1:2080"
    assert api._client is None


async def test_the_bot_waits_for_telegram_instead_of_dying(monkeypatch):
    from bidpilot.bot import BotService

    class FlakyAPI:
        token = "123:abc"
        proxy = ""
        calls = 0

        async def get_me(self):
            FlakyAPI.calls += 1
            if FlakyAPI.calls < 3:
                raise BotAPIError("اتصال به تلگرام برقرار نشد: All connection attempts failed")
            return {"username": "BidPilotIranbot"}

        async def set_commands(self):
            return None

        async def get_updates(self, offset, timeout=50):
            # a plain error (not BotAPIError) escapes run_forever's retry loop
            raise RuntimeError("stop now")

    service = BotService(FakeStore({"telegram.chat_ids": ["1"]}), FlakyAPI())

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    monkeypatch.setattr(service, "_log", lambda message: None)

    with pytest.raises(RuntimeError):
        await asyncio.wait_for(service.run_forever(), timeout=5)

    assert FlakyAPI.calls == 3, "it must retry getMe instead of exiting on the first failure"


async def test_a_dead_connection_is_reported_as_such(monkeypatch):
    from bidpilot.bot import BotService

    class DeadAPI:
        token = "123:abc"
        proxy = ""

        async def get_me(self):
            raise BotAPIError("All connection attempts failed")

    service = BotService(FakeStore({}), DeadAPI())
    seen: list[str] = []

    async def no_sleep(_seconds):
        return None

    async def fake_check(token, proxy="", **_kwargs):
        return False, "اتصال برقرار نشد (ConnectError)"

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    monkeypatch.setattr(network, "check_telegram", fake_check)
    monkeypatch.setattr("bidpilot.bot.log.warning", lambda fmt, *a: seen.append(fmt % a))

    with pytest.raises(BotAPIError):
        await asyncio.wait_for(service._wait_for_telegram(attempts=2), timeout=5)

    assert any("پروکسی" in message or "VPN" in message for message in seen)
