"""Telegram control bot — the whole configuration lives in the chat.

What the user can do here (no ``.env``, no file editing):

``/start``    connect, then guided setup if something is missing
``/setup``    walk through every question again (existing answers are defaults)
``/style``    choose the tone + write free-form instructions for how to pitch
``/profile``  name, experience, stack, portfolio, signature
``/llm``      pick a free AI provider and paste its permanent key
``/market``   choose the marketplace and store the account (password or cookie)
``/status``   what is configured, masked secrets, readiness
``/preview``  generate a real proposal for a real project, without sending
``/mode``     switch between dry-run and live
``/test``     verify the AI key and the Telegram connection
``/help``     this list

The bot is intentionally thin: all state lives in
:class:`bidpilot.settings_store.SettingsStore` and all questions come from
:mod:`bidpilot.onboarding`, so the CLI wizard and the bot always stay in sync.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from html import escape
from typing import Any

import httpx

from .automation import Automation
from .llm_providers import LLMProvider, get_preset, verify_provider
from .notifications.telegram import split_message
from .onboarding import (
    STAGE_TITLES,
    Question,
    QuestionError,
    all_questions,
    apply_answer,
    bid_percent,
    llm_questions,
    mark_preview_ok,
    mark_setup_complete,
    marketplace_questions,
    missing_steps,
    mode_questions,
    parse_answer,
    preview_is_fresh,
    pricing_questions,
    profile_questions,
    readiness,
    save_chat_id,
    site_label,
    sites_overview,
    status_text,
    style_questions,
    switch_site,
    tone_summary,
    user_llm_questions,
    user_pricing_questions,
)
from .per_user import (
    build_user_llm_provider,
    get_user_llm_api_key,
    get_user_llm_model,
    get_user_llm_provider_id,
    is_llm_onboarding_done,
    is_onboarding_blocked,
    is_profile_onboarding_done,
    is_user_llm_verified,
    onboarding_progress,
    set_user,
    set_user_llm_verified,
    update_user,
)
from .settings_store import SettingsStore, get_store
from .sitecheck import check_site

log = logging.getLogger(__name__)


class BotAPIError(RuntimeError):
    pass


class TelegramAPI:
    """Thin Bot API client (no SDK)."""

    def __init__(
        self,
        token: str,
        *,
        client: httpx.AsyncClient | None = None,
        base_url: str = "https://api.telegram.org",
        timeout: float = 70.0,
        proxy: str = "",
    ) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        #: api.telegram.org is blocked in Iran — a local proxy is the only way in
        self.proxy = (proxy or "").strip()
        self._client = client

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            kwargs: dict[str, Any] = {"timeout": self.timeout}
            if self.proxy:
                kwargs["proxy"] = self.proxy
            self._client = httpx.AsyncClient(**kwargs)
        return self._client

    async def set_proxy(self, proxy: str) -> None:
        """Switch the proxy without restarting the bot (closes the old client)."""
        self.proxy = (proxy or "").strip()
        await self.aclose()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _scrub(self, message: str) -> str:
        """The token is part of the request URL — never let it reach a log."""
        return message.replace(self.token, "***") if self.token else message

    async def call(self, method: str, **params: Any) -> Any:
        client = await self._get_client()
        try:
            response = await client.post(f"{self.base_url}/bot{self.token}/{method}", json=params)
        except httpx.HTTPError as exc:  # pragma: no cover - network
            raise BotAPIError(self._scrub(f"اتصال به تلگرام برقرار نشد: {exc}")) from exc
        try:
            payload = response.json()
        except ValueError as exc:  # pragma: no cover
            raise BotAPIError(f"پاسخ نامعتبر تلگرام (HTTP {response.status_code})") from exc
        if response.status_code >= 400 or not payload.get("ok", False):
            description = payload.get("description") or f"HTTP {response.status_code}"
            raise BotAPIError(str(description))
        return payload.get("result")

    # ------------------------------------------------------------- helpers
    async def get_me(self) -> dict[str, Any]:
        return await self.call("getMe") or {}

    async def get_updates(self, offset: int | None = None, timeout: int = 50) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message", "edited_message", "callback_query"],
        }
        if offset is not None:
            params["offset"] = offset
        return await self.call("getUpdates", **params) or []

    async def send_message(
        self,
        chat_id: str | int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str = "HTML",
    ) -> None:
        for chunk in split_message(text, 3_800):
            params: dict[str, Any] = {
                "chat_id": str(chat_id),
                "text": chunk,
                "disable_web_page_preview": True,
            }
            if parse_mode:
                params["parse_mode"] = parse_mode
            if reply_markup:
                params["reply_markup"] = reply_markup
            try:
                await self.call("sendMessage", **params)
            except BotAPIError as exc:
                # A 400 is usually our HTML; a network error is not — retrying the
                # same dead connection just doubles the wait.
                if "اتصال" not in str(exc) and "connect" not in str(exc).lower():
                    log.warning("sendMessage failed (%s); retrying as plain text", exc)
                    params.pop("parse_mode", None)
                    params.pop("reply_markup", None)
                    await self.call("sendMessage", **params)
                else:
                    raise

    async def get_file(self, file_id: str) -> dict[str, Any]:
        return await self.call("getFile", file_id=file_id) or {}

    async def download_file(self, file_path: str, *, limit: int = 20 * 1024 * 1024) -> bytes:
        """Download a document the user sent (Telegram limits bot downloads to 20 MB)."""
        client = await self._get_client()
        url = f"{self.base_url}/file/bot{self.token}/{file_path}"
        try:
            response = await client.get(url)
        except httpx.HTTPError as exc:  # pragma: no cover - network
            raise BotAPIError(self._scrub(f"دانلود فایل ناموفق بود: {exc}")) from exc
        if response.status_code >= 400:
            raise BotAPIError(f"دانلود فایل ناموفق بود (HTTP {response.status_code})")
        if len(response.content) > limit:
            raise BotAPIError("فایل بزرگ‌تر از حد مجاز (۲۰ مگابایت) است.")
        return response.content

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        try:
            await self.call("answerCallbackQuery", callback_query_id=callback_id, text=text[:190])
        except BotAPIError as exc:  # pragma: no cover
            log.debug("answerCallbackQuery failed: %s", exc)

    async def set_commands(self) -> None:
        commands = [
            {"command": "status", "description": "وضعیت و تنظیمات فعلی"},
            {"command": "setup", "description": "راه‌اندازی گام‌به‌گام"},
            {"command": "style", "description": "تغییر لحن و سبک پیشنهاد"},
            {"command": "sites", "description": "سایت‌های فریلنسری و سوییچ"},
            {"command": "market", "description": "حساب سایت فعال (کوکی/رمز)"},
            {"command": "preview", "description": "دیدن یک پیشنهاد نمونه"},
            {"command": "proxy", "description": "پروکسی هوش مصنوعی (برای خطای ۴۰۳)"},
            {"command": "mode", "description": "آزمایشی / ارسال واقعی"},
            {"command": "help", "description": "راهنما"},
        ]
        try:
            await self.call("setMyCommands", commands=commands)
        except BotAPIError as exc:  # pragma: no cover
            log.debug("setMyCommands failed: %s", exc)


@dataclass
class Flow:
    name: str
    questions: list[Question] = field(default_factory=list)
    index: int = 0
    mark_complete: bool = False

    @property
    def current(self) -> Question | None:
        return self.questions[self.index] if self.index < len(self.questions) else None


HELP_TEXT = """<b>BidPilot — راهنمای حرفه‌ای</b>  <i>✦ دستیار هوشمند پیشنهاددهی</i>

<b>شروع سریع (۱ دقیقه)</b>
• /start — منوی اصلی حرفه‌ای (پروفایل، پلن، وضعیت زنده)
• /plans — پلن‌ها + پرداخت با ۴ درگاه سبک (زیبال/آی‌دی‌پی/نکست‌پی/زرین‌پال)
• /invite — باشگاه دعوت: لینک اختصاصی، ارتقای رایگان بدون خرید
• /account — حساب من + سقف‌های ساعتی/روزانه
• /status — داشبورد با گراف مصرف

<b>پرداخت — ۴ درگاه، احراز سبک</b>
• زیبال 💚، آی‌دی‌پی 💙، نکست‌پی 🧡، زرین‌پال 💛 — فقط موبایل+شبا (زیبال فوری)
• بدون کلید هم تست می‌شود (شبیه‌سازی). راهنما: /zarinpal

<b>دعوت دوستان — رایگان ارتقا بگیر 🎁</b>
• /invite → لینک <code>t.me/bot?start=ref_XXXX</code> + دکمه اشتراک
• ۳ دعوت=۷ روز استارتر · ۵=۱۵ روز · ۱۰=۳۰ روز حرفه‌ای · ۲۰=۳۰ روز سازمانی
• هر خرید دوست = +۷ روز همان پلن (نامحدود)

<b>هسته</b>
/setup — راه‌اندازی کامل
/run — 🚀 شروع رصد خودکار
/stop — ⏸ توقف
/style — لحن و درصد قیمت (۵۰٪ = نصف بودجه ثابت — اصلاح‌شده)
/profile, /llm, /market, /sites, /filters — پروفایل، هوش، سایت، فیلتر
/preview — پیش‌نمایش بدون ارسال
/mode, /proxy, /check, /test, /deps — حالت، پروکسی، تست‌ها
/settings, /cancel — تنظیمات، لغو

