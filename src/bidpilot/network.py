"""«آیا اصلاً به اینترنتِ بیرون دسترسی دارم؟» — one place to ask.

Two things are true for a user in Iran and they are easy to confuse:

* **Telegram** (``api.telegram.org``) is blocked at the network level, so the
  bot cannot receive updates or send messages without a VPN/proxy;
* **Groq** answers ``403 Forbidden`` because it blocks the country by IP.

Both look like different errors, and both are fixed the same way — a working
local proxy. So the proxy lives in **one** place (``network.proxy``) and this
module decides who uses it:

* Telegram and the LLM → through the proxy (foreign services),
* the Iranian marketplace (parscoders/ponisha/karlancer) → **directly**, because
  routing it through a foreign exit node is slower and often blocked.

The diagnosis below turns "All connection attempts failed" into a sentence the
user can act on, and the startup check makes a dead network obvious immediately
instead of a traceback five seconds later.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)

#: hosts that must go through the proxy when one is configured
FOREIGN_HOSTS = ("api.telegram.org", "api.groq.com", "generativelanguage.googleapis.com")


def proxy_from_store(store) -> str:
    """The configured proxy: ``network.proxy`` first, then the older ``llm.proxy``."""
    if store is None:
        return ""
    for key in ("network.proxy", "llm.proxy"):
        try:
            value = str(store.get(key) or "").strip()
        except Exception:  # pragma: no cover - broken store
            value = ""
        if value:
            return value
    return ""


def set_proxy(store, value: str) -> str:
    """Store the proxy in both keys so old and new code agree. Returns what was saved."""
    text = (value or "").strip()
    if text.strip().lower() in {"off", "none", "خاموش", "بدون", "-"}:
        text = ""
    if text and "://" not in text:
        text = "http://" + text
    store.update({"network.proxy": text or None, "llm.proxy": text or None})
    try:
        from .config import reset_secret_cache

        reset_secret_cache()
    except Exception:  # pragma: no cover
        pass
    return text


@dataclass
class Reachability:
    telegram: bool | None = None
    llm: bool | None = None
    telegram_detail: str = ""
    llm_detail: str = ""
    proxy: str = ""
    extras: dict[str, str] = field(default_factory=dict)

    @property
    def offline(self) -> bool:
        """Both foreign services unreachable → almost certainly no proxy/VPN."""
        return self.telegram is False and self.llm is False

    def summary(self) -> str:
        """Persian, one paragraph, always with the next step."""
        lines: list[str] = []
        if self.proxy:
            lines.append(f"🔌 پروکسی فعلی: <code>{self.proxy}</code>")
        else:
            lines.append("🔌 پروکسی: تنظیم نشده (مستقیم وصل می‌شوم)")
        if self.telegram is not None:
            lines.append(("✅ " if self.telegram else "❌ ") + "تلگرام: " + (self.telegram_detail or ""))
        if self.llm is not None:
            lines.append(("✅ " if self.llm else "❌ ") + "هوش مصنوعی: " + (self.llm_detail or ""))
        for name, detail in self.extras.items():
            lines.append(f"• {name}: {detail}")
        if self.offline:
            lines.append(
                "\n⚠️ نه تلگرام و نه سرویس هوش مصنوعی در دسترس نیست. در ایران هر دو مسدودند مگر "
                "VPN/پروکسی روشن باشد. برنامه‌ی VPN را باز کن و آدرس پروکسی محلی‌اش را با "
                "«/proxy» بده (مثلاً <code>http://127.0.0.1:10809</code>) — کلید و توکن "
                "هیچ مشکلی ندارند."
            )
        elif self.telegram is False:
            lines.append("\n⚠️ فقط تلگرام در دسترس نیست: اتصال/VPN یا توکن را بررسی کن. /proxy")
        elif self.llm is False:
            lines.append("\n⚠️ فقط هوش مصنوعی مشکل دارد: /proxy یا /llm")
        return "\n".join(lines)


async def _probe(url: str, *, proxy: str = "", timeout: float = 8.0) -> tuple[bool, str]:
    try:
        kwargs: dict = {"timeout": timeout}
        if proxy:
            kwargs["proxy"] = proxy
        async with httpx.AsyncClient(**kwargs) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        return False, f"اتصال برقرار نشد ({type(exc).__name__})"
    return True, f"HTTP {response.status_code}"


async def check_telegram(token: str, *, proxy: str = "", timeout: float = 10.0) -> tuple[bool, str]:
    """Reach ``getMe`` — the cheapest proof that the bot can talk to Telegram."""
    if not token:
        return False, "توکن ثبت نشده"
    try:
        kwargs: dict = {"timeout": timeout}
        if proxy:
            kwargs["proxy"] = proxy
        async with httpx.AsyncClient(**kwargs) as client:
            response = await client.get(f"https://api.telegram.org/bot{token}/getMe")
    except httpx.HTTPError as exc:
        return False, f"اتصال برقرار نشد ({type(exc).__name__})"
    try:
        payload = response.json()
    except ValueError:  # pragma: no cover
        return False, f"پاسخ نامعتبر (HTTP {response.status_code})"
    if payload.get("ok"):
        username = (payload.get("result") or {}).get("username") or "?"
        return True, f"متصل (@{username})"
    description = str(payload.get("description") or f"HTTP {response.status_code}")
    if response.status_code in (401, 404):
        return False, f"توکن رد شد ({description})"
    return False, description


async def diagnose(
    *,
    token: str = "",
    llm_check=None,
    proxy: str = "",
    check_marketplace: bool = False,
    marketplace_url: str = "",
) -> Reachability:
    """Probe Telegram + the LLM (and optionally the site) in parallel."""
    result = Reachability(proxy=proxy)
    tasks: list = [check_telegram(token, proxy=proxy)]
    if llm_check is not None:
        tasks.append(llm_check())
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)

    telegram = outcomes[0]
    if isinstance(telegram, BaseException):  # pragma: no cover - defensive
        result.telegram, result.telegram_detail = False, str(telegram)
    else:
        result.telegram, result.telegram_detail = telegram

    if llm_check is not None:
        llm = outcomes[1]
        if isinstance(llm, BaseException):  # pragma: no cover - defensive
            result.llm, result.llm_detail = False, str(llm)
        else:
            result.llm, result.llm_detail = llm

    if check_marketplace and marketplace_url:
        # deliberately *without* the proxy: the Iranian site must stay direct
        ok, detail = await _probe(marketplace_url)
        result.extras["سایت فریلنسری (مستقیم)"] = ("✅ " if ok else "❌ ") + detail
    return result


__all__ = [
    "FOREIGN_HOSTS",
    "Reachability",
    "check_telegram",
    "diagnose",
    "proxy_from_store",
    "set_proxy",
]