هر کاربر حساب و سقف جدا دارد: رایگان ۵/ساعت، پولی تا ۸۰/ساعت.
هیچ .env لازم نیست؛ همه امن در data/settings.json ذخیره می‌شود."""


class BotService:
    """Command/flow engine. Transport is injected, which makes it testable."""

    def __init__(
        self,
        store: SettingsStore,
        api: TelegramAPI | Any,
        *,
        config_path: str = "config/config.yaml",
        log_fn: Callable[[str], None] | None = None,
        automation: Automation | None = None,
    ) -> None:
        self.store = store
        self.api = api
        self.config_path = config_path
        self._flows: dict[str, Flow] = {}
        #: the last transport error, surfaced by /status so a silent outage is visible
        self._last_send_error: str = ""
        self._log = log_fn or (lambda message: log.info("%s", message))
        #: background bidder, started from the chat with «شروع فعالیت»
        self.automation = automation or Automation(
            store=store, config_path=config_path, notify=self.notify_owner
        )

    # ------------------------------------------------------------- transport
    async def send(self, chat_id: str | int, text: str, reply_markup: dict | None = None) -> bool:
        """Send one message; a network hiccup must never kill the command.

        A flaky connection used to bubble ``BotAPIError`` out of ``/preview``
        (or any long handler), aborting it half-way and printing a traceback.
        The message is logged instead, and the caller keeps working.
        """
        try:
            await self.api.send_message(chat_id, text, reply_markup=reply_markup)
            return True
        except BotAPIError as exc:
            self._last_send_error = str(exc)
            log.warning("Telegram send failed for %s: %s", chat_id, exc)
            return False
        except Exception as exc:  # pragma: no cover - defensive
            self._last_send_error = str(exc)
            log.warning("Telegram send failed for %s: %s", chat_id, exc)
            return False

    async def notify_owner(self, text: str) -> None:
        """Used by the scheduler/CLI to push a message to the configured chats."""
        for chat_id in self.store.get_list("telegram.chat_ids"):
            try:
                await self.send(str(chat_id), text)
            except Exception as exc:  # pragma: no cover - network
                log.warning("Could not notify %s: %s", chat_id, exc)

    # ------------------------------------------------------------ authorising
    def is_allowed(self, chat_id: str | int) -> bool:
        allowed = [str(c) for c in self.store.get_list("telegram.chat_ids")]
        if not allowed:
            save_chat_id(self.store, chat_id)
            self._log(f"first chat claimed as owner: {chat_id}")
            return True
        return str(chat_id) in allowed

    # --------------------------------------------------------------- polling
    async def run_forever(self, *, poll_timeout: int = 50) -> None:
        me = await self._wait_for_telegram()
        self._log(f"bot @{me.get('username', '?')} is listening")
        await self.api.set_commands()
        offset: int | None = None
        backoff = 3.0
        while True:
            try:
                updates = await self.api.get_updates(offset, timeout=poll_timeout)
                backoff = 3.0
            except BotAPIError as exc:  # pragma: no cover - network
                self._log(f"getUpdates failed: {exc}; retrying in {backoff:.0f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            for update in updates:
                offset = int(update.get("update_id", 0)) + 1
                try:
                    await self.handle_update(update)
                except Exception:  # pragma: no cover - never kill the bot
                    log.exception("Failed to handle update")

    async def _wait_for_telegram(self, *, attempts: int = 0) -> dict[str, Any]:
        """Retry ``getMe`` with backoff — a VPN that is still connecting is not fatal.

        The old behaviour raised on the first failure and the CLI exited with a
        traceback, which is a terrible first impression for «اتصال برقرار نشد».
        """
        from .network import check_telegram

        delay = 5.0
        tries = 0
        while True:
            try:
                return await self.api.get_me()
            except BotAPIError as exc:
                tries += 1
                ok, detail = await check_telegram(self.api.token, proxy=self.api.proxy)
                hint = ""
                if not ok and "توکن" in detail:
                    hint = (
                        "\n🔑 توکن رد شد. توکن را از @BotFather بگیر و در settings بگذار "
                        "(bidpilot settings set telegram.bot_token -v <TOKEN>)."
                    )
                else:
                    hint = (
                        "\n🔌 اتصال به api.telegram.org برقرار نشد. در ایران تلگرام بدون "
                        "VPN/پروکسی در دسترس نیست. VPN را روشن کن و آدرس پروکسی محلی را با "
                        "/proxy بده (یا: bidpilot settings set network.proxy http://127.0.0.1:10809)."
                    )
                self._last_send_error = str(exc)
                # WARNING (not INFO) on purpose: a dead connection must be visible
                # in the console of a user who never reads logs.
                log.warning("getMe failed (%s): %s%s", tries, exc, hint)
                if attempts and tries >= attempts:
                    raise
                await asyncio.sleep(delay)
                delay = min(delay * 1.6, 60.0)

    # --------------------------------------------------------------- updates
    async def handle_update(self, update: dict[str, Any]) -> None:
        if callback := update.get("callback_query"):
            await self._handle_callback(callback)
            return
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return
        # ── maintenance mode (admin only) ──
        try:
            if self.store.get_bool("admin.maintenance"):
                from .admin import is_admin as _is_admin
                if not _is_admin(chat_id, self.store):
                    await self.send(chat_id, "🚧 ربات در حال نگهداری است — بزودی برمی‌گردیم.\n<i>ادمین: /admin → خاموش کردن نگهداری</i>")
                    return
        except Exception:
            pass
        if message.get("document"):
            if not self.is_allowed(chat_id):
                try:
                    save_chat_id(self.store, str(chat_id))
                except Exception:
                    return
            await self._handle_document(message, str(chat_id))
            return
        text = (message.get("text") or "").strip()
        if not text:
            return
        if not self.is_allowed(chat_id):
            # Referral / public onboarding: let anyone join via /start (esp. /start ref_...)
            # Otherwise the invite link is useless. Auto-add them to allowed list.
            lower = text.lower()
            is_public_entry = lower.startswith("/start") or lower.startswith("/help") or lower.startswith("/plans")
            if is_public_entry:
                try:
                    save_chat_id(self.store, str(chat_id))
                    self._log(f"new user auto-added (public entry): {chat_id} via {text[:30]}")
                except Exception:
                    pass
            else:
                await self.send(
                    chat_id,
                    "⛔ این چت مجاز نیست.\n"
                    f"chat_id تو: <code>{escape(str(chat_id))}</code>\n"
                    "برای اجازه‌دادن، مالک باید این شناسه را در تنظیمات اضافه کند.\n"
                    "<i>نکته: برای دعوت دوستان، از <code>/start</code> با لینک دعوت استفاده کن.</i>",
                )
                return
        # ── Onboarding gate: no dashboard/op before LLM+Profile verified ──
        try:
            chat_str = str(chat_id)
            in_flow = chat_str in self._flows and self._flows[chat_str].current is not None
            if not in_flow and is_onboarding_blocked(self.store, chat_str):
                low = text.lower()
                # Only block operational commands; allow informational ones to pass through
                blocked_in_update = ("/preview", "/run", "/filters", "/mode", "/live", "/scheduler", "/autopilot", "/market", "/status")
                if any(low.startswith(b) for b in blocked_in_update):
                    await self.send(chat_str, "⚠️ اول باید مراحل ۱ و ۲ را کامل کنی — کلید هوش مصنوعی و پروفایل.\n/llm را بزن تا کلیدت را ثبت و تأیید کنی.")
                    await self._enforce_onboarding(chat_str)
                    return
                if low.startswith("/cancel"):
                    await self.send(chat_str, "⚠️ برای ادامه باید ابتدا مرحله‌ی ۱ و ۲ را کامل کنی. لغو انجام شد ولی دوباره از همین‌جا شروع می‌کنیم.")
                    await self._enforce_onboarding(chat_str)
                    return
                if low.startswith("/start") or low.startswith("/llm"):
                    await self._enforce_onboarding(chat_str)
                    return
                # For other commands like /status,/help,/deps let them through (don't enforce)
                if low.startswith("/"):
                    pass
                else:
                    # Plain text outside flow while blocked — nudge to onboarding
                    # but don't block if it's not operational
                    pass

        except Exception:
            pass
        if text.startswith("/"):
            command, _, rest = text.partition(" ")
            await self.handle_command(str(chat_id), command.lower(), rest.strip())
            return
        await self.handle_text(str(chat_id), text)

    async def _handle_callback(self, callback: dict[str, Any]) -> None:
        data = str(callback.get("data") or "")
        message = callback.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        callback_id = str(callback.get("id") or "")
        if chat_id is None:
            return
        await self.api.answer_callback(callback_id)
        if not self.is_allowed(chat_id):
            # Allow callbacks from newly auto-added users (e.g. after /start ref_...)
            try:
                save_chat_id(self.store, str(chat_id))
            except Exception:
                return
        chat = str(chat_id)

        # Admin panel — exclusive to owner, intercepted before other handlers
        if data.startswith("admin:"):
            await self._handle_admin_callback(chat, data)
            return

        if data.startswith("ch:"):
            _, index, choice_index = (data.split(":") + ["", "", ""])[:3]
            flow = self._flows.get(chat)
            if not flow or str(flow.index) != index:
                await self.send(chat_id, "این گزینه مربوط به پرسش فعلی نیست. /cancel و دوباره تلاش کن.")
                return
            question = flow.current
            if question is None:
                return
            choices = question.choice_list(self.store)
            try:
                position = int(choice_index)
                value = choices[position].value
            except (ValueError, IndexError):
                await self.send(chat_id, "گزینه نامعتبر است.")
                return
            await self._consume_answer(chat, value)
            return

        if data.startswith("site:"):
            await self._cmd_site(chat, data.split(":", 1)[1])
            return

        if data == "run:ask":
            await self._cmd_run(chat)
            return
        if data == "run:confirm":
            await self._start_work(chat)
            return
        if data == "run:stop":
            await self._cmd_stop(chat)
            return
        if data == "cmd:profile":
            await self.handle_command(chat, "/profile", "")
            return
        if data.startswith("dep:"):
            await self._install_dep(chat, data.split(":", 1)[1])
            return
        if data == "llm:proxy":
            from .onboarding import proxy_questions

            await self._start_flow(chat, "proxy", proxy_questions(self.store))
            return
        if data == "llm:test":
            await self._cmd_test(chat)
            return
        if data == "cmd:llm":
            await self.handle_command(chat, "/llm", "")
            return
        if data == "profile:retry":
            await self._retry_profile_import(chat)
            return
        if data == "profile:restart":
            from .profile_import import clear_pending_import

            clear_pending_import(self.store)
            await self._start_flow(chat, "profile", profile_questions(self.store))
            return
        if data == "cmd:check":
            await self._cmd_check(chat)
            return
        if data == "cmd:run":
            await self._cmd_run(chat)
            return
        if data == "cmd:mode":
            await self.handle_command(chat, "/mode", "")
            return

        if data == "cmd:status":
            await self.handle_command(chat, "/status", "")
        elif data == "cmd:setup":
            await self.handle_command(chat, "/setup", "")
        elif data == "cmd:style":
            await self.handle_command(chat, "/style", "")
        elif data == "cmd:preview":
            await self.handle_command(chat, "/preview", "")
        elif data == "cmd:filters":
            await self.handle_command(chat, "/filters", "")
        elif data == "cmd:sites":
            await self.handle_command(chat, "/sites", "")
        elif data == "cmd:market":
            await self.handle_command(chat, "/market", "")
        elif data == "cmd:plans":
            await self.handle_command(chat, "/plans", "")
        elif data == "cmd:home":
            await self.handle_command(chat, "/start", "")
        elif data.startswith("plan:"):
            await self._cmd_plan_detail(chat, data.split(":", 1)[1])
        elif data.startswith("pay_pick:"):
            await self._cmd_pick_gateway(chat, data.split(":", 1)[1])
        elif data.startswith("buy:"):
            parts = data.split(":", 2)
            if len(parts) == 3:
                await self._cmd_buy(chat, parts[1], gateway=parts[2])
            else:
                await self._cmd_buy(chat, parts[1])
        elif data.startswith("verify:"):
            await self._cmd_verify(chat, data.split(":", 1)[1])
        elif data.startswith("ref:copy:"):
            await self._cmd_ref_copy(chat, data.split(":", 2)[1])
        elif data == "cmd:invite":
            await self._cmd_invite(chat)
        elif data == "zarinpal:guide":
            await self._cmd_zarinpal_guide(chat)
        elif data == "payments:history":
            await self._cmd_payments(chat)
        elif data == "plans:compare":
            from .bot_ui import compare_table_text
            await self.send(chat_id, compare_table_text(), {"inline_keyboard": [[{"text": "↩️ پلن‌ها", "callback_data": "cmd:plans"}]]})
        elif data.startswith("flt:"):
            await self._toggle_filter(chat, data.split(":", 1)[1])
        elif data == "cmd:help":
            await self.send(chat_id, HELP_TEXT)
        elif data == "cmd:cancel":
            await self.handle_command(chat, "/cancel", "")

    # -------------------------------------------------------------- commands
    async def handle_command(self, chat_id: str, command: str, rest: str = "") -> None:
        # ── Gate: block all ops until LLM verified (Step 1) ──
        try:
            if is_onboarding_blocked(self.store, chat_id):
                # Operational commands that must be blocked until onboarding done;
                # informational/help commands remain allowed so tests and user can still navigate
                blocked_ops = {"/preview", "/run", "/filters", "/mode", "/live", "/scheduler", "/autopilot", "/market", "/sites", "/credentials", "/setup", "/status"}
                if command.lower() in blocked_ops:

                    await self.send(chat_id, "⚠️ اول باید مراحل ۱ و ۲ را کامل کنی — کلید هوش مصنوعی و پروفایل.\n/llm را بزن تا کلیدت را ثبت و تأیید کنی.")
                    await self._enforce_onboarding(chat_id)
                    return
                if command.lower() == "/cancel":
                    # Respect block: don't let cancel escape onboarding
                    if chat_id in self._flows:
                        self._flows.pop(chat_id, None)
                        await self.send(chat_id, "لغو شد — ولی برای ادامه باید مراحل را کامل کنی.")
                    await self._enforce_onboarding(chat_id)
                    return
        except Exception:
            pass
        if command in {"/start", "/menu"}:
            await self._cmd_start(chat_id, rest)
        elif command == "/help":
            await self.send(chat_id, HELP_TEXT)
        elif command == "/setup":
            await self._start_flow(chat_id, "setup", all_questions(self.store), mark_complete=True)
        elif command in {"/style", "/tone"}:
            # tone + writing instructions + the share of the budget we bid
            await self._start_flow(
                chat_id, "style", [*style_questions(self.store), *pricing_questions(self.store)]
            )
        elif command == "/profile":
            if is_llm_onboarding_done(self.store, chat_id):
                await self._start_profile_onboarding(chat_id)
            else:
                await self.send(chat_id, "⚠️ اول باید کلید هوش مصنوعی را تأیید کنی: /llm")
                await self._start_llm_onboarding(chat_id)
            return
        elif command == "/llm":
            # Per-user LLM onboarding (isolated)
            await self._start_llm_onboarding(chat_id)
            return
        elif command == "/proxy":
            from .onboarding import proxy_questions

            await self._start_flow(chat_id, "proxy", proxy_questions(self.store))
        elif command == "/market":
            await self._start_flow(chat_id, "market", marketplace_questions(self.store))
        elif command == "/sites":
            await self._cmd_sites(chat_id)
        elif command == "/site":
            await self._cmd_site(chat_id, rest)
        elif command == "/mode":
            await self._start_flow(chat_id, "mode", mode_questions(self.store))
        elif command == "/status":
            await self._cmd_status(chat_id)
        elif command == "/preview":
            await self._cmd_preview(chat_id)
        elif command == "/filters":
            await self._cmd_filters(chat_id)
        elif command == "/deps":
            await self._cmd_deps(chat_id)
        elif command == "/test":
            await self._cmd_test(chat_id)
        elif command == "/run":
            await self._cmd_run(chat_id)
        elif command == "/stop":
            await self._cmd_stop(chat_id)
        elif command == "/check":
            await self._cmd_check(chat_id)
        elif command == "/settings":
            await self._cmd_settings(chat_id)
        elif command == "/skip":
            flow = self._flows.get(chat_id)
            if flow is not None and flow.current is not None:
                await self._consume_answer(chat_id, "/skip")
            else:
                await self.send(chat_id, "الان پرسشی در جریان نیست.")
        elif command in {"/invite", "/referral", "/دعوت", "/باشگاه", "/affiliate"}:
            await self._cmd_invite(chat_id)
        elif command in {"/plans", "/plan", "/subscription", "/اشتراک"}:
            await self._cmd_plans(chat_id)
        elif command in {"/account", "/اکانت", "/myplan"}:
            await self._cmd_account(chat_id)
        elif command == "/zarinpal":
            await self._cmd_zarinpal_guide(chat_id)
        elif command.startswith("/buy"):
            _parts = (rest or "starter").strip().split()
            _plan = _parts[0] if _parts else "starter"
            _gw = _parts[1] if len(_parts) > 1 else None
            if _gw:
                await self._cmd_buy(chat_id, _plan, gateway=_gw)
            else:
                await self._cmd_buy(chat_id, _plan)
        elif command in {"/admin", "/panel", "/پنل"}:
            if not self._require_admin(chat_id):
                await self.send(chat_id, "⛔ فقط ادمین (7806301378) دسترسی دارد.")
                return
            stats = await self._admin_stats()
            from .admin import admin_main_text, admin_main_keyboard
            await self.send(chat_id, admin_main_text(stats), admin_main_keyboard())
        elif command.startswith("/admin_set"):
            if not self._require_admin(chat_id):
                await self.send(chat_id, "⛔ فقط ادمین.")
                return
            await self._handle_admin_set(chat_id, rest)
        elif command.startswith("/admin_search"):
            if not self._require_admin(chat_id):
                await self.send(chat_id, "⛔ فقط ادمین.")
                return
            await self._handle_admin_search(chat_id, rest)
        elif command.startswith("/admin_user"):
            if not self._require_admin(chat_id):
                await self.send(chat_id, "⛔ فقط ادمین.")
                return
            target = (rest or "").strip().split()[0] if rest else ""
            if not target:
                await self.send(chat_id, "مثال: <code>/admin_user 8697160216</code>")
                return
            await self._handle_admin_callback(chat_id, f"admin:user:{target}")
        elif command.startswith("/broadcast"):
            if not self._require_admin(chat_id):
                await self.send(chat_id, "⛔ فقط ادمین.")
                return
            await self._handle_broadcast(chat_id, rest)
        elif command == "/cancel":
            if self._flows.pop(chat_id, None):
                await self.send(chat_id, "لغو شد. هر وقت خواستی /setup یا /help.")
            else:
                await self.send(chat_id, "چیزی در جریان نیست. /help")
        else:
            await self.send(chat_id, "این دستور را نمی‌شناسم.\n\n" + HELP_TEXT)

    # ------------------------------------------------------- documents/profile
    async def _handle_document(self, message: dict[str, Any], chat_id: str) -> None:
        """A PDF resume the user sent while the profile flow waits for a file."""
        flow = self._flows.get(chat_id)
        question = flow.current if flow else None
        document = message.get("document") or {}
        name = str(document.get("file_name") or "resume.pdf")
        mime = str(document.get("mime_type") or "")
        size = int(document.get("file_size") or 0)

        if question is None or question.kind != "file":
            await self.send(
                chat_id,
                "فایل گرفتم، ولی الان جایی برای آن باز نیست.\nبرای ساختن پروفایل از رزومه: /profile",
            )
            return
        if not (mime == "application/pdf" or name.lower().endswith(".pdf")):
            await self.send(chat_id, "⚠️ فقط فایل PDF پذیرفته می‌شود (نه عکس، نه Word).")
            return
        if size and size > 20 * 1024 * 1024:
            await self.send(chat_id, "⚠️ فایل بزرگ‌تر از ۲۰ مگابایت است. نسخه‌ی کوچک‌تر بفرست.")
            return

        file_id = str(document.get("file_id") or "")
        await self.send(chat_id, "📄 فایل رسید؛ دارم می‌خوانم و تحلیل می‌کنم…")
        try:
            info = await self.api.get_file(file_id)
            remote_path = str(info.get("file_path") or "")
            data = await self.api.download_file(remote_path)
        except Exception as exc:
            await self.send(
                chat_id,
                f"⚠️ فایل دانلود نشد: {escape(str(exc))}\n"
                "می‌توانی لینک گیت‌هاب/پورتفولیو بدهی (/profile) یا فایل را دوباره بفرستی.",
            )
            return

        target = self._uploads_dir() / name
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        except OSError as exc:
            await self.send(chat_id, f"❌ ذخیره‌ی فایل ناموفق بود: {escape(str(exc))}")
            return

        await self._consume_answer(chat_id, str(target))

    async def _ensure_pdf_reader(self, chat_id: str) -> bool:
        """Install ``pypdf`` on the spot instead of telling the user "pip install"."""
        from .deps import ensure, has, install_command

        if has("pypdf"):
            return True
        await self.send(
            chat_id,
            "📦 خواندن PDF به بسته‌ی کوچک <code>pypdf</code> نیاز دارد و در این نسخه نصب نیست.\n"
            "همین حالا خودم نصبش می‌کنم (چند ثانیه)…",
        )
        ok, _detail = await asyncio.to_thread(ensure, "pypdf")
        if ok:
            await self.send(chat_id, "✅ نصب شد؛ حالا فایل را می‌خوانم…")
            return True
        self._note_import_error("pypdf نصب نیست")
        await self.send(
            chat_id,
            "❌ نصب خودکار انجام نشد (اینترنت یا دسترسی نوشتن محیط مجازی).\n\n"
            "این دستور را در همان پوشه‌ی BidPilot اجرا کن و بعد فایل را دوباره بفرست:\n"
            f"<code>{escape(install_command('pypdf'))}</code>\n\n"
            "راه‌های جایگزین بدون نصب: /profile → «لینک گیت‌هاب»، «لینک پورتفولیو» یا «ورود دستی».",
        )
        return False

    def _note_import_error(self, message: str, chat_id: str | int | None = None) -> None:
        try:
            if chat_id is not None:
                try:
                    from .per_user import set_user
                    set_user(self.store, str(chat_id), "profile.import_error", (message or "خوانده نشد")[:300])
                    return
                except Exception:
                    pass
            self.store.set("profile.import_error", (message or "خوانده نشد")[:300])
        except Exception:
            log.warning("Could not store the profile import error")

    def _uploads_dir(self):
        from pathlib import Path

        base = getattr(self.store, "path", None)
        root = Path(base).parent if base else Path("data")
        return root / "uploads"

    async def _import_profile_from_pending(self, chat_id: str) -> None:
        """Run the queued resume/URL import and tell the user exactly what was extracted."""
        from .profile_import import (
            ProfileImportError,
            pending_import,
            run_pending_import,
            summary_text,
        )

        pending = pending_import(self.store, chat_id=chat_id)
        if pending is None:
            pending = pending_import(self.store)
        if not pending:
            return
        if pending["type"] == "file" and not await self._ensure_pdf_reader(chat_id):
            return
        await self.send(chat_id, "🤖 دارم متن را با هوش مصنوعی شخصی تو تحلیل می‌کنم…")
        try:
            result = await run_pending_import(self.store, save_dir=self._uploads_dir(), chat_id=chat_id)
            if result is None:
                # fallback global
                result = await run_pending_import(self.store, save_dir=self._uploads_dir())
        except ProfileImportError as exc:
            self._note_import_error(str(exc), chat_id)
            await self.send(
                chat_id,
                f"⚠️ {escape(str(exc))}\n\n"
                "اطلاعات قبلی پروفایل حفظ شد (چیزی پاک نشد).\n"
                "می‌توانی دوباره با /profile فایل/لینک بدهی، یا «ورود دستی» را انتخاب کنی.",
            )
            return
        except Exception as exc:
            log.exception("Profile import failed")
            self._note_import_error(f"تحلیل انجام نشد: {exc}", chat_id)
            await self.send(
                chat_id,
                f"⚠️ تحلیل پروفایل انجام نشد: {escape(str(exc))}\n"
                "اطلاعات فعلی حفظ شد؛ با /profile دوباره تلاش کن یا دستی وارد کن.",
            )
            return

        if result is None:
            return
        from .profile_import import clear_import_error

        clear_import_error(self.store, chat_id=chat_id)
        clear_import_error(self.store)
        body = summary_text(result.fields)
        footer = (
            "🤖 استخراج با هوش مصنوعی انجام شد."
            if result.used_llm
            else "ℹ️ استخراج ساده (بدون هوش مصنوعی) انجام شد."
        )
        warnings = "\n".join(f"⚠️ {escape(w)}" for w in result.warnings)
        await self.send(
            chat_id,
            body
            + "\n\n"
            + footer
            + (("\n" + warnings) if warnings else "")
            + "\n\nاگر چیزی درست نیست، /profile را بزن و دستی اصلاح کن.",
        )

    # ------------------------------------------------------------ start/stop
    async def _cmd_run(self, chat_id: str) -> None:
        """«شروع فعالیت» — confirm what will happen, then start the bidder."""
        state = readiness(self.store)
        missing = missing_steps(self.store, include_recommended=False)
        if not state["credentials"]:
            await self.send(chat_id, "🔑 اول حساب سایت را وصل کن: /market")
            return
        if missing:
            await self.send(
                chat_id,
                "⚠️ هنوز این‌ها ناقص است:\n" + "\n".join(f"• {escape(m)}" for m in missing) + "\n\n/setup",
            )
            return
        if self.automation.running:
            await self.send(chat_id, self.automation.status_text(), self._run_keyboard())
            return

        live = state["mode"] == "live"
        if live and not preview_is_fresh(self.store):
            await self.send(chat_id, self._need_test_text(), self._need_test_keyboard())
            return
        mode_line = (
            "🔴 <b>واقعی</b> — پیشنهادها روی سایت ثبت می‌شوند."
            if live
            else "🧪 <b>آزمایشی</b> — پیشنهادها ساخته می‌شوند ولی ارسال نمی‌شوند."
        )
        limits = self._limits()
        await self.send(
            chat_id,
            "<b>آماده‌ی شروع</b>\n\n"
            f"🌐 سایت: {escape(site_label(state['marketplace']))}\n"
            f"💰 قیمت: {bid_percent(self.store)}٪ بودجه‌ی کارفرما\n"
            f"🎭 لحن: {escape(str(state['tone'] or 'پیش‌فرض'))}\n"
            f"⏱ فاصله‌ی بررسی: هر {limits['interval']} ثانیه\n"
            f"📊 سقف: {limits['per_day']} پیشنهاد در روز، {limits['per_hour']} در ساعت\n\n"
            f"{mode_line}\n\n"
            "با «تأیید و شروع» رصد پروژه‌ها و ارسال پیشنهاد آغاز می‌شود.",
            {
                "inline_keyboard": [
                    [{"text": "🚀 تأیید و شروع", "callback_data": "run:confirm"}],
                    [
                        {"text": "🧪 تغییر به آزمایشی", "callback_data": "cmd:mode"}
                        if live
                        else {"text": "🔴 حالت واقعی", "callback_data": "cmd:mode"}
                    ],
                    [{"text": "لغو", "callback_data": "cmd:cancel"}],
                ]
            },
        )

    def _limits(self) -> dict[str, Any]:
        try:
            from .config import load_config

            cfg = load_config(self.config_path, store=self.store)
            return {
                "interval": int(cfg.runtime.poll_interval_seconds or 120),
                "per_day": int(cfg.runtime.max_bids_per_day or 0),
                "per_hour": int(cfg.runtime.max_bids_per_hour or 0),
            }
        except Exception:  # pragma: no cover - broken config
            return {"interval": 120, "per_day": 0, "per_hour": 0}

    def _need_test_text(self) -> str:
        return (
            "<b>اول یک آزمایش بی‌خطر لازم است</b>\n\n"
            "حالت فعلی 🔴 <b>واقعی</b> است؛ یعنی هر پیشنهاد روی سایت ثبت می‌شود. "
            "تا وقتی یک پیش‌نمایش موفق یا یک دور 🧪 آزمایشی نبینم، ارسال واقعی را باز نمی‌کنم — "
            "این‌طور پیشنهادِ ناقص یا ناموفق روی حساب تو ثبت نمی‌شود.\n\n"
            "یکی از این دو را انتخاب کن:\n"
            "• 🧪 <b>پیش‌نمایش</b> — همین حالا یک پروژه گرفته می‌شود و پیشنهادش را می‌بینی (چیزی ارسال نمی‌شود).\n"
            "• 🔁 <b>حالت آزمایشی</b> — یک دور کامل بدون ارسال اجرا می‌شود.\n\n"
            "بعد از آن /run در حالت واقعی باز است."
        )

    @staticmethod
    def _need_test_keyboard() -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [{"text": "🧪 پیش‌نمایش همین حالا", "callback_data": "cmd:preview"}],
                [{"text": "🔁 اجرای یک دور آزمایشی", "callback_data": "cmd:mode"}],
                [{"text": "📊 وضعیت", "callback_data": "cmd:status"}],
            ]
        }

    async def _start_work(self, chat_id: str) -> None:
        if self.automation.running:
            await self.send(chat_id, self.automation.status_text(), self._run_keyboard())
            return
        if self.store.get("runtime.submit_mode") == "live" and not preview_is_fresh(self.store):
            await self.send(chat_id, self._need_test_text(), self._need_test_keyboard())
            return
        await self.send(chat_id, "🔐 یک‌بار ورود به حساب را بررسی می‌کنم…")
        check = await check_site(self.store, config_path=self.config_path)
        if not check.ok:
            await self.send(
                chat_id,
                check.message()
                + "\n\n⛔ <b>ورود به حساب تأیید نشد؛ جست‌وجو و ارسال پیشنهاد شروع نمی‌شود.</b>\n"
                "از /market کوکی/ورود را به‌روز کن، با /test دوباره بررسی کن و سپس /run را بزن.",
            )
            return
        await self.send(chat_id, check.message())
        # Per-user isolation: use this chat's own LLM key for search/proposals
        ok, message = await self.automation.start(chat_id=chat_id)
        await self.send(
            chat_id,
            ("🚀 " if ok else "❌ ") + escape(message) + ("\n\nاولین نتیجه را همین‌جا می‌فرستم." if ok else ""),
            self._run_keyboard() if ok else None,
        )

    async def _cmd_stop(self, chat_id: str) -> None:
        ok, message = await self.automation.stop()
        await self.send(chat_id, ("⏸ " if ok else "ℹ️ ") + escape(message), self._run_keyboard())

    # ----------------------------------------------------------- dependencies
    def _deps_text(self) -> str:
        from .deps import DEPENDENCIES, has, install_command

        lines = ["<b>📦 بسته‌های لازم</b>", ""]
        for dep in DEPENDENCIES:
            ok = has(dep.module)
            lines.append(f"{'✅' if ok else '❌'} <code>{escape(dep.package)}</code> — {escape(dep.why)}")
            if not ok:
                lines.append(f"    <code>{escape(install_command(dep.package))}</code>")
        if all(has(dep.module) for dep in DEPENDENCIES):
            lines += ["", "همه‌چیز نصب است؛ نه لازم است کاری کنی."]
        else:
            lines += ["", "دکمه‌ی زیر نصب خودکار را انجام می‌دهد (نصب در همان پایتونی که ربات با آن اجرا شده)."]
        return "\n".join(lines)

    def _deps_keyboard(self) -> dict[str, Any]:
        from .deps import missing

        rows = [
            [{"text": f"⬇️ نصب {dep.package}", "callback_data": f"dep:{dep.package}"}] for dep in missing()
        ]
        rows.append([{"text": "📊 وضعیت", "callback_data": "cmd:status"}])
        return {"inline_keyboard": rows}

    async def _cmd_deps(self, chat_id: str) -> None:
        await self.send(chat_id, self._deps_text(), self._deps_keyboard())

    async def _install_dep(self, chat_id: str, package: str) -> None:
        from .deps import ensure, has

        module = package.strip()
        if not module.isidentifier():
            await self.send(chat_id, "نام بسته نامعتبر است.")
            return
        await self.send(chat_id, f"⬇️ دارم <code>{escape(module)}</code> را نصب می‌کنم…")
        ok, detail = await asyncio.to_thread(ensure, module)
        if ok or has(module):
            await self.send(chat_id, "✅ نصب شد.")
        else:
            from .deps import DEPENDENCIES, install_command

            dep = next((item for item in DEPENDENCIES if item.module == module), None)
            tail = f"\n<pre>{escape(detail[-300:])}</pre>" if detail else ""
            await self.send(
                chat_id,
                "❌ نصب خودکار نشد (اینترنت یا دسترسی نوشتن محیط مجازی).\n"
                "این دستور را در همان پوشه‌ی BidPilot اجرا کن:\n"
                f"<code>{escape(install_command(dep.package if dep else module))}</code>" + tail,
            )
        await self.send(chat_id, self._deps_text(), self._deps_keyboard())

    # ---------------------------------------------------------------- filters
    def _filter_view(self) -> tuple[Any, Any]:
        """(config.filters, ProjectFilter) built exactly like the pipeline does."""
        from .config import load_config
        from .filters import ProjectFilter

        cfg = load_config(self.config_path, store=self.store)
        flt = ProjectFilter(
            cfg.filters,
            profile_skills=list(cfg.profile.stack),
            profile_role=cfg.profile.role,
        )
        return cfg.filters, flt

    @staticmethod
    def _rule_line(enabled: bool, title: str, why: str) -> str:
        return f"{'✅' if enabled else '⬜️'} <b>{escape(title)}</b>\n<i>{escape(why)}</i>"

    def _filters_text(self) -> str:
        try:
            cfg, flt = self._filter_view()
        except Exception as exc:  # broken config must not look like a crash
            return f"❌ فیلترها خوانده نشد: {escape(str(exc))}"
        lines = [
            "<b>🧹 فیلتر پروژه‌ها</b>",
            "",
            self._rule_line(
                bool(cfg.require_budget),
                "بودجه اجباری",
                "پروژه‌ای که بودجه‌اش اعلام نشده باشد بررسی نمی‌شود (قیمت حدسی نمی‌دهم).",
            ),
            "",
            self._rule_line(
                bool(cfg.require_skill_match),
                "تطابق مهارت",
                "پروژه باید دست‌کم یکی از مهارت‌های پروفایل تو را داشته باشد.",
            ),
            "",
            f"🧠 <b>مهارت‌هایی که از پروفایل خواندم:</b> {escape(flt.describe_keywords())}",
        ]
        if cfg.min_budget or cfg.max_budget:
            low = f"{cfg.min_budget:,}" if cfg.min_budget else "—"
            high = f"{cfg.max_budget:,}" if cfg.max_budget else "—"
            lines.append(f"💰 بازه‌ی بودجه: {low} تا {high} تومان")
        if cfg.exclude_keywords:
            lines.append("🚫 ممنوعه‌ها: " + escape("، ".join(cfg.exclude_keywords[:8])))
        if not flt.skill_keywords:
            lines += [
                "",
                "⚠️ پروفایل مهارتی خالی است؛ تا وقتی /profile را کامل نکنی هیچ پروژه‌ای قبول نمی‌شود.",
            ]
        lines += [
            "",
            "<i>مهارت‌ها از /profile می‌آیند؛ با دکمه‌ی زیر دوباره از پروفایل ساخته می‌شوند.</i>",
        ]
        return "\n".join(lines)

    @staticmethod
    def _filters_keyboard() -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [{"text": "🔁 بودجه اجباری", "callback_data": "flt:budget"}],
                [{"text": "🔁 تطابق مهارت", "callback_data": "flt:skill"}],
                [{"text": "♻️ بازسازی از پروفایل", "callback_data": "flt:derive"}],
                [{"text": "📊 وضعیت", "callback_data": "cmd:status"}],
            ]
        }

    async def _cmd_filters(self, chat_id: str) -> None:
        await self.send(chat_id, self._filters_text(), self._filters_keyboard())

    async def _toggle_filter(self, chat_id: str, name: str) -> None:
        if name == "derive":
            self.store.set("filters.skill_keywords", [])
            await self.send(chat_id, "♻️ کلیدواژه‌ها دوباره از پروفایل ساخته شد.")
        elif name in {"budget", "skill"}:
            key = "filters.require_budget" if name == "budget" else "filters.require_skill_match"
            stored = self.store.get(key)
            current = True if stored is None else bool(stored)
            self.store.set(key, not current)
            await self.send(
                chat_id,
                f"{'✅ روشن' if not current else '⬜️ خاموش'} شد — "
                + ("بودجه اجباری" if name == "budget" else "تطابق مهارت"),
            )
        else:
            await self.send(chat_id, "این گزینه را نمی‌شناسم.")
            return
        await self.send(chat_id, self._filters_text(), self._filters_keyboard())

    async def _cmd_check(self, chat_id: str) -> None:
        await self.send(chat_id, "🔐 دارم ورود به حساب سایت را بررسی می‌کنم…")
        check = await check_site(self.store, config_path=self.config_path)
        await self.send(chat_id, check.message(), self._run_keyboard() if check.ok else None)

    @staticmethod
    def _run_keyboard() -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [{"text": "🚀 شروع فعالیت", "callback_data": "run:ask"}],
                [{"text": "⏸ توقف", "callback_data": "run:stop"}],
                [{"text": "📊 وضعیت", "callback_data": "cmd:status"}],
            ]
        }

    async def _cmd_sites(self, chat_id: str) -> None:
        rows = sites_overview(self.store)
        lines = ["<b>سایت‌های فریلنسری</b>", ""]
        for row in rows:
            mark = "🟢" if row["active"] else ("✅" if row["ready"] else "⚪️")
            lines.append(
                f"{mark} <b>{escape(row['label'])}</b>\n"
                f"     {escape(row['summary'])}" + ("\n     <i>سایت فعال</i>" if row["active"] else "")
            )
        lines.append("")
        lines.append("برای عوض کردن سایت فعال یکی را بزن (یا <code>/site ponisha</code>):")
        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text": ("🔵 " if row["active"] else "") + row["label"].split(" (")[0],
                        "callback_data": f"site:{row['site']}",
                    }
                ]
                for row in rows
            ]
        }
        await self.send(chat_id, "\n".join(lines), keyboard)

    async def _cmd_site(self, chat_id: str, name: str) -> None:
        name = (name or "").strip()
        if not name:
            await self._cmd_sites(chat_id)
            return
        try:
            active = switch_site(self.store, name)
        except QuestionError as exc:
            await self.send(chat_id, f"⚠️ {escape(str(exc))}\n\n/sites")
            return

        # keep the running daemon in sync (scheduler reloads on the next cycle)
        self._log(f"active site switched to {active}")
        from .credentials import site_credentials

        creds = site_credentials(active, self.store)
        token = self.store.get("telegram.bot_token")
        hint = "برای وارد کردن حساب: /market"
        if creds.ready:
            hint = "حساب این سایت ثبت شده ✅ — /test برای بررسی، /preview برای نمونه"
        if not token:
            hint += "\n(توکن تلگرام هنوز در پنل ذخیره نشده است.)"
        await self.send(
            chat_id,
            f"✅ سایت فعال شد: <b>{escape(site_label(active))}</b>\n\n{escape(hint)}",
            {
                "inline_keyboard": [
                    [
                        {"text": "🔑 حساب سایت", "callback_data": "cmd:market"},
                        {"text": "🏠 همه سایت‌ها", "callback_data": "cmd:sites"},
                    ]
                ]
            },
        )

    # ── Per-user onboarding helpers ─────────────────────────────
    async def _enforce_onboarding(self, chat_id: str) -> None:
        """Gate dashboard until Step 1 (LLM verified) and Step 2 (Profile) pass."""
        prog = onboarding_progress(self.store, chat_id)
        if not prog["llm_done"]:
            await self._start_llm_onboarding(chat_id)
            return
        if not prog["profile_done"]:
            await self._start_profile_onboarding(chat_id)
            return
        # Both done — proceed to normal start
        await self._show_dashboard(chat_id)

    async def _start_llm_onboarding(self, chat_id: str) -> None:
        prog = onboarding_progress(self.store, chat_id)
        # If provider not chosen, start at provider selection
        from .onboarding import user_llm_questions
        # Build a focused flow: provider -> api_key -> model
        qs = user_llm_questions(self.store, chat_id)
        # If already has provider but not verified, skip provider question
        if prog["provider"] and prog["provider"] in [c.value for c in qs[0].choice_list(self.store)]:
            # Keep all 3; _advance will skip applicable? Provider already set still applicable
            # We keep full flow but user can re-pick via buttons
            pass
        await self.send(
            chat_id,
            "🔐 <b>مرحله ۱ از ۳ — اتصال هوش مصنوعی شخصی تو</b>\n"
            "برای اینکه پیشنهادها با <b>کلید خودت</b> ساخته شوند (ایزوله برای هر کاربر)، "
            "ابتدا سرویس و کلیدت را وصل کنیم. این کلید فقط برای تو ذخیره می‌شود.\n"
            + "—" * 12 + "\n"
            "❗ بدون این مرحله، هیچ جستجو یا پیشنهادی انجام نمی‌شود.",
        )
        await self._start_flow(chat_id, "user_llm", qs, mark_complete=False)

    async def _start_profile_onboarding(self, chat_id: str) -> None:
        # Reuse existing profile_questions but per-user variant if available
        # For per-user, we store under users.{chat}.profile.*
        # Use global profile_questions flow but intercept save to per-user via apply_answer key_fn
        # Simpler: show profile source intro then start flow
        from .bot_ui import profile_intro_text
        from .onboarding import profile_questions
        # Build per-user profile source question: need to map to per-user keys
        # We will use global profile_questions but apply_answer will handle per-user if we inject key_fn
        # Instead, construct per-user variant:
        def _make_per_user_profile_qs():
            base = profile_questions(self.store)
            from .per_user import user_key, get_user
            new_qs = []
            for q in base:
                if q.key.startswith("profile."):
                    orig_key = q.key
                    orig_when = q.when
                    # Make when per-user aware: check users.{chat}.profile.source
                    def _make_when(orig, ok=orig_key):
                        if orig is None:
                            return None
                        def _wrapped(s, ok=ok):
                            per_src = get_user(s, chat_id, "profile.source")
                            if per_src is not None:
                                try:
                                    if ok == "profile.source":
                                        return True
                                    if ok == "profile.resume_path":
                                        return str(per_src).lower() == "resume"
                                    if ok == "profile.source_url":
                                        return str(per_src).lower() in {"github", "portfolio"}
                                    if ok in {"profile.first_name","profile.full_name","profile.role","profile.years_experience","profile.stack","profile.highlights","profile.portfolio_url","profile.signature"}:
                                        return str(per_src).lower() == "manual"
                                except Exception:
                                    pass
                            # Fallback to original global check
                            try:
                                return orig(s)
                            except Exception:
                                return True
                        return _wrapped
                    new_when = _make_when(orig_when) if orig_key != "profile.source" else None
                    new_q = Question(
                        key=q.key,
                        prompt=q.prompt,
                        kind=q.kind,
                        stage=q.stage,
                        choices=list(q.choices),
                        default=q.default,
                        help=q.help,
                        optional=q.optional,
                        allow_custom=q.allow_custom,
                        when=new_when,
                        validator=q.validator,
                        dynamic_prompt=q.dynamic_prompt,
                        dynamic_help=q.dynamic_help,
                        dynamic_default=q.dynamic_default,
                        dynamic_choices=q.dynamic_choices,
                        key_fn=lambda s, ok=orig_key: user_key(chat_id, ok),
                    )
                    new_qs.append(new_q)
                else:
                    new_qs.append(q)
            return new_qs
        qs = _make_per_user_profile_qs()
        await self.send(chat_id, profile_intro_text(step="۱/۲", stage="پروفایل و نمونه‌کار"))
        await self._start_flow(chat_id, "user_profile", qs, mark_complete=False)

    async def _show_dashboard(self, chat_id: str) -> None:
        try:
            from .bot_ui import main_menu_keyboard, main_menu_text
            from .per_user import user_status_text
            from .subscriptions.plans import get_plan
            from .db import Database
            from .subscriptions.manager import SubscriptionManager
            from .subscriptions.zarinpal import ZarinpalConfig
            db = Database(str(self.store.get("database_url") or "sqlite+aiosqlite:///data/bidpilot.sqlite3"))
            await db.init()
            async with db.session_factory() as s:
                mgr = SubscriptionManager(s, zarinpal_config=ZarinpalConfig(merchant_id=str(self.store.get("zarinpal.merchant_id") or "")))
                _, plan = await mgr.effective_plan(chat_id)
                plan_title = plan.title
            await db.dispose()
            # Strictly per-user: no global fallback
            first = str(self.store.get(f"users.{chat_id}.profile.first_name") or self.store.get(f"users.{chat_id}.profile.full_name") or "").split()[0] if self.store.get(f"users.{chat_id}.profile.full_name") else None
            await self.send(chat_id, main_menu_text(first_name=first, plan_title=plan_title) + "\n\n" + user_status_text(self.store, chat_id), main_menu_keyboard())
        except Exception:
            from .per_user import user_status_text as _ust
            try:
                await self.send(chat_id, _ust(self.store, chat_id) + "\n\n" + self._menu_hint(), self._menu_keyboard())
            except Exception:
                await self.send(chat_id, self._menu_hint(), self._menu_keyboard())

    async def _cmd_start_old(self, chat_id: str) -> None:
        save_chat_id(self.store, chat_id)
        try:
            await self._ensure_account(chat_id)
        except Exception:
            pass
        # ── New gated onboarding: LLM (verified) -> Profile -> dashboard ──
        if is_onboarding_blocked(self.store, chat_id):
            await self._enforce_onboarding(chat_id)
            return
        # fallback to legacy global readiness check for old installs
        state = readiness(self.store)
        if not state["setup_completed"] or missing_steps(self.store, include_recommended=False):
            # If per-user done but global missing, skip legacy setup and show dashboard
            if is_llm_onboarding_done(self.store, chat_id) and is_profile_onboarding_done(self.store, chat_id):
                await self._show_dashboard(chat_id)
                return
            await self.send(
                chat_id,
                "✨ <b>به BidPilot خوش آمدی 👋</b>\n"
                "<i>دستیار هوشمند پیشنهاددهی — بدون .env، همه در تلگرام</i>\n"
                "─" * 28 + "\n"
                "بیا در چند سؤال همه‌چیز را تنظیم کنیم — حساب شخصی تو ساخته می‌شود و سقف‌هات فعال می‌شود.\n"
                "هر وقت خواستی با /cancel متوقف شوی.",
            )
            await self._start_flow(chat_id, "setup", all_questions(self.store), mark_complete=True)
            return
        # Premium welcome with professional menu
        try:
            from .bot_ui import main_menu_keyboard, main_menu_text
            from .subscriptions.plans import get_plan
            # try to resolve user's plan title
            plan_title = None
            try:
                from .db import Database
                from .subscriptions.manager import SubscriptionManager
                from .subscriptions.zarinpal import ZarinpalConfig
                db = Database(str(self.store.get("database_url") or "sqlite+aiosqlite:///data/bidpilot.sqlite3"))
                await db.init()
                async with db.session_factory() as s:
                    mgr = SubscriptionManager(s, zarinpal_config=ZarinpalConfig(merchant_id=str(self.store.get("zarinpal.merchant_id") or "")))
                    _, plan = await mgr.effective_plan(chat_id)
                    plan_title = plan.title
                await db.dispose()
            except Exception:
                pass
            first = str(self.store.get("profile.first_name") or self.store.get("profile.full_name") or "").split()[0] if self.store.get("profile.full_name") else None
            await self.send(chat_id, main_menu_text(first_name=first, plan_title=plan_title) + "\n\n" + status_text(self.store), main_menu_keyboard())
            return
        except Exception:
            await self.send(chat_id, status_text(self.store) + "\n\n" + self._menu_hint(), self._menu_keyboard())

    async def _cmd_status(self, chat_id: str) -> None:
        text = status_text(self.store)
        rows = sites_overview(self.store)
        sites = "\n".join(
            f"{'🟢' if row['active'] else ('✅' if row['ready'] else '⚪️')} "
            f"{escape(row['label'])}" + (" (فعال)" if row["active"] else "")
            for row in rows
        )
        text += f"\n\n<b>سایت‌ها</b>\n{sites}"
        steps = missing_steps(self.store)
        if steps:
            text += "\n\n<b>باقی‌مانده:</b>\n" + "\n".join(f"• {escape(s)}" for s in steps)
        text += "\n\n" + tone_summary(self.store)

        from .network import proxy_from_store

        proxy = proxy_from_store(self.store)
        text += "\n🔌 پروکسی: " + (f"<code>{escape(proxy)}</code>" if proxy else "تنظیم نشده (مستقیم)")
        if self._last_send_error:
            text += (
                "\n⚠️ آخرین ارسال به تلگرام ناموفق بود ("
                + escape(self._last_send_error[:120])
                + ") — VPN/پروکسی را بررسی کن: /proxy"
            )

        from .runtime_info import install_info

        info = install_info()
        text += "\n" + info.status_line()
        if info.warning():
            text += "\n⚠️ " + escape(info.warning()) + f"\n<code>{escape(info.fix_command())}</code>"
        # Admin entry — only for owner
        from .admin import is_admin
        kb = self._menu_keyboard()
        if is_admin(chat_id, self.store):
            # inject admin button into keyboard
            try:
                kb["inline_keyboard"].insert(0, [{"text": "🔐 پنل ادمین — مدیریت کامل", "callback_data": "admin:main"}])
            except Exception:
                pass
            text += "\n\n<i>🔐 شما ادمین هستید — <code>/admin</code> برای پنل کامل.</i>"
        await self.send(chat_id, text, kb)

    # ── Admin Panel (owner only) ─────────────────────────────────────
    def _require_admin(self, chat_id: str) -> bool:
        from .admin import is_admin
        return is_admin(chat_id, self.store)

    async def _admin_stats(self) -> dict[str, Any]:
        stats: dict[str, Any] = {}
        try:
            from .db import Database
            db = Database(str(self.store.get("database_url") or "sqlite+aiosqlite:///data/bidpilot.sqlite3"))
            await db.init()
            async with db.session_factory() as s:
                from sqlalchemy import func, select
                from .subscriptions.models import PaymentRow, ReferralRow, UserAccountRow
                from .db import BidRow
                # users
                stats["users_total"] = (await s.execute(select(func.count()).select_from(UserAccountRow))).scalar() or 0
                stats["users_active_7d"] = (await s.execute(select(func.count()).select_from(UserAccountRow).where(UserAccountRow.updated_at >= datetime.now(timezone.utc) - timedelta(days=7)))).scalar() or 0
                # subs active (plan != free and not expired)
                stats["subs_active"] = (await s.execute(select(func.count()).select_from(UserAccountRow).where(UserAccountRow.plan_id != "free"))).scalar() or 0
                # revenue
                rev = (await s.execute(select(func.coalesce(func.sum(PaymentRow.amount_toman), 0)).where(PaymentRow.status == "verified"))).scalar() or 0
                stats["revenue_total"] = f"{int(rev):,} تومان".replace(",", "٬")
                stats["payments_ok"] = (await s.execute(select(func.count()).select_from(PaymentRow).where(PaymentRow.status == "verified"))).scalar() or 0
                stats["referrals_ok"] = (await s.execute(select(func.count()).select_from(ReferralRow).where(ReferralRow.status.in_(["qualified","purchased"])))).scalar() or 0
                stats["bids_submitted"] = (await s.execute(select(func.count()).select_from(BidRow).where(BidRow.status == "submitted"))).scalar() or 0
                # 7/30d revenue
                for days, key in [(1,"revenue_today"),(7,"revenue_7d"),(30,"revenue_30d")]:
                    since = datetime.now(timezone.utc) - timedelta(days=days)
                    r = (await s.execute(select(func.coalesce(func.sum(PaymentRow.amount_toman),0)).where(PaymentRow.status=="verified", PaymentRow.verified_at >= since))).scalar() or 0
                    stats[key] = f"{int(r):,} تومان".replace(",", "٬")
            await db.dispose()
        except Exception as e:
            stats["error"] = str(e)
        return stats

    async def _handle_admin_callback(self, chat_id: str, data: str) -> None:
        if not self._require_admin(chat_id):
            await self.send(chat_id, "⛔ این بخش فقط برای ادمین (7806301378) است.")
            return
        # Routing
        try:
            from . import admin as admin_ui
            from sqlalchemy import select, func
            from .db import Database
            from .subscriptions.models import PaymentRow, UserAccountRow, ReferralRow
        except Exception as e:
            await self.send(chat_id, f"خطای بارگذاری پنل: {escape(str(e))}")
            return

        cmd = data[len("admin:"):]
        # Main
        if cmd == "main":
            stats = await self._admin_stats()
            await self.send(chat_id, admin_ui.admin_main_text(stats), admin_ui.admin_main_keyboard())
            return
        if cmd == "users" or cmd.startswith("users:"):
            parts = cmd.split(":")
            page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
            if cmd == "users:search":
                await self.send(chat_id, "🔍 برای جستجو: <code>/admin_search 78063</code> یا <code>/admin_set user:7806301378 plan elite</code>\nبرای دیدن جزئیات، شناسه را بفرست: <code>/admin_user 8697160216</code>")
                return
            # fetch page
            try:
                db = Database(str(self.store.get("database_url") or "sqlite+aiosqlite:///data/bidpilot.sqlite3"))
                await db.init()
                async with db.session_factory() as s:
                    total = (await s.execute(select(func.count()).select_from(UserAccountRow))).scalar() or 0
                    limit = 6
                    offset = (page-1)*limit
                    rows_q = await s.execute(select(UserAccountRow).order_by(UserAccountRow.id.desc()).limit(limit).offset(offset))
                    rows = rows_q.scalars().all()
                    ui_rows = []
                    for r in rows:
                        exp = r.plan_expires_at.strftime("%Y-%m-%d") if r.plan_expires_at else "—"
                        ui_rows.append({"chat_id": r.telegram_chat_id, "username": r.telegram_username, "plan_id": r.plan_id, "expires": exp, "is_active": r.is_active, "referral_code": r.referral_code, "referred_by": r.referred_by})
                    has_more = offset + limit < total
                    await self.send(chat_id, admin_ui.users_text(ui_rows, total, page), admin_ui.users_keyboard(ui_rows, page, has_more))
                await db.dispose()
            except Exception as e:
                await self.send(chat_id, f"خطا: {escape(str(e))}")
            return
        if cmd.startswith("user:"):
            # admin:user:chat_id  or admin:user:plan:chat_id etc
            parts = cmd.split(":")
            # actions with 3 parts: user:action:chat_id
            if len(parts) == 3 and parts[1] in {"plan","extend","toggle","delete","bonus"}:
                action, target = parts[1], parts[2]
                if action == "plan":
                    await self.send(chat_id, f"برای تغییر پلن کاربر <code>{escape(target)}</code>:\n<code>/admin_set user:{target} plan elite</code> یا free/starter/pro\nمثال: <code>/admin_set user:{target} plan pro</code>")
                    return
                if action == "extend":
                    await self.send(chat_id, f"تمدید برای <code>{escape(target)}</code>:\n<code>/admin_set user:{target} extend 30</code> → ۳۰ روز\n<code>/admin_set user:{target} extend_bonus 7 starter</code>")
                    return
                if action == "toggle":
                    try:
                        db = Database(str(self.store.get("database_url") or "sqlite+aiosqlite:///data/bidpilot.sqlite3"))
                        await db.init()
                        async with db.session_factory() as s:
                            row = (await s.execute(select(UserAccountRow).where(UserAccountRow.telegram_chat_id==str(target)))).scalar_one_or_none()
                            if row:
                                row.is_active = not row.is_active
                                await s.commit()
                                await self.send(chat_id, f"{'✅ فعال شد' if row.is_active else '🚫 مسدود شد'}: {escape(target)}")
                            else:
                                await self.send(chat_id, "کاربر یافت نشد.")
                        await db.dispose()
                    except Exception as e:
                        await self.send(chat_id, f"خطا: {escape(str(e))}")
                    return
                if action == "delete":
                    await self.send(chat_id, f"⚠️ حذف کاربر <code>{escape(target)}</code> قطعی است:\n<code>/admin_set user:{target} delete yes</code>")
                    return
                if action == "bonus":
                    await self.send(chat_id, f"هدیه روز: <code>/admin_set user:{target} bonus 7 pro</code> → ۷ روز حرفه‌ای")
                    return
            # view user detail: admin:user:CHATID
            if len(parts)==2:
                target = parts[1]
                try:
                    db = Database(str(self.store.get("database_url") or "sqlite+aiosqlite:///data/bidpilot.sqlite3"))
                    await db.init()
                    async with db.session_factory() as s:
                        row = (await s.execute(select(UserAccountRow).where(UserAccountRow.telegram_chat_id==str(target)))).scalar_one_or_none()
                        if not row:
                            await self.send(chat_id, "کاربر یافت نشد.")
                        else:
                            u = {
                                "chat_id": row.telegram_chat_id,
                                "username": row.telegram_username,
                                "first_name": row.telegram_first_name,
                                "plan_id": row.plan_id,
                                "is_active": row.is_active,
                                "started": row.plan_started_at.strftime("%Y-%m-%d %H:%M") if row.plan_started_at else None,
                                "expires": row.plan_expires_at.strftime("%Y-%m-%d %H:%M") if row.plan_expires_at else None,
                                "referral_code": row.referral_code,
                                "referred_by": row.referred_by,
                                "total_bids": row.total_bids,
                                "successful_bids": row.successful_bids,
                                "created": row.created_at.strftime("%Y-%m-%d") if row.created_at else None,
                            }
                            await self.send(chat_id, admin_ui.user_detail_text(u), admin_ui.user_detail_keyboard(target))
                    await db.dispose()
                except Exception as e:
                    await self.send(chat_id, f"خطا: {escape(str(e))}")
                return
        if cmd == "plans" or cmd.startswith("plan:"):
            # plan list or single plan actions
            if cmd == "plans":
                from .subscriptions.plans import all_plans as _all
                plans = [{"id":p.id,"title":p.title,"tagline":p.tagline,"price_toman":p.price_toman,"period_days":p.period_days,"badge":p.badge,"limits":p.limits.__dict__, "features": p.features} for p in _all()]
                overrides = {k.replace("admin.plans.",""): self.store.get(k) for k in [f"admin.plans.{p['id']}.price_toman" for p in plans] if self.store.get(k) is not None}
                # also collect limit overrides
                for p in plans:
                    for f in ["max_bids_per_hour","max_bids_per_day","max_bids_per_month","min_seconds_between_bids","max_active_sites"]:
                        v = self.store.get(f"admin.plans.{p['id']}.limits.{f}")
                        if v is not None:
                            overrides.setdefault(f"{p['id']}_limits", {})[f] = v
                await self.send(chat_id, admin_ui.plans_admin_text(plans, overrides), admin_ui.plans_admin_keyboard(plans))
                return
            # single plan view / actions
            parts = cmd.split(":")
            if len(parts)==2:
                pid = parts[1]
                if pid == "new":
                    await self.send(chat_id, "پلن سفارشی: <code>/admin_set plan:custom price 450000</code> سپس <code>/admin_set plan:custom title \"پلن ویژه\"</code>")
                    return
                from .subscriptions.plans import get_plan
                try:
                    p = get_plan(pid)
                    plan_dict = {"id":p.id,"title":p.title,"tagline":p.tagline,"price_toman":p.price_toman,"period_days":p.period_days,"badge":p.badge,"limits":p.limits.__dict__,"features":p.features}
                    ov = {}
                    # gather overrides for this plan
                    for k in [f"admin.plans.{pid}.price_toman", f"admin.plans.{pid}.period_days"]:
                        v = self.store.get(k)
                        if v is not None:
                            ov[pid] = ov.get(pid,{})
                            ov[pid][k.split(".")[-1]] = v
                    for f in ["max_bids_per_hour","max_bids_per_day","max_bids_per_month","min_seconds_between_bids","max_active_sites"]:
                        v = self.store.get(f"admin.plans.{pid}.limits.{f}")
                        if v is not None:
                            ov.setdefault(f"{pid}_limits", {})[f]=v
                    await self.send(chat_id, admin_ui.single_plan_text(plan_dict, ov), admin_ui.single_plan_keyboard(pid))
                except Exception as e:
                    await self.send(chat_id, f"پلن نامعتبر: {escape(str(e))}")
                return
            if len(parts)==3:
                _, field, pid = parts
                if field == "price":
                    await self.send(chat_id, f"قیمت جدید برای <b>{esc(pid)}</b> را بفرست:\n<code>/admin_set plan:{pid} price 350000</code>")
                    return
                if field == "period":
                    await self.send(chat_id, f"دوره جدید (روز): <code>/admin_set plan:{pid} period 30</code>")
                    return
                if field == "limits":
                    await self.send(chat_id, f"سقف‌ها: <code>/admin_set plan:{pid} limits max_bids_per_hour 20</code>\nیا <code>/admin_set plan:{pid} limits max_bids_per_day 100</code>")
                    return
                if field == "recommend":
                    cur = self.store.get(f"admin.plans.{pid}.recommended")
                    new = not bool(cur) if cur is not None else True
                    # simplified: store flag
                    self.store.set(f"admin.plans.{pid}.recommended", new)
                    await self.send(chat_id, f"{'⭐ پیشنهاد ویژه شد' if new else 'حذف از پیشنهاد ویژه'}: {esc(pid)}")
                    return
        if cmd == "revenue":
            await self.send(chat_id, admin_ui.revenue_text(self.store), admin_ui.revenue_keyboard())
            return
        if cmd.startswith("rev:"):
            sub = cmd.split(":")[1]
            if sub == "ratio":
                await self.send(chat_id, "نسبت داخل بازه (۰٫۱ تا ۱٫۰) یا درصد تخفیف بودجه ثابت:\n<code>/admin_set pricing.ratio 0.5</code> یا <code>/admin_set pricing.bid_percent 60</code>")
                return
            if sub == "strategy":
                await self.send(chat_id, "استراتژی: <code>/admin_set pricing.strategy midpoint_round</code>\nگزینه‌ها: midpoint_round / aggressive_low / premium_high / fixed")
                return
            if sub == "commission":
                await self.send(chat_id, "کمیسیون پلتفرم ٪: <code>/admin_set admin.revenue.commission_percent 10</code>")
                return
            if sub == "promo":
                await self.send(chat_id, "کد تخفیف: <code>/admin_set admin.promo.active_code WELCOME10</code> و درصد: <code>/admin_set admin.promo.discount_percent 10</code>")
                return
        if cmd == "gateways":
            await self.send(chat_id, admin_ui.gateways_text(self.store), admin_ui.gateways_keyboard())
            return
        if cmd.startswith("gw:"):
            gid = cmd.split(":")[1]
            await self.send(chat_id, f"مرچنت {esc(gid)}:\n<code>/admin_set payments.{gid}.merchant_id YOUR_MERCHANT</code>\nسندباکس: <code>/admin_set payments.{gid}.sandbox 1</code>")
            return
        if cmd == "referrals":
            # stats
            try:
                db = Database(str(self.store.get("database_url") or "sqlite+aiosqlite:///data/bidpilot.sqlite3"))
                await db.init()
                async with db.session_factory() as s:
                    total = (await s.execute(select(func.count()).select_from(ReferralRow))).scalar() or 0
                    qual = (await s.execute(select(func.count()).select_from(ReferralRow).where(ReferralRow.status.in_(["qualified","purchased"])))).scalar() or 0
                    purch = (await s.execute(select(func.count()).select_from(ReferralRow).where(ReferralRow.status=="purchased"))).scalar() or 0
                    stats = {"total": total, "qualified": qual, "purchased": purch}
                await db.dispose()
            except Exception:
                stats = {}
            await self.send(chat_id, admin_ui.referrals_text(self.store, stats), admin_ui.referrals_keyboard())
            return
        if cmd.startswith("ref:"):
            sub = cmd.split(":")[1]
            if sub == "milestones":
                await self.send(chat_id, "میل‌استون‌ها JSON:\n<code>/admin_set admin.referrals.milestones [{\"count\":3,\"days\":7,\"plan\":\"starter\"}]</code>")
                return
            if sub == "bonus":
                await self.send(chat_id, "پاداش خرید معرف: <code>/admin_set admin.referrals.purchase_bonus_days 7</code>")
                return
            if sub == "list":
                try:
                    db = Database(str(self.store.get("database_url") or "sqlite+aiosqlite:///data/bidpilot.sqlite3"))
                    await db.init()
                    async with db.session_factory() as s:
                        rows = (await s.execute(select(ReferralRow).order_by(ReferralRow.id.desc()).limit(8))).scalars().all()
                        txt = "<b>آخرین دعوت‌ها</b>\n"+hr()+"\n" if rows else "هنوز دعوتی نیست."
                        for r in rows:
                            txt += f"<code>{esc(r.inviter_chat_id)}</code> → <code>{esc(r.invitee_chat_id)}</code> · {esc(r.status)}\n"
                        await self.send(chat_id, txt)
                    await db.dispose()
                except Exception as e:
                    await self.send(chat_id, f"خطا: {escape(str(e))}")
                return
        if cmd == "reports":
            stats = await self._admin_stats()
            # enrich
            total_users = stats.get("users_total",0)
            subs = stats.get("subs_active",0)
            conv = f"{(int(subs)/max(int(total_users),1)*100):.1f}٪" if str(total_users).isdigit() else "—"
            stats["conversion_rate"] = conv
            await self.send(chat_id, admin_ui.reports_text(stats), admin_ui.reports_keyboard())
            return
        if cmd.startswith("reports:"):
            sub = cmd.split(":")[1]
            if sub == "csv":
                which = cmd.split(":")[2] if ":" in cmd[8:] else "users"
                await self.send(chat_id, f"در حال تولید CSV <b>{esc(which)}</b>...\nبرای دانلود دستی: روی سرور <code>sqlite3 data/bidpilot.sqlite3 \"SELECT * FROM user_accounts;\" > export.csv</code>")
                return
            if sub == "chart":
                await self.send(chat_id, "نمودار ۷روزه (درآمد/ثبت‌نام) بزودی — فعلاً از <b>Reports</b> بخش درآمد روزانه را ببینید.")
                return
        if cmd == "system":
            await self.send(chat_id, admin_ui.system_text(self.store), admin_ui.system_keyboard())
            return
        if cmd.startswith("sys:"):
            sub = cmd.split(":")[1]
            if sub == "poll":
                await self.send(chat_id, "پولینگ: <code>/admin_set runtime.poll_interval_seconds 120</code> و جیتر <code>/admin_set runtime.jitter_seconds 30</code>")
                return
            if sub == "mode":
                cur = self.store.get("runtime.submit_mode") or "dry_run"
                nxt = "live" if cur == "dry_run" else "dry_run"
                self.store.set("runtime.submit_mode", nxt)
                await self.send(chat_id, f"حالت ارسال → <b>{esc(nxt)}</b>")
                return
            if sub == "llm":
                await self.send(chat_id, "LLM: <code>/admin_set llm.provider groq</code> و مدل <code>/admin_set llm.model openai/gpt-oss-120b</code>")
                return
            if sub == "proxy":
                await self.send(chat_id, "پروکسی: <code>/admin_set network.proxy http://127.0.0.1:10809</code> یا <code>off</code>")
                return
            if sub == "filters":
                await self.send(chat_id, "فیلترها: <code>/admin_set filters.require_budget true</code> / <code>filters.require_skill_match</code>")
                return
            if sub == "site":
                await self.send(chat_id, "سایت فعال: <code>/admin_set marketplace.name parscoders</code> (یا ponisha/karlancer)")
                return
            if sub == "maintenance":
                cur = self.store.get_bool("admin.maintenance")
                self.store.set("admin.maintenance", not cur)
                await self.send(chat_id, f"حالت نگهداری: {'🔴 روشن — کاربران عادی بلاک می‌شوند' if not cur else '🟢 خاموش'}")
                return
        if cmd == "advanced":
            await self.send(chat_id, admin_ui.advanced_text(self.store), admin_ui.advanced_keyboard())
            return
        if cmd.startswith("adv:"):
            sub = cmd.split(":")[1]
            if sub == "logs":
                await self.send(chat_id, "لاگ: روی سرور <code>tail -f data/bidpilot.log</code> یا <code>bidpilot doctor</code>")
                return
            if sub == "cleanup":
                await self.send(chat_id, "پاکسازی: <code>/admin_set admin.adv.cleanup yes</code> (فعلاً دستی روی سرور انجام می‌شود)")
                return
            if sub == "addadmin":
                await self.send(chat_id, "افزودن ادمین دوم: <code>/admin_set admin.extra_ids 123456,789012</code>")
                return
        if cmd == "broadcast":
            await self.send(chat_id, "📢 پیام همگانی: <code>/broadcast سلام — آپدیت جدید ...</code>\nهمه کاربران فعال پیام را دریافت می‌کنند.")
            return
        if cmd.startswith("broadcast:"):
            # admin:broadcast:confirm:123
            if "confirm" in cmd:
                pending = self.store.get("admin.pending_broadcast")
                if not pending:
                    await self.send(chat_id, "پیام همگانی یافت نشد — دوباره <code>/broadcast</code> بفرست.")
                    return
                # send to all users
                from .db import Database
                from .subscriptions.models import UserAccountRow
                from sqlalchemy import select
                await self.send(chat_id, f"⏳ در حال ارسال به همه کاربران... \n<code>{esc(str(pending)[:200])}...</code>")
                db = Database(str(self.store.get("database_url") or "sqlite+aiosqlite:///data/bidpilot.sqlite3"))
                await db.init()
                count = 0
                async with db.session_factory() as s:
                    rows = (await s.execute(select(UserAccountRow.telegram_chat_id))).scalars().all()
                    for uid in rows:
                        try:
                            await self.send(str(uid), f"📢 <b>پیام از تیم BidPilot</b>\n{hr()}\n{esc(str(pending))}")
                            count += 1
                            await asyncio.sleep(0.05)
                        except Exception:
                            pass
                await db.dispose()
                self.store.delete("admin.pending_broadcast")
                await self.send(chat_id, f"✅ پیام همگانی به {count} کاربر ارسال شد.")
                return
        await self.send(chat_id, "دستور ادمین نامعتبر.", admin_ui.admin_main_keyboard())

    async def _handle_admin_set(self, chat_id: str, rest: str) -> None:
        if not rest or " " not in rest.strip():
            await self.send(chat_id, "<b>فرمت:</b> <code>/admin_set &lt;key&gt; &lt;value&gt;</code>\nنمونه‌ها:\n"
                                      "<code>/admin_set pricing.ratio 0.4</code>\n"
                                      "<code>/admin_set admin.revenue.commission_percent 12</code>\n"
                                      "<code>/admin_set plan:starter price 350000</code> (میانبر)\n"
                                      "<code>/admin_set user:8697160216 plan elite</code>\n"
                                      "<code>/admin_set user:8697160216 extend 30</code>")
            return
        # user:xxx sugar
        rest = rest.strip()
        # handle sugar: plan:xxx, user:xxx
        if rest.startswith("plan:") or rest.startswith("user:"):
            # translate to store keys
            try:
                # plan:starter price 350000 -> admin.plans.starter.price_toman 350000
                if rest.startswith("plan:"):
                    # e.g. plan:starter price 350000
                    left, val = rest.rsplit(" ", 1)
                    _, plan_and_field = left.split(" ", 1)  # "plan:starter" "price"
                    # Actually rest = "plan:starter price 350000" -> left="plan:starter price", val="350000"
                    # Better split 3 parts
                    parts = rest.split()
                    # parts[0]=plan:starter, parts[1]=price, parts[2]=350000 (value may contain spaces? take rest)
                    if len(parts) >= 3:
                        plan_part = parts[0]  # plan:starter
                        field = parts[1]
                        value = " ".join(parts[2:])
                        pid = plan_part.split(":")[1]
                        key_map = {"price":"admin.plans.{}.price_toman","period":"admin.plans.{}.period_days","title":"admin.plans.{}.title"}
                        key_tmpl = key_map.get(field, f"admin.plans.{{}}.{field}")
                        key = key_tmpl.format(pid)
                        # try convert numeric
                        try:
                            if field in {"price","period"} or "price" in key:
                                value = str(int(value.replace(",","").replace("٬","")))
                            self.store.set(key, int(value))
                        except ValueError:
                            self.store.set(key, value)
                        await self.send(chat_id, f"✅ ذخیره شد: <code>{esc(key)} = {esc(value)}</code>")
                        return
                if rest.startswith("user:"):
                    parts = rest.split()
                    if len(parts) >= 3:
                        user_part = parts[0]  # user:ID
                        field = parts[1]
                        value = " ".join(parts[2:])
                        uid = user_part.split(":")[1]
                        # handle user actions via DB
                        from .db import Database
                        from sqlalchemy import select
                        from .subscriptions.models import UserAccountRow
                        from datetime import timedelta, timezone
                        db = Database(str(self.store.get("database_url") or "sqlite+aiosqlite:///data/bidpilot.sqlite3"))
                        await db.init()
                        async with db.session_factory() as s:
                            row = (await s.execute(select(UserAccountRow).where(UserAccountRow.telegram_chat_id==str(uid)))).scalar_one_or_none()
                            if not row:
                                await self.send(chat_id, "کاربر یافت نشد.")
                                await db.dispose()
                                return
                            if field == "plan":
                                row.plan_id = value.strip().lower()
                                row.plan_started_at = datetime.now(timezone.utc)
                                row.plan_expires_at = datetime.now(timezone.utc) + timedelta(days=30)
                                await s.commit()
                                await self.send(chat_id, f"✅ پلن {esc(uid)} → <b>{esc(row.plan_id)}</b> تا {esc(row.plan_expires_at.strftime('%Y-%m-%d'))}")
                            elif field in {"extend","extend_bonus","bonus"}:
                                days = int(value.split()[0])
                                plan_id = value.split()[1] if len(value.split())>1 else row.plan_id
                                # extend
                                now = datetime.now(timezone.utc)
                                base = row.plan_expires_at if row.plan_expires_at and row.plan_expires_at>now else now
                                row.plan_expires_at = base + timedelta(days=days)
                                if field != "extend" and len(value.split())>1:
                                    row.plan_id = plan_id
                                await s.commit()
                                await self.send(chat_id, f"✅ تمدید {days} روز برای {esc(uid)} → انقضا {esc(row.plan_expires_at.strftime('%Y-%m-%d'))}")
                            elif field == "delete" and value.lower() in {"yes","true","1"}:
                                await s.delete(row)
                                await s.commit()
                                await self.send(chat_id, f"🗑️ حذف شد: {esc(uid)}")
                            else:
                                await self.send(chat_id, f"فیلد نامعتبر: {esc(field)}")
                        await db.dispose()
                        return
            except Exception as e:
                await self.send(chat_id, f"خطا: {escape(str(e))}")
                return
        # generic store set: key value
        try:
            key, val = rest.split(" ", 1)
            key = key.strip()
            val = val.strip()
            # try json / int / bool
            lowered = val.lower()
            if lowered in {"true","false","yes","no","on","off"}:
                b = lowered in {"true","yes","on"}
                self.store.set(key, b)
            else:
                try:
                    # int?
                    if val.replace(",","").replace("٬","").replace("-","").isdigit():
                        self.store.set(key, int(val.replace(",","").replace("٬","")))
                    elif val.replace(".","",1).replace(",","").isdigit():
                        self.store.set(key, float(val))
                    else:
                        # try json
                        if (val.startswith("{") or val.startswith("[")):
                            self.store.set(key, json.loads(val))
                        else:
                            self.store.set(key, val)
                except Exception:
                    self.store.set(key, val)
            await self.send(chat_id, f"✅ ذخیره شد: <code>{esc(key)} = {esc(val)}</code>")
        except Exception as e:
            await self.send(chat_id, f"❌ ذخیره ناموفق: {escape(str(e))}")

    async def _handle_admin_search(self, chat_id: str, rest: str) -> None:
        q = (rest or "").strip()
        if not q:
            await self.send(chat_id, "مثال: <code>/admin_search ali</code> یا <code>/admin_search 8697</code>")
            return
        try:
            from .db import Database
            from sqlalchemy import select, or_
            from .subscriptions.models import UserAccountRow
            db = Database(str(self.store.get("database_url") or "sqlite+aiosqlite:///data/bidpilot.sqlite3"))
            await db.init()
            async with db.session_factory() as s:
                stmt = select(UserAccountRow).where(or_(UserAccountRow.telegram_chat_id.contains(q), UserAccountRow.telegram_username.contains(q), UserAccountRow.telegram_first_name.contains(q))).limit(10)
                rows = (await s.execute(stmt)).scalars().all()
                if not rows:
                    await self.send(chat_id, "نتیجه‌ای نبود.")
                else:
                    txt = f"<b>نتایج جستجو «{esc(q)}» — {len(rows)} مورد</b>\n{hr()}\n"
                    for r in rows:
                        txt += f"<code>{esc(r.telegram_chat_id)}</code> · @{esc(r.telegram_username or '—')} · {esc(r.plan_id)} · انقضا {esc(r.plan_expires_at.strftime('%Y-%m-%d') if r.plan_expires_at else '—')}\n"
                    await self.send(chat_id, txt)
            await db.dispose()
        except Exception as e:
            await self.send(chat_id, f"خطا: {escape(str(e))}")

    async def _handle_broadcast(self, chat_id: str, rest: str) -> None:
        if not rest or len(rest.strip()) < 5:
            await self.send(chat_id, "متن پیام همگانی را بفرست:\n<code>/broadcast سلام — فردا تخفیف ۲۰٪ برای پلن حرفه‌ای!</code>")
            return
        text = rest.strip()
        # confirmation keyboard
        await self.send(chat_id, f"<b>پیش‌نمایش پیام همگانی:</b>\n\n{esc(text)}\n\n{hr()}\nآیا ارسال شود به همه کاربران؟", {"inline_keyboard": [[{"text": "✅ بله، ارسال کن", "callback_data": f"admin:broadcast:confirm:{len(text)}"} , {"text": "❌ لغو", "callback_data": "admin:main"}]]})
        # store pending broadcast in memory/flow? simple: store in store temp
        self.store.set("admin.pending_broadcast", text)

    async def _cmd_profile(self, chat_id: str) -> None:
        """A failed import keeps its queue: offer the retry before asking again."""
        from .profile_import import pending_import

        pending = pending_import(self.store)
        error = str(self.store.get("profile.import_error") or "")
        if pending and error:
            kind = "فایل PDF" if pending["type"] == "file" else "همان لینک"
            await self.send(
                chat_id,
                "⚠️ آخرین تلاش برای خواندن پروفایل ناموفق بود:\n"
                f"<pre>{escape(error[:400])}</pre>\n"
                f"{kind} هنوز ذخیره است — می‌توانم بدون اینکه دوباره بفرستی، از نو پردازشش کنم.",
                {
                    "inline_keyboard": [
                        [{"text": "🔁 تلاش دوباره", "callback_data": "profile:retry"}],
                        [{"text": "🆕 از نو (انتخاب منبع)", "callback_data": "profile:restart"}],
                    ]
                },
            )
            return
        await self._start_flow(chat_id, "profile", profile_questions(self.store))

    async def _retry_profile_import(self, chat_id: str) -> None:
        from .deps import ensure, has
        from .profile_import import pending_import

        if pending_import(self.store) is None:
            await self.send(
                chat_id,
                "چیزی در صف نیست (فایل/لینک قبلی پاک شده).\n/profile را بزن و از نو بفرست.",
            )
            return
        if not has("pypdf"):
            import asyncio

            await self.send(chat_id, "📦 اول بسته‌ی pypdf را نصب می‌کنم…")
            ok, _ = await asyncio.to_thread(ensure, "pypdf")
            if not ok:
                await self._send_pdf_install_help(chat_id)
                return
        await self.send(chat_id, "🔁 دوباره تلاش می‌کنم…")
        await self._import_profile_from_pending(chat_id)

    async def _send_pdf_install_help(self, chat_id: str, *, reason: str = "") -> None:
        from .deps import install_command

        head = "❌ نصب خودکار pypdf انجام نشد (اینترنت یا دسترسی نوشتن محیط مجازی)."
        if reason:
            head += f"\n<pre>{escape(reason[:300])}</pre>"
        await self.send(
            chat_id,
            head + "\n\nاین دستور را در همان پوشه‌ی BidPilot اجرا کن:\n"
            f"<code>{escape(install_command('pypdf'))}</code>\n\n"
            "بعد دکمه‌ی «🔁 تلاش دوباره» را بزن — لازم نیست فایل را دوباره بفرستی.",
            {"inline_keyboard": [[{"text": "🔁 تلاش دوباره", "callback_data": "profile:retry"}]]},
        )

    async def _apply_proxy_and_check(self, chat_id: str) -> None:
        """Save the proxy, reconnect Telegram through it, then prove it works."""
        from .network import diagnose, proxy_from_store

        proxy = proxy_from_store(self.store)
        setter = getattr(self.api, "set_proxy", None)
        if setter is not None:
            await setter(proxy)  # next request builds a client with the new proxy
        if proxy:
            await self.send(
                chat_id,
                f"🔌 پروکسی ذخیره شد: <code>{escape(proxy)}</code>\n"
                "دارم تلگرام و هوش مصنوعی را با همین پروکسی تست می‌کنم…",
            )
        else:
            await self.send(chat_id, "✅ پروکسی خاموش شد؛ مستقیم وصل می‌شوم. دارم تست می‌کنم…")

        reach = await diagnose(
            token=str(getattr(self.api, "token", "") or ""),
            llm_check=self._llm_probe(),
            proxy=proxy,
        )
        await self.send(chat_id, reach.summary())
        if reach.telegram:
            await self.send(chat_id, "✅ از این به بعد ربات از همین مسیر وصل می‌شود.")
        else:
            await self.send(
                chat_id,
                "❌ هنوز تلگرام در دسترس نیست. اگر VPN روشن است، پورت پروکسی‌اش را چک کن "
                "(V2Ray/Clash پورت HTTP، مثلاً 10809) و دوباره /proxy بزن.",
                {"inline_keyboard": [[{"text": "🔁 تست دوباره", "callback_data": "llm:test"}]]},
            )

    def _llm_probe(self):
        """A callable ``diagnose`` can await: is the model reachable right now?"""

        async def probe() -> tuple[bool, str]:
            provider = str(self.store.get("llm.provider") or "groq")
            key = self.store.get("llm.api_key") or self.store.get(f"llm.keys.{provider}")
            if not key:
                return False, "کلید تنظیم نشده (/llm)"
            from .llm_providers import get_preset, is_reasoning_model, verify_provider

            preset = get_preset(provider)
            model = str(self.store.get("llm.model") or preset.model)
            ok, message = await verify_provider(
                LLMProvider(
                    id=preset.id,
                    base_url=str(self.store.get("llm.base_url") or preset.base_url),
                    model=model,
                    api_key=str(key),
                    extra_headers=dict(preset.extra_headers),
                ),
                proxy=str(self.store.get("network.proxy") or self.store.get("llm.proxy") or ""),
                reasoning_effort="low" if is_reasoning_model(model) else "",
            )
            return ok, message

        return probe

    async def _cmd_settings(self, chat_id: str) -> None:
        rows = self.store.masked_items()
        if not rows:
            await self.send(chat_id, "هنوز چیزی ذخیره نشده است. /setup")
            return
        body = "\n".join(f"{escape(key)} = {escape(value)}" for key, value in rows[:40])
        await self.send(chat_id, f"<b>تنظیمات ذخیره‌شده</b>\n<pre>{body}</pre>")

    async def _cmd_test(self, chat_id: str) -> None:
        # Per-user test first
        per_provider = get_user_llm_provider_id(self.store, chat_id)
        if per_provider:
            provider = per_provider
            key = get_user_llm_api_key(self.store, chat_id)
        else:
            provider = str(self.store.get("llm.provider") or "groq")
            key = self.store.get("llm.api_key") or self.store.get(f"llm.keys.{provider}")
        if not key:
            await self.send(chat_id, "❌ کلید هوش مصنوعی ثبت نشده. /llm")
            return
        try:
            preset = get_preset(provider)
        except KeyError:
            await self.send(chat_id, f"❌ ارائه‌دهنده‌ی ناشناخته: {escape(provider)}")
            return
        await self.send(chat_id, "⏳ در حال تست کلید هوش مصنوعی…")
        from .llm_providers import is_reasoning_model
        from .network import proxy_from_store

        # Per-user model if exists
        per_model = get_user_llm_model(self.store, chat_id)
        if per_model:
            model = str(per_model)
            base_url = str(get_user(self.store, chat_id, "llm.base_url") or preset.base_url)
        else:
            model = str(self.store.get("llm.model") or preset.model)
            base_url = str(self.store.get("llm.base_url") or preset.base_url)
        proxy = proxy_from_store(self.store)
        ok, message = await verify_provider(
            LLMProvider(
                id=preset.id,
                base_url=base_url,
                model=model,
                api_key=str(key),
                extra_headers=dict(preset.extra_headers),
            ),
            proxy=proxy,
            reasoning_effort="low" if is_reasoning_model(model) else "",
        )
        await self.send(chat_id, ("✅ " if ok else "❌ ") + escape(message))
        if not ok:
            blocked = any(token in message for token in ("403", "۴۰۳", "Forbidden", "پروکسی"))
            hint = "🔌 پروکسی بده" if blocked else "⚙️ انتخاب سرویس/مدل"
            await self.send(
                chat_id,
                "<i>اگر خطا مربوط به کلید نیست (۴۰۳/Forbidden یا خالی بودن پاسخ)، "
                "اول پروکسی یا مدل را عوض کن.</i>",
                {
                    "inline_keyboard": [
                        [{"text": hint, "callback_data": "llm:proxy" if blocked else "cmd:llm"}],
                        [{"text": "🔁 تست دوباره", "callback_data": "llm:test"}],
                    ]
                },
            )
        check = await check_site(self.store, config_path=self.config_path)
        await self.send(chat_id, check.message(), self._run_keyboard() if check.ok else None)

    async def _cmd_preview(self, chat_id: str) -> None:
        # Block preview until onboarding done
        if is_onboarding_blocked(self.store, chat_id):
            await self.send(chat_id, "⚠️ اول باید کلید هوش مصنوعی و پروفایل را کامل کنی.\n/ start را بزن تا مرحله ۱ را شروع کنیم.")
            await self._enforce_onboarding(chat_id)
            return
        await self.send(
            chat_id,
            "🔎 دارم داخل سایت می‌گردم؛ فهرست پروژه‌ها را می‌خوانم و پروژه‌های مرتبط با تخصصت را باز می‌کنم…",
        )
        try:
            messages = await self._generate_preview(limit=1, chat_id=chat_id)
        except Exception as exc:
            await self.send(chat_id, f"❌ پیش‌نمایش ناموفق بود: {escape(str(exc))}")
            return
        if not messages:
            await self.send(
                chat_id,
                "پروژه‌ای برای پیش‌نمایش پیدا نشد (یا همه با فیلترها/بودجه رد شدند).\n"
                "برای دیدن دلیل رد شدن‌ها و روشن/خاموش کردن قاعده‌ها: /filters",
            )
            return
        mark_preview_ok(self.store, "preview", note=messages[-1][:200])
        for message in messages:
            await self.send(chat_id, message)
        if any("403" in message or "۴۰۳" in message or "Forbidden" in message for message in messages):
            # a blocked IP is not something the user can fix by reading the log
            await self.send(
                chat_id,
                "🔌 سرویس هوش مصنوعی به این اتصال ۴۰۳ داد. با یک پروکسی/VPN محلی درست می‌شود؛ "
                "آدرسش را همین‌جا بده.",
                {
                    "inline_keyboard": [
                        [{"text": "🔌 تنظیم پروکسی", "callback_data": "llm:proxy"}],
                        [{"text": "⚙️ عوض کردن سرویس/مدل", "callback_data": "cmd:llm"}],
                    ]
                },
            )
        if self.store.get("runtime.submit_mode") == "live":
            await self.send(
                chat_id,
                "✅ آزمایش انجام شد؛ حالا /run در حالت واقعی باز است.",
            )

    async def _generate_preview(self, limit: int = 1, chat_id: str | None = None) -> list[str]:
        """Build a throw-away Application and generate real proposals (per-user LLM)."""
        from .app import build_application
        from .config import load_config, reset_secret_cache, Secrets
        from .config import secrets_from_store

        reset_secret_cache()
        cfg = load_config(self.config_path)
        # ── Per-user overrides: LLM key/model isolation ──
        if chat_id is not None:
            try:
                from .per_user import build_user_llm_provider, get_user
                provider = build_user_llm_provider(self.store, chat_id)
                if provider is not None:
                    cfg.llm.provider = provider.id
                    cfg.llm.base_url = provider.base_url
                    cfg.llm.model = provider.model
                    # Also sync pricing per-user
                    strat = get_user(self.store, chat_id, "pricing.strategy")
                    if strat:
                        cfg.pricing["strategy"] = str(strat)
                    hide = get_user(self.store, chat_id, "pricing.hide_price_in_text")
                    if hide is not None:
                        cfg.pricing["hide_price_in_text"] = bool(hide)
                        cfg.pricing["hide_price_in_text"] = bool(hide)
                    # bid_percent per-user
                    bp = get_user(self.store, chat_id, "pricing.bid_percent")
                    if bp is not None:
                        try:
                            cfg.pricing["ratio"] = float(int(bp) / 100)
                        except Exception:
                            pass
                    # Profile per-user override for generator
                    stack = get_user(self.store, chat_id, "profile.stack")
                    if stack:
                        cfg.profile.stack = list(stack)
                    fn = get_user(self.store, chat_id, "profile.full_name")
                    if fn:
                        cfg.profile.full_name = str(fn)
                    first = get_user(self.store, chat_id, "profile.first_name")
                    if first:
                        cfg.profile.first_name = str(first)
                    role = get_user(self.store, chat_id, "profile.role")
                    if role:
                        cfg.profile.role = str(role)
                    # Build secrets with per-user key
                    secrets = Secrets()
                    # Inject per-user key into secrets
                    secrets = secrets_from_store(secrets, self.store)
                    # Override with per-user key
                    from .llm_providers import DEFAULT_PROVIDER
                    pid = provider.id
                    # Ensure secrets has the right key for this provider
                    secrets.llm_keys[pid] = provider.api_key or ""
                    secrets.llm_api_key = provider.api_key
                    # Build app with per-user secrets
                    config_name = str(self.store.get("marketplace.name") or "")
                    if config_name and not cfg.source(config_name):
                        from .config import SourceConfig
                        cfg.sources = [SourceConfig(type=config_name, settings={})]
                    application = build_application(cfg, secrets)
                    # Continue with per-user application
                    try:
                        await application.db.init()
                        name = config_name if config_name in application.sources else None
                        if name is None:
                            name = next(iter(application.sources), None)
                        if not name:
                            raise RuntimeError("هیچ سایت فریلنسری فعالی تنظیم نشده است. /market")
                        source = application.sources[name]
                        projects = await source.fetch_projects()
                        pricing = _pricing_for(cfg)
                        # Inject per-user LLM into pricing if ai_search
                        if getattr(pricing, "name", "") == "ai_search":
                            try:
                                pricing.llm_provider = provider
                                pricing.llm_config = cfg.llm
                            except Exception:
                                pass
                        generator = application.generator
                        delivery = int(cfg.pricing.get("delivery_days", 14))
                        messages: list[str] = []
                        notes = self._discovery_note(source, len(projects))
                        if notes:
                            messages.append(notes)
                        rejected: dict[str, int] = {}
                        filter_preview = application.filter_preview()
                        proposals = 0
                        for project in projects:
                            if proposals >= limit:
                                break
                            decision = filter_preview.evaluate(project)
                            if not decision:
                                key = decision.reason.split("—")[0].strip()[:90]
                                rejected[key] = rejected.get(key, 0) + 1
                                continue
                            # Handle ai_search async if needed
                            price = pricing.price(project, project.budget)
                            if hasattr(pricing, "price_async"):
                                try:
                                    # Try async variant for more accurate price (warn slower)
                                    if pricing.name == "ai_search":
                                        await self.send(chat_id, "🤖 قیمت هوشمند: دارم قیمت بازار را تحلیل می‌کنم (کمی کندتر)…")
                                        price_async = await pricing.price_async(project, project.budget)  # type: ignore
                                        if price_async:
                                            price = price_async
                                except Exception:
                                    pass
                            if price is None:
                                key = "قیمت‌گذاری ممکن نبود (بودجه نامشخص)"
                                rejected[key] = rejected.get(key, 0) + 1
                                continue
                            from .text import parse_days
                            hint = parse_days(project.employer_deadline)
                            bid_delivery = min(delivery, hint) if hint else delivery
                            hide_price = bool(cfg.pricing.get("hide_price_in_text"))
                            proposal = await generator.generate(project, price.amount, bid_delivery, hide_price=hide_price)  # type: ignore[union-attr]
                            deadline_line = (
                                f"⏱ کارفرما: {escape(project.employer_deadline)} | پیشنهاد من: {bid_delivery} روز"
                                if project.employer_deadline
                                else f"⏱ {bid_delivery} روز"
                            )
                            proposals += 1
                            messages.append(
                                "🧪 <b>پیش‌نمایش پیشنهاد</b>\n"
                                f"<b>{escape(project.title)}</b>\n"
                                f"💵 {price.amount:,} تومان | {deadline_line} | 🤖 {escape(proposal.generated_by)}\n"
                                f"🔗 {escape(project.url)}\n\n"
                                f"<pre>{escape(proposal.text)}</pre>"
                            )
                        if proposals == 0 and rejected:
                            lines = ["⚠️ <b>هیچ پروژه‌ای از فیلترها نگذشت</b>"]
                            lines += [f"• {count}× {escape(reason)}" for reason, count in rejected.items()]
                            lines.append("/filters — قاعده‌ها را ببین یا موقتاً خاموش کن.")
                            messages.append("\n".join(lines))
                        reason = str(getattr(generator, "last_llm_error", "") or "")
                        if messages and reason:
                            messages.insert(
                                0,
                                "⚠️ <b>هوش مصنوعی جواب نداد</b> — پیشنهاد زیر از قالب آماده ساخته شد.\n"
                                f"<pre>{escape(reason[:700])}</pre>"
                                "🔧 کلید را با /test تست کن یا در /llm عوضش کن.",
                            )
                        return messages
                    finally:
                        await application.close()
            except Exception as e:
                # Fall through to global path on error
                import logging
                logging.getLogger(__name__).debug("per-user preview path failed, fallback to global: %s", e)
        config_name = str(self.store.get("marketplace.name") or "")
        if config_name and not cfg.source(config_name):
            # the store enabled a source that is not in config.yaml
            from .config import SourceConfig

            cfg.sources = [SourceConfig(type=config_name, settings={})]
        application = build_application(cfg)
        try:
            await application.db.init()
            name = config_name if config_name in application.sources else None
            if name is None:
                name = next(iter(application.sources), None)
            if not name:
                raise RuntimeError("هیچ سایت فریلنسری فعالی تنظیم نشده است. /market")
            source = application.sources[name]
            projects = await source.fetch_projects()
            pricing = _pricing_for(cfg)
            generator = application.generator
            delivery = int(cfg.pricing.get("delivery_days", 14))
            messages: list[str] = []

            notes = self._discovery_note(source, len(projects))
            if notes:
                messages.append(notes)

            rejected: dict[str, int] = {}
            filter_preview = application.filter_preview()
            proposals = 0
            for project in projects:
                if proposals >= limit:
                    break
                decision = filter_preview.evaluate(project)
                if not decision:
                    key = decision.reason.split("—")[0].strip()[:90]
                    rejected[key] = rejected.get(key, 0) + 1
                    continue
                price = pricing.price(project, project.budget)
                if price is None:
                    key = "قیمت‌گذاری ممکن نبود (بودجه نامشخص)"
                    rejected[key] = rejected.get(key, 0) + 1
                    continue
                from .text import parse_days

                hint = parse_days(project.employer_deadline)
                bid_delivery = min(delivery, hint) if hint else delivery
                proposal = await generator.generate(project, price.amount, bid_delivery)  # type: ignore[union-attr]
                deadline_line = (
                    f"⏱ کارفرما: {escape(project.employer_deadline)} | پیشنهاد من: {bid_delivery} روز"
                    if project.employer_deadline
                    else f"⏱ {bid_delivery} روز"
                )
                proposals += 1
                messages.append(
                    "🧪 <b>پیش‌نمایش پیشنهاد</b>\n"
                    f"<b>{escape(project.title)}</b>\n"
                    f"💵 {price.amount:,} تومان | {deadline_line} | 🤖 {escape(proposal.generated_by)}\n"
                    f"🔗 {escape(project.url)}\n\n"
                    f"<pre>{escape(proposal.text)}</pre>"
                )
            if proposals == 0 and rejected:
                lines = ["⚠️ <b>هیچ پروژه‌ای از فیلترها نگذشت</b>"]
                lines += [f"• {count}× {escape(reason)}" for reason, count in rejected.items()]
                lines.append("/filters — قاعده‌ها را ببین یا موقتاً خاموش کن.")
                messages.append("\n".join(lines))
            reason = str(getattr(generator, "last_llm_error", "") or "")
            if messages and reason:
                # The template fallback is silent by design; the *reason* must not be.
                messages.insert(
                    0,
                    "⚠️ <b>هوش مصنوعی جواب نداد</b> — پیشنهاد زیر از قالب آماده ساخته شد.\n"
                    f"<pre>{escape(reason[:700])}</pre>"
                    "🔧 کلید را با /test تست کن یا در /llm عوضش کن.",
                )
            return messages
        finally:
            await application.close()

    @staticmethod
    def _discovery_note(source: Any, project_count: int) -> str:
        """Tell the user what the agent really did — «AI searched» must be verifiable."""
        result = getattr(source, "last_discovery", None)
        error = str(getattr(source, "last_discovery_error", "") or "")
        if result is not None and project_count:
            return (
                "🧭 <b>جست‌وجوی هوشمند در سایت</b>\n"
                f"• {result.pages_read} صفحه خوانده شد، {len(result.visited)} آدرس باز شد\n"
                f"• {project_count} پروژه پیدا شد ({result.llm_calls} درخواست به مدل)\n"
                "<i>مدل خودش فهرست‌ها و صفحه‌ی پروژه‌ها را با حساب تو خوانده است.</i>"
            )
        if error:
            # a failed search must not be reported as «nothing found»
            from .sources.discovery import friendly_llm_error

            return (
                "⚠️ <b>جست‌وجوی هوشمند انجام نشد</b> — با سلکتورهای ثابت سایت ادامه دادم.\n"
                f"{escape(friendly_llm_error(error))}\n"
                "<i>جزئیات کامل در لاگ است؛ /test یا /llm را هم ببین.</i>"
            )
        if result is not None:
            return (
                "🧭 <b>جست‌وجوی هوشمند در سایت</b>\n"
                f"• {result.pages_read} صفحه خوانده شد، {result.llm_calls} درخواست به مدل\n"
                "• صفحه‌ها را خواندم ولی پروژه‌ی مرتبطی پیدا نشد."
            )
        return ""

    # ----------------------------------------------------------------- flows
    async def _start_flow(
        self,
        chat_id: str,
        name: str,
        questions: Sequence[Question],
        *,
        mark_complete: bool = False,
    ) -> None:
        # Keep *all* questions: later answers can make an earlier question
        # relevant (e.g. choosing "cookie" reveals the cookie field) and
        # _advance skips whatever does not apply at that moment.
        if not any(q.applicable(self.store) for q in questions):
            await self.send(chat_id, "همه‌چیز همین حالا تنظیم است ✅")
            return
        self._flows[chat_id] = Flow(
            name=name, questions=list(questions), index=0, mark_complete=mark_complete
        )
        await self._advance(chat_id)

    async def _advance(self, chat_id: str) -> None:
        flow = self._flows.get(chat_id)
        if flow is None:
            return
        while flow.index < len(flow.questions) and not flow.questions[flow.index].applicable(self.store):
            flow.index += 1
        if flow.index >= len(flow.questions):
            await self._finish_flow(chat_id, flow)
            return
        question = flow.questions[flow.index]
        text = self._question_text(flow, question)
        keyboard = self._choice_keyboard(flow, question)
        await self.send(chat_id, text, keyboard)

    def _visible_count(self, flow: Flow) -> tuple[int, int]:
        """(position, total) of the current question among the ones that apply now."""
        visible = [q for q in flow.questions if q.applicable(self.store)]
        position = visible.index(flow.questions[flow.index]) + 1 if visible else 1
        return position, len(visible)

    def _question_text(self, flow: Flow, question: Question) -> str:
        position, total = self._visible_count(flow)
        step = f"{position}/{total} · {STAGE_TITLES.get(question.stage, '')}"
        lines = [f"<b>{escape(question.text(self.store))}</b>", f"<i>{step}</i>"]
        help_text = question.help_text(self.store)
        if help_text:
            lines.append(escape(help_text))
        choices = question.choice_list(self.store)
        if choices:
            for i, choice in enumerate(choices, 1):
                line = f"{i}) {escape(choice.label)}"
                if choice.description:
                    line += f" — <i>{escape(choice.description)}</i>"
                lines.append(line)
            if question.allow_custom:
                lines.append("یا لحن دلخواهت را با جمله‌ی خودت بنویس.")
        elif question.optional:
            lines.append("<i>برای رد کردن: /skip</i>")
        return "\n".join(lines)

    def _choice_keyboard(self, flow: Flow, question: Question) -> dict[str, Any] | None:
        choices = question.choice_list(self.store)
        rows: list[list[dict[str, str]]] = []
        if choices:
            for i, choice in enumerate(choices):
                rows.append([{"text": choice.label, "callback_data": f"ch:{flow.index}:{i}"}])
        rows.append([{"text": "لغو", "callback_data": "cmd:cancel"}])
        return {"inline_keyboard": rows}

    async def handle_text(self, chat_id: str, text: str) -> None:
        flow = self._flows.get(chat_id)
        if flow is None or flow.current is None:
            await self.send(chat_id, "دستور را نمی‌شناسم.\n\n" + HELP_TEXT, self._menu_keyboard())
            return
        await self._consume_answer(chat_id, text)

    async def _consume_answer(self, chat_id: str, raw: Any) -> None:
        flow = self._flows.get(chat_id)
        if flow is None or flow.current is None:
            return
        question = flow.current
        try:
            value = parse_answer(question, raw, self.store)
        except QuestionError as exc:
            await self.send(chat_id, f"⚠️ {escape(str(exc))}")
            return

        # ── Per-user LLM key live verification ──
        store_key = question.store_key(self.store)
        if store_key.startswith(f"users.{chat_id}.llm.api_key") or store_key == f"users.{chat_id}.llm.api_key":
            # value is the raw API key
            provider_id = str(self.store.get(f"users.{chat_id}.llm.provider") or "groq").strip().lower()
            # For provider that doesn't require key (ollama), skip verification
            from .llm_providers import get_preset, verify_user_key
            try:
                preset = get_preset(provider_id)
                if preset.requires_key:
                    await self.send(chat_id, "⏳ در حال بررسی کلید… (یک درخواست آزمایشی به سرویس می‌فرستم)")
                    # Determine model to verify against: use currently selected model or preset default
                    model = str(self.store.get(f"users.{chat_id}.llm.model") or preset.model)
                    proxy = str(self.store.get("network.proxy") or self.store.get("llm.proxy") or "")
                    ok, msg = await verify_user_key(provider_id, str(value), model, proxy=proxy)
                    if not ok:
                        # Spec phrase: "API key معتبر نیست" must be reported and block progress
                        await self.send(
                            chat_id,
                            f"❌ <b>API key معتبر نیست</b>\n{escape(msg)}\n\nلطفاً کلید صحیح را دوباره بفرست (یا سرویس را با /llm عوض کن).",
                        )
                        return
                    # Success — now store key and mark verified
                    try:
                        apply_answer(self.store, question, value)
                    except Exception as exc:
                        log.exception("Could not save setting %s", store_key)
                        await self.send(chat_id, f"❌ ذخیره نشد: {escape(str(exc))}")
                        return
                    set_user_llm_verified(self.store, chat_id, True)
                    # Also persist model explicitly if not yet set (apply will handle)
                    await self.send(chat_id, f"✅ {escape(msg)}\nحالا مدل را انتخاب کن — این مدل دقیقاً برای تو ذخیره می‌شود.")
                    self._log(f"chat {chat_id}: verified LLM key for {provider_id} model {model}")
                    flow.index += 1
                    await self._advance(chat_id)
                    return
            except Exception as e:
                # Fallback: if verification infrastructure fails, still store but warn
                log.warning("Verification failed internally: %s", e)

        # Verify model choice too — test key+model combo (catches deprecated qwen/qwen3-32b etc)
        if store_key.startswith(f"users.{chat_id}.llm.model"):
            provider_id = str(self.store.get(f"users.{chat_id}.llm.provider") or "groq").strip().lower()
            api_key = str(self.store.get(f"users.{chat_id}.llm.api_key") or self.store.get(f"users.{chat_id}.llm.keys.{provider_id}") or "")
            from .llm_providers import get_preset, verify_user_key
            try:
                preset = get_preset(provider_id)
                if preset.requires_key and not api_key:
                    await self.send(chat_id, "⚠️ هنوز کلید API ثبت نشده؛ اول کلید را بفرست یا سرویس بدون کلید (Ollama) انتخاب کن.")
                    return
                chosen_model = str(value).strip()
                if chosen_model:
                    proxy = str(self.store.get("network.proxy") or self.store.get("llm.proxy") or "")
                    await self.send(chat_id, f"⏳ در حال تست مدل <code>{escape(chosen_model)}</code> با کلید تو…")
                    ok, msg = await verify_user_key(provider_id, api_key, chosen_model, proxy=proxy)
                    if not ok:
                        if "مدل یافت نشد" in msg or "model_not_found" in msg.lower() or "does not exist" in msg.lower():
                            await self.send(chat_id, f"⚠️ <b>مدل در دسترس نیست</b>\n{escape(msg)}\n\nمدل دیگری از لیست انتخاب کن یا با /llm سرویس را عوض کن.")
                        else:
                            await self.send(chat_id, f"❌ <b>API key معتبر نیست</b>\n{escape(msg)}\n\nکلید را دوباره چک کن یا مدل دیگری امتحان کن.")
                        return
                    try:
                        apply_answer(self.store, question, value)
                    except Exception as exc:
                        await self.send(chat_id, f"❌ ذخیره نشد: {escape(str(exc))}")
                        return
                    from .per_user import set_user_llm_verified
                    set_user_llm_verified(self.store, chat_id, True)
                    await self.send(chat_id, f"✅ {escape(msg)}\nاین مدل دقیقاً برای تو ذخیره شد و همه درخواست‌ها با آن انجام می‌شود.")
                    self._log(f"chat {chat_id}: verified LLM model {chosen_model} for {provider_id}")
                    flow.index += 1
                    await self._advance(chat_id)
                    return
            except Exception as e:
                log.warning("Model verification failed internally: %s", e)
                # fallback to normal storage

        # Per-user profile skip: parse returns KEEP, but we need to store "skip" explicitly to mark onboarding done
        from .onboarding import KEEP as _KEEP
        if value is _KEEP and store_key.startswith(f"users.{chat_id}.profile.source"):
            # raw was skip-like
            normalized = str(raw or "").strip().lower()
            if normalized in {"skip", "/skip", "رد", "بعدا", "-"} or "skip" in normalized or raw == "skip":
                try:
                    from .per_user import set_user
                    set_user(self.store, chat_id, "profile.source", "skip")
                    set_user(self.store, chat_id, "onboarding.profile_skipped", True)
                    self._log(f"chat {chat_id}: profile skip stored")
                    flow.index += 1
                    await self._advance(chat_id)
                    return
                except Exception as e:
                    log.warning("Could not store skip: %s", e)

        try:
            apply_answer(self.store, question, value)
        except Exception as exc:
            log.exception("Could not save setting %s", question.store_key(self.store))
            await self.send(
                chat_id,
                f"❌ ذخیره نشد: {escape(str(exc))}\n"
                "دسترسی نوشتن به data/settings.json را بررسی کن و دوباره تلاش کن.",
            )
            return
        if question.store_key(self.store) == "telegram.chat_ids":
            save_chat_id(self.store, chat_id)
        self._log(f"chat {chat_id}: saved {question.store_key(self.store)}")
        # Per-user profile pending import handling
        sk = question.store_key(self.store)
        if sk.endswith("profile.resume_path") or sk.endswith("profile.source_url"):
            await self._import_profile_from_pending(chat_id)
        flow.index += 1
        await self._advance(chat_id)

    async def _finish_flow(self, chat_id: str, flow: Flow) -> None:
        self._flows.pop(chat_id, None)
        # ── New per-user onboarding flows ──
        if flow.name == "user_llm":
            # After LLM provider/key/model done, proceed to profile step
            if not is_llm_onboarding_done(self.store, chat_id):
                await self.send(chat_id, "⚠️ هنوز مرحله‌ی هوش مصنوعی کامل نشده — دوباره /start بزن.")
                return
            await self.send(chat_id, "✅ <b>کلید تأیید شد و مدل ذخیره شد.</b>\nاین مدل دقیقاً برای تمام درخواست‌های تو استفاده خواهد شد.")
            # Now enforce profile step
            if is_profile_onboarding_done(self.store, chat_id):
                await self.send(chat_id, "✅ پروفایل هم کامل است — می‌ریم به داشبورد.")
                await self._show_dashboard(chat_id)
                # Optionally continue to marketplace/tone/pricing if desired
                return
            await self._start_profile_onboarding(chat_id)
            return
        if flow.name == "user_profile":
            err = str(self.store.get(f"users.{chat_id}.profile.import_error") or self.store.get("profile.import_error") or "")
            if err:
                await self.send(chat_id, f"⚠️ پروفایل با فایل کامل نشد: {escape(err)}\nدوباره /profile را بزن یا دستی وارد کن.")
                return
            from .per_user import is_marketplace_onboarding_done, is_profile_onboarding_done
            if not is_profile_onboarding_done(self.store, chat_id):
                await self.send(chat_id, "✅ پروفایل ذخیره شد.")
                await self._show_dashboard(chat_id)
                return
            if is_marketplace_onboarding_done(self.store, chat_id):
                await self.send(chat_id, "✅ پروفایل ذخیره شد.")
                await self._show_dashboard(chat_id)
                return
            await self.send(chat_id, "✅ پروفایل ذخیره شد.\nحالا تنظیمات تکمیلی (سایت، لحن، قیمت‌گذاری) را انجام می‌دهیم.")
            try:
                from .onboarding import user_marketplace_questions, user_style_questions
                qs = []
                qs.extend(user_marketplace_questions(self.store, chat_id))
                qs.extend(user_style_questions(self.store, chat_id))
                qs.extend(user_pricing_questions(self.store, chat_id))
                await self.send(
                    chat_id,
                    "📋 <b>مرحله ۳ — سایت، لحن و قیمت‌گذاری</b>\n"
                    "سایت فریلنسری، لحن پیشنهادات و نحوه قیمت‌گذاری را انتخاب کن.\n"
                    "💡 گزینه هوشمند قیمت‌گذاری (AI جستجوی بازار) کندتر است اما دقیق‌تر — در صورت انتخاب هشدار می‌دهیم.\n"
                    "💡 گزینه «نیازی نیست قیمت را در متن بنویسی» فقط قیمت را در فرم سقف بودجه پر می‌کند.",
                )
                await self._start_flow(chat_id, "user_market_style_pricing", qs, mark_complete=False)
            except Exception as e:
                log.warning("Could not start step 3: %s", e)
                await self._show_dashboard(chat_id)
            return
        if flow.name == "user_market_style_pricing":
            await self.send(chat_id, "✅ تنظیمات تکمیلی ذخیره شد.")
            await self._show_dashboard(chat_id)
            return
        if flow.mark_complete:
            mark_setup_complete(self.store, complete=not missing_steps(self.store, include_recommended=False))
            # referral qualification happens async
            try:
                await self._maybe_qualify_referral(chat_id)
            except Exception:
                pass
            await self.send(
                chat_id,
                "✅ تنظیمات ذخیره شد. از این به بعد همه‌چیز از همین تلگرام قابل تغییر است.\n\n"
                + status_text(self.store)
                + "\n\n"
                + self._menu_hint(),
                self._menu_keyboard(),
            )
            await self._post_setup(chat_id)
            return
        if flow.name == "profile":
            error = str(self.store.get("profile.import_error") or "")
            if error:
                await self.send(
                    chat_id,
                    "⚠️ <b>پروفایل با فایل کامل نشد</b> — اطلاعات قبلی دست‌نخورده ماند.\n"
                    f"دلیل: {escape(error)}\n\n"
                    "• دوباره فایل/لینک بفرست: /profile\n"
                    "• یا دستی وارد کن: /profile → «ورود دستی»",
                )
                return
            await self.send(chat_id, "✅ پروفایل به‌روز شد.\n\n" + status_text(self.store))
            return
        if flow.name == "style":
            await self.send(
                chat_id,
                "🎭 به‌روز شد.\n"
                + escape(tone_summary(self.store))
                + f"\n💰 قیمت: {bid_percent(self.store)}٪ بودجه‌ی کارفرما"
                + "\n\nبرای دیدن نتیجه: /preview",
            )
            return
        if flow.name == "proxy":
            await self._apply_proxy_and_check(chat_id)
            return
        if flow.name == "mode":
            live = str(self.store.get("runtime.submit_mode")) == "live"
            if live and not preview_is_fresh(self.store):
                await self.send(
                    chat_id,
                    "🔴 حالت واقعی ثبت شد — ولی تا یک آزمایش موفق، /run واقعی را باز نمی‌کند.\n"
                    "همین حالا /preview را بزن.",
                )
                return
            await self.send(chat_id, "✅ حالت ذخیره شد.")
            return
        if flow.name == "market":
            # right after the account is entered: prove the login and offer «شروع فعالیت»
            await self.send(chat_id, "✅ حساب ذخیره شد.")
            await self._maybe_qualify_referral(chat_id)
            await self._post_setup(chat_id)
            return
        await self.send(chat_id, "✅ ذخیره شد.\n\n" + status_text(self.store))

    async def _post_setup(self, chat_id: str) -> None:
        """Right after setup: prove the login works, then offer «شروع فعالیت»."""
        state = readiness(self.store)
        if not state["credentials"]:
            return
        await self.send(chat_id, "🔐 حالا ورود به حساب سایت را بررسی می‌کنم…")
        check = await check_site(self.store, config_path=self.config_path)
        await self.send(chat_id, check.message())
        if not check.ok:
            return
        await self.send(
            chat_id,
            "همه‌چیز آماده است. با زدن دکمه‌ی زیر، رصد پروژه‌ها و ارسال پیشنهاد شروع می‌شود.",
            {
                "inline_keyboard": [
                    [{"text": "🚀 شروع فعالیت", "callback_data": "run:ask"}],
                    [
                        {"text": "🧪 پیش‌نمایش", "callback_data": "cmd:preview"},
                        {"text": "📊 وضعیت", "callback_data": "cmd:status"},
                    ],
                ]
            },
        )


    # ── referral & start with deep link ──────────────────────────
    def _bot_username(self) -> str:
        # Correct bot is @BidPilotIranbot — not @BidPilotBot
        for k in ("telegram.bot_username", "telegram.username", "bot.username", "telegram.bot_id"):
            v = self.store.get(k)
            if v:
                # handle cases where value is "BidPilotIranbot" or "https://t.me/..."
                s = str(v).strip().lstrip("@")
                if "t.me/" in s:
                    s = s.split("t.me/")[-1].split("?")[0].split("/")[0]
                if s:
                    return s
        # Fallback: canonical production bot
        return "BidPilotIranbot"

    async def _ensure_bot_username_cached(self) -> None:
        """If store has no username, try to learn it live from Telegram (getMe)."""
        # Already have it?
        for k in ("telegram.bot_username", "telegram.username", "bot.username"):
            if self.store.get(k):
                return
        try:
            me = await self.api.get_me()  # type: ignore[attr-defined]
            uname = (me or {}).get("username")
            if uname:
                # persist so future referrals use correct link even before next restart
                try:
                    self.store.set("telegram.bot_username", str(uname).lstrip("@"))
                except Exception:
                    pass
        except Exception:
            pass

    async def _handle_referral_start(self, chat_id: str, rest: str) -> bool:
        rest = (rest or "").strip()
        if not rest.lower().startswith("ref_"):
            return False
        code = rest[4:].strip().upper()
        if not code:
            return False
        try:
            from .db import Database
            from .subscriptions.referrals import ReferralManager

            db_url = self.store.get("database_url") or "sqlite+aiosqlite:///data/bidpilot.sqlite3"
            db = Database(str(db_url))
            await db.init()
            async with db.session_factory() as s:
                rm = ReferralManager(s, bot_username=self._bot_username())
                ok = await rm.record_referral(chat_id, code)
                await s.commit()
                if ok:
                    await self.send(chat_id, f"🎁 خوش آمدید! شما با لینک دوست وارد شدید — کد <code>{code}</code> ثبت شد.\nبا تکمیل اتصال بازار، دوست‌تان پاداش می‌گیرد و شما هم می‌توانید دوستان خود را دعوت کنید: /invite")
                    # also ensure invitee has code
                    async with db.session_factory() as s2:
                        rm2 = ReferralManager(s2, bot_username=self._bot_username())
                        await rm2.ensure_code(chat_id)
                        await s2.commit()
                    return True
            await db.dispose()
        except Exception as exc:
            import logging
            logging.getLogger(__name__).debug("referral start failed: %s", exc)
        return False

    async def _maybe_qualify_referral(self, chat_id: str) -> None:
        """Call after setup/market complete — if invitee finishes, inviter gets milestone."""
        try:
            from .db import Database
            from .subscriptions.referrals import ReferralManager

            db_url = self.store.get("database_url") or "sqlite+aiosqlite:///data/bidpilot.sqlite3"
            db = Database(str(db_url))
            await db.init()
            async with db.session_factory() as s:
                rm = ReferralManager(s, bot_username=self._bot_username())
                grants = await rm.mark_setup_complete(chat_id)
                await s.commit()
                # notify inviter if milestone hit (best-effort)
                for g in grants:
                    # we don't know inviter chat to push — log only; they will see on /invite
                    pass
            await db.dispose()
        except Exception:
            pass

    async def _cmd_start(self, chat_id: str, rest: str = "") -> None:
        await self._ensure_bot_username_cached()
        if rest:
            await self._handle_referral_start(chat_id, rest)
        await self._ensure_account(chat_id)
        save_chat_id(self.store, chat_id)
        # ── Gate: onboarding must complete before dashboard ──
        if is_onboarding_blocked(self.store, chat_id):
            await self._enforce_onboarding(chat_id)
            return
        # Fallback to legacy global readiness check for old installs (e.g. tests expecting token question)
        try:
            from .onboarding import readiness, missing_steps
            state = readiness(self.store)
            if not state["setup_completed"] or missing_steps(self.store, include_recommended=False):
                # If per-user done but global missing, skip legacy setup and show dashboard
                if is_llm_onboarding_done(self.store, chat_id) and is_profile_onboarding_done(self.store, chat_id):
                    pass
                else:
                    await self.send(
                        chat_id,
                        "✨ <b>به BidPilot خوش آمدی 👋</b>\n"
                        "<i>دستیار هوشمند پیشنهاددهی — بدون .env، همه در تلگرام</i>\n"
                        "─" * 28 + "\n"
                        "بیا در چند سؤال همه‌چیز را تنظیم کنیم — حساب شخصی تو ساخته می‌شود و سقف‌هات فعال می‌شود.\n"
                        "هر وقت خواستی با /cancel متوقف شوی.",
                    )
                    await self._start_flow(chat_id, "setup", all_questions(self.store), mark_complete=True)
                    return
        except Exception:
            pass
        try:
            from .db import Database
            from .subscriptions.manager import SubscriptionManager
            from .subscriptions.referrals import ReferralManager
            from .subscriptions.zarinpal import ZarinpalConfig

            db_url = self.store.get("database_url") or "sqlite+aiosqlite:///data/bidpilot.sqlite3"
            db = Database(str(db_url))
            await db.init()
            async with db.session_factory() as s:
                mgr = SubscriptionManager(s, zarinpal_config=ZarinpalConfig(merchant_id=str(self.store.get("zarinpal.merchant_id") or "")))
                user, plan = await mgr.effective_plan(chat_id)
                title = plan.title if plan else None
                rm = ReferralManager(s, bot_username=self._bot_username())
                await rm.ensure_code(chat_id)
                await s.commit()
            await db.dispose()
        except Exception:
            title = None
        from .bot_ui import main_menu_keyboard, main_menu_text

        first = None
        try:
            first = str(self.store.get("profile.display_name") or "").split()[0]
        except Exception:
            pass
        await self.send(chat_id, main_menu_text(first_name=first, plan_title=title), main_menu_keyboard())

    async def _cmd_pick_gateway(self, chat_id: str, plan_id: str) -> None:
        from .bot_ui import gateway_picker_keyboard, gateway_picker_text
        from .subscriptions.plans import get_plan

        plan = get_plan(plan_id)
        if plan.is_free():
            await self.send(chat_id, "پلن رایگان نیازی به پرداخت ندارد — همین حالا فعال است ✅")
            return
        await self.send(chat_id, gateway_picker_text(plan), gateway_picker_keyboard(plan.id))

    async def _cmd_invite(self, chat_id: str) -> None:
        await self._ensure_bot_username_cached()
        try:
            from .db import Database
            from .subscriptions.referrals import ReferralManager, MILESTONES

            db_url = self.store.get("database_url") or "sqlite+aiosqlite:///data/bidpilot.sqlite3"
            db = Database(str(db_url))
            await db.init()
            bot_user = self._bot_username()
            async with db.session_factory() as s:
                rm = ReferralManager(s, bot_username=bot_user)
                stats = await rm.stats(chat_id)
                await s.commit()
            await db.dispose()
            from .bot_ui import referral_card, referral_keyboard

            txt = referral_card(
                code=stats["code"],
                link=stats["link"],
                bot_username=bot_user,
                total_qualified=stats["total_qualified"],
                pending=stats["pending"],
                next_milestone=stats["next_milestone"],
                recent=stats["recent"],
                milestones=MILESTONES,
            )
            await self.send(chat_id, txt, referral_keyboard(stats["link"], stats["code"]))
        except Exception as exc:
            await self.send(chat_id, f"⚠️ خطا در نمایش دعوت: {exc}")

    async def _cmd_ref_copy(self, chat_id: str, code: str) -> None:
        try:
            from .db import Database
            from .subscriptions.referrals import ReferralManager

            db_url = self.store.get("database_url") or "sqlite+aiosqlite:///data/bidpilot.sqlite3"
            db = Database(str(db_url))
            await db.init()
            async with db.session_factory() as s:
                rm = ReferralManager(s, bot_username=self._bot_username())
                _, link = await rm.invite_link(chat_id)
                await s.commit()
            await db.dispose()
            await self.send(chat_id, f"🔗 لینک دعوت شما:\n<code>{link}</code>\n\nروی لینک بزنید و نگه دارید تا کپی شود، یا از دکمه اشتراک استفاده کنید.", {"inline_keyboard": [[{"text": "📤 اشتراک در تلگرام", "url": f"https://t.me/share/url?url={link}"}]]})
        except Exception as exc:
            await self.send(chat_id, f"⚠️ {exc}")

    # ── Subscription & ZarinPal ────────────────────────────────────────
    async def _ensure_account(self, chat_id: str) -> None:
        """Create per-user account row if DB is reachable; never fail the UX."""
        try:
            from .db import Database
            from .subscriptions.manager import SubscriptionManager
            from .subscriptions.zarinpal import ZarinpalConfig

            db_url = self.store.get("database_url") or "sqlite+aiosqlite:///data/bidpilot.sqlite3"
            # settings_store may hold zarinpal merchant
            merchant = str(self.store.get("zarinpal.merchant_id") or self.store.get("payments.zarinpal_merchant_id") or "")
            sandbox = bool(self.store.get("zarinpal.sandbox"))
            cfg = ZarinpalConfig(merchant_id=merchant, sandbox=sandbox)
            db = Database(str(db_url))
            await db.init()
            async with db.session_factory() as s:
                mgr = SubscriptionManager(s, zarinpal_config=cfg)
                await mgr.get_or_create_user(chat_id)
                await s.commit()
            await db.dispose()
        except Exception:
            log.debug("ensure_account failed (DB not ready yet)", exc_info=True)

    async def _cmd_plans(self, chat_id: str) -> None:
        from .bot_ui import plans_keyboard, plans_overview_text

        await self._ensure_account(chat_id)
        await self.send(chat_id, plans_overview_text(), plans_keyboard())

    async def _cmd_plan_detail(self, chat_id: str, plan_id: str) -> None:
        from .bot_ui import plan_detail_keyboard, plan_detail_text
        from .subscriptions.plans import get_plan

        plan = get_plan(plan_id)
        await self.send(chat_id, plan_detail_text(plan), plan_detail_keyboard(plan))

    async def _cmd_buy(self, chat_id: str, plan_id: str, gateway: str | None = None) -> None:
        from .subscriptions.plans import get_plan

        plan = get_plan(plan_id.strip().lower())
        if plan.is_free():
            await self.send(chat_id, "پلن رایگان فعال است — نیاز به پرداخت ندارد. ✅\nبرای سقف بالاتر یک پلن پولی بخرید: /plans")
            return
        gw = (gateway or "zibal").lower()
        # friendly label
        try:
            from .subscriptions.gateways import GATEWAYS_META as _GM
            gname = _GM.get(gw, _GM["zarinpal"]).label_fa
        except Exception:
            gname = gw
        await self.send(chat_id, f"⏳ در حال ساخت لینک پرداخت <b>{escape(gname)}</b> برای <b>{escape(plan.title)}</b> ({escape(plan.formatted_price)}) ...")
        try:
            from .db import Database
            from .subscriptions.manager import SubscriptionManager
            from .subscriptions.zarinpal import ZarinpalConfig

            db_url = self.store.get("database_url") or "sqlite+aiosqlite:///data/bidpilot.sqlite3"
            merchant = str(self.store.get("zarinpal.merchant_id") or self.store.get("payments.zarinpal_merchant_id") or "")
            sandbox = bool(self.store.get("zarinpal.sandbox"))
            cb = str(self.store.get("zarinpal.callback_url") or "https://bidpilot.local/payment/callback")
            cfg = ZarinpalConfig(merchant_id=merchant, sandbox=sandbox, callback_url=cb)
            db = Database(str(db_url))
            await db.init()
            async with db.session_factory() as s:
                mgr = SubscriptionManager(s, zarinpal_config=cfg)
                payment, url = await mgr.create_payment(chat_id, plan.id, gateway=gw)
                await s.commit()
            await db.dispose()
            from .bot_ui import payment_created_text, payment_keyboard

            await self.send(chat_id, payment_created_text(plan, url, payment.authority or "", gateway=gw), payment_keyboard(payment.authority or "", url, gateway=gw))
        except Exception as exc:
            log.exception("buy failed")
            await self.send(chat_id, f"❌ ساخت پرداخت ناموفق: {escape(str(exc))}\n\nراهنما: /zarinpal")

    async def _cmd_verify(self, chat_id: str, authority: str) -> None:
        await self.send(chat_id, f"⏳ در حال بررسی پرداخت <code>{escape(authority)}</code> در زرین‌پال ...")
        try:
            from .db import Database
            from .subscriptions.manager import SubscriptionManager
            from .subscriptions.zarinpal import ZarinpalConfig

            db_url = self.store.get("database_url") or "sqlite+aiosqlite:///data/bidpilot.sqlite3"
            merchant = str(self.store.get("zarinpal.merchant_id") or self.store.get("payments.zarinpal_merchant_id") or "")
            sandbox = bool(self.store.get("zarinpal.sandbox"))
            cfg = ZarinpalConfig(merchant_id=merchant, sandbox=sandbox)
            db = Database(str(db_url))
            await db.init()
            async with db.session_factory() as s:
                mgr = SubscriptionManager(s, zarinpal_config=cfg)
                payment, sub = await mgr.verify_payment(authority)
                await s.commit()
                # refresh plan for message
                from .subscriptions.plans import get_plan as _gp

                plan = _gp(payment.plan_id)
                from .bot_ui import payment_verified_text

                await self.send(chat_id, payment_verified_text(plan, payment.ref_id or "—", sub.expires_at if sub else None))
                # referral bonus: if this user was invited, give inviter +7 days
                try:
                    from .subscriptions.referrals import ReferralManager

                    rm2 = ReferralManager(s, bot_username=self._bot_username())
                    bonus = await rm2.mark_purchase(chat_id, plan.id)
                    await s.commit()
                    if bonus:
                        days = bonus[0].get("days", 7)
                        await self.send(chat_id, f"🎁 پاداش دعوت برای معرف شما ثبت شد: +{days} روز")
                except Exception:
                    pass
                await self._cmd_account(chat_id)
            await db.dispose()
        except Exception as exc:
            await self.send(chat_id, f"❌ تأیید ناموفق: {escape(str(exc))}\nکد را چک کن یا دوباره /plans → خرید بزن.")

    async def _cmd_account(self, chat_id: str) -> None:
        try:
            from .db import Database
            from .subscriptions.manager import SubscriptionManager
            from .subscriptions.zarinpal import ZarinpalConfig

            db_url = self.store.get("database_url") or "sqlite+aiosqlite:///data/bidpilot.sqlite3"
            merchant = str(self.store.get("zarinpal.merchant_id") or "")
            cfg = ZarinpalConfig(merchant_id=merchant)
            db = Database(str(db_url))
            await db.init()
            async with db.session_factory() as s:
                mgr = SubscriptionManager(s, zarinpal_config=cfg)
                user, plan = await mgr.effective_plan(chat_id)
                # per-user usage from bids table if available
                from .repositories import Repository

                repo = Repository(s)
                ph = await repo.bids_per_hour(user_chat_id=chat_id)
                pd = await repo.bids_per_day(user_chat_id=chat_id)
                pm = await repo.bids_per_month(user_chat_id=chat_id)
                from .bot_ui import status_card, progress_bar

                exp = user.plan_expires_at.strftime("%Y/%m/%d") if user and user.plan_expires_at else "—"
                usage = {"per_hour": ph, "per_day": pd}
                card = (
                    f"<b>👤 حساب {escape(str(chat_id))}</b>\n"
                    f"{plan.badge} <b>{escape(plan.title)}</b> — {escape(plan.tagline)}\n"
                    f"💳 {escape(plan.formatted_price)} · {escape(plan.price_per_day)}\n"
                    f"⏰ انقضا: <b>{exp}</b>\n"
                    f"{'─'*28}\n"
                    f"<b>مصرف</b>\n"
                    f"ساعتی: {ph}/{plan.limits.max_bids_per_hour} {progress_bar(ph, plan.limits.max_bids_per_hour)}\n"
                    f"روزانه: {pd}/{plan.limits.max_bids_per_day} {progress_bar(pd, plan.limits.max_bids_per_day)}\n"
                    f"ماهانه: {pm}/{plan.limits.max_bids_per_month if plan.limits.max_bids_per_month < 999999 else '∞'}\n"
                    f"فاصله ایمن: {plan.limits.min_seconds_between_bids}s\n"
                )
                await self.send(chat_id, status_card(card, plan, usage), self._menu_keyboard())
            await db.dispose()
        except Exception as exc:
            log.debug("account failed", exc_info=True)
            await self.send(chat_id, f"⚠️ نمایش حساب فعلاً ممکن نیست: {escape(str(exc))}")

    async def _cmd_zarinpal_guide(self, chat_id: str) -> None:
        from .subscriptions.zarinpal import ZarinpalClient

        guide = ZarinpalClient.setup_guide()
        # also surface current merchant status
        merchant = str(self.store.get("zarinpal.merchant_id") or "")
        sandbox = bool(self.store.get("zarinpal.sandbox"))
        status = f"\n\n<b>وضعیت فعلی:</b>\n• merchant_id: {'<code>'+escape(merchant)+'</code>' if merchant else '<i>تنظیم نشده — پرداخت شبیه‌سازی می‌شود</i>'}\n• حالت: {'سندباکس (تست)' if sandbox else 'واقعی (Production)'}\n\n<i>برای تنظیم: <code>/settings</code> → zarinpal.merchant_id یا دستور: <code>bidpilot settings set zarinpal.merchant_id &lt;MERCHANT&gt;</code></i>"
        await self.send(chat_id, guide + status, {"inline_keyboard": [[{"text": "💳 دیدن پلن‌ها", "callback_data": "cmd:plans"}]]})

    async def _cmd_payments(self, chat_id: str) -> None:
        try:
            from .db import Database
            from .subscriptions.manager import SubscriptionManager
            from .subscriptions.zarinpal import ZarinpalConfig

            db_url = self.store.get("database_url") or "sqlite+aiosqlite:///data/bidpilot.sqlite3"
            db = Database(str(db_url))
            await db.init()
            async with db.session_factory() as s:
                mgr = SubscriptionManager(s, zarinpal_config=ZarinpalConfig(merchant_id=str(self.store.get("zarinpal.merchant_id") or "")))
                pays = await mgr.list_payments(chat_id, limit=8)
                if not pays:
                    await self.send(chat_id, "🧾 هنوز پرداختی ثبت نشده است.\n/plans برای خرید → زرین‌پال")
                else:
                    lines = ["<b>🧾 آخرین پرداخت‌ها</b>", "─"*28]
                    for p in pays:
                        lines.append(f"• {p.plan_id} — {p.amount_toman:,} تومان — <b>{escape(p.status)}</b> — <code>{escape(p.authority or '—')}</code>")
                    await self.send(chat_id, "\n".join(lines))
            await db.dispose()
        except Exception as exc:
            await self.send(chat_id, f"⚠️ {escape(str(exc))}")

    # ------------------------------------------------------------------ misc
    @staticmethod
    def _menu_hint() -> str:
        # New professional hint — delegates to bot_ui but kept as text fallback
        from .bot_ui import FOOTER

        return (
            "چه کاری می‌خواهی؟\n"
            "• /run شروع فعالیت (اول یک /preview یا دور آزمایشی لازم است)\n"
            "• /plans پلن‌ها و خرید اشتراک (زرین‌پال) — حرفه‌ای تا ۸۰/ساعت\n"
            "• /account حساب من: سقف ساعتی/روزانه + تاریخ انقضا\n"
            "• /status داشبورد حرفه‌ای با گراف مصرف\n"
            "• /preview دیدن یک پیشنهاد نمونه\n"
            "• /style تغییر لحن، سبک و درصد قیمت\n"
            "• /filters فیلتر پروژه‌ها\n"
            "• /test تست هوش مصنوعی\n"
            f"\n<i>{FOOTER}</i>"
        )

    @staticmethod
    def _menu_keyboard() -> dict[str, Any]:
        # Professional main menu — uses bot_ui for consistency
        try:
            from .bot_ui import main_menu_keyboard

            return main_menu_keyboard()
        except Exception:
            return {
                "inline_keyboard": [
                    [{"text": "🚀 شروع فعالیت", "callback_data": "run:ask"}],
                    [
                        {"text": "🧪 پیش‌نمایش", "callback_data": "cmd:preview"},
                        {"text": "📊 داشبورد", "callback_data": "cmd:status"},
                    ],
                    [
                        {"text": "🌐 سایت‌ها", "callback_data": "cmd:sites"},
                        {"text": "🔑 حساب سایت", "callback_data": "cmd:market"},
                    ],
                    [
                        {"text": "👤 پروفایل", "callback_data": "cmd:profile"},
                        {"text": "💳 پلن‌ها", "callback_data": "cmd:plans"},
                    ],
                    [{"text": "🧹 فیلتر پروژه‌ها", "callback_data": "cmd:filters"}],
                    [{"text": "⚙️ تنظیم مجدد", "callback_data": "cmd:setup"}],
                ]
            }


def _pricing_for(cfg: Any):
    from .pricing.strategy import PricingConfig

    return PricingConfig(**cfg.pricing).build()


def build_bot(
    token: str | None = None,
    *,
    store: SettingsStore | None = None,
    config_path: str = "config/config.yaml",
) -> BotService:
    store = store or get_store()
    token = token or store.get("telegram.bot_token")
    if not token:
        raise RuntimeError(
            "توکن ربات تلگرام تنظیم نشده است.\n"
            "  bidpilot setup            # راه‌اندازی گام‌به‌گام\n"
            "  bidpilot settings set telegram.bot_token <TOKEN>"
        )
    from .network import proxy_from_store

    api = TelegramAPI(str(token), proxy=proxy_from_store(store))
    return BotService(store, api, config_path=config_path)


__all__ = ["BotAPIError", "BotService", "Flow", "TelegramAPI", "build_bot", "HELP_TEXT"]
