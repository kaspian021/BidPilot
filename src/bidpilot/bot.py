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
    ) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client = client

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

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
                log.warning("sendMessage failed (%s); retrying as plain text", exc)
                params.pop("parse_mode", None)
                params.pop("reply_markup", None)
                await self.call("sendMessage", **params)

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


HELP_TEXT = """<b>BidPilot — راهنما</b>

/setup — راه‌اندازی کامل (تلگرام، هوش مصنوعی رایگان، حساب سایت، پروفایل، لحن، قیمت، حالت ارسال)
/run — 🚀 شروع فعالیت: رصد پروژه‌ها و ارسال پیشنهادها
/stop — ⏸ توقف ارسال
/check — بررسی اینکه هنوز وارد حساب سایت هستیم یا نه
/style — لحن، دستورهای نوشتن و درصد قیمت
/profile — ساخت پروفایل از رزومه PDF، گیت‌هاب یا پورتفولیو (یا ورود دستی)
/llm — انتخاب سرویس هوش مصنوعی رایگان و کلید آن
/sites — همه‌ی سایت‌های فریلنسری و وضعیت هرکدام
/site <name> — عوض کردن سایت فعال (مثلاً /site ponisha)
/market — حساب سایت فعال: روش ورود + کوکی یا نام کاربری/رمز
/status — چه چیزهایی تنظیم شده است؟
/preview — یک پیشنهاد نمونه از یک پروژه‌ی واقعی (بدون ارسال)
/filters — کدام پروژه‌ها بررسی می‌شوند؟ (بودجه، مهارت، کلیدواژه‌ها)
/mode — جابه‌جایی بین حالت آزمایشی و واقعی
/test — تست کلید هوش مصنوعی و تلگرام
/deps — بسته‌های لازم (مثل pypdf برای خواندن PDF) و نصب خودکارشان
/settings — دیدن مقادیر ذخیره‌شده
/cancel — لغو پرسش جاری

هیچ فایل .env لازم نیست؛ همه‌ی پاسخ‌ها امن در data/settings.json ذخیره می‌شوند."""


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
        self._log = log_fn or (lambda message: log.info("%s", message))
        #: background bidder, started from the chat with «شروع فعالیت»
        self.automation = automation or Automation(
            store=store, config_path=config_path, notify=self.notify_owner
        )

    # ------------------------------------------------------------- transport
    async def send(self, chat_id: str | int, text: str, reply_markup: dict | None = None) -> None:
        await self.api.send_message(chat_id, text, reply_markup=reply_markup)

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
        me = await self.api.get_me()
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
        if message.get("document"):
            if not self.is_allowed(chat_id):
                return
            await self._handle_document(message, str(chat_id))
            return
        text = (message.get("text") or "").strip()
        if not text:
            return
        if not self.is_allowed(chat_id):
            await self.send(
                chat_id,
                "⛔ این چت مجاز نیست.\n"
                f"chat_id تو: <code>{escape(str(chat_id))}</code>\n"
                "برای اجازه‌دادن، مالک باید این شناسه را در تنظیمات اضافه کند.",
            )
            return
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
            return
        chat = str(chat_id)

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
        elif data == "cmd:help":
            await self.send(chat_id, HELP_TEXT)
        elif data == "cmd:cancel":
            await self.handle_command(chat, "/cancel", "")

    # -------------------------------------------------------------- commands
    async def handle_command(self, chat_id: str, command: str, rest: str = "") -> None:
        if command in {"/start", "/menu"}:
            await self._cmd_start(chat_id)
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
            await self._start_flow(chat_id, "profile", profile_questions(self.store))
        elif command == "/llm":
            await self._start_flow(chat_id, "llm", llm_questions(self.store))
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

    def _note_import_error(self, message: str) -> None:
        try:
            self.store.set("profile.import_error", (message or "خوانده نشد")[:300])
        except Exception:  # pragma: no cover - read-only store
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

        pending = pending_import(self.store)
        if not pending:
            return
        if pending["type"] == "file" and not await self._ensure_pdf_reader(chat_id):
            return
        await self.send(chat_id, "🤖 دارم متن را با هوش مصنوعی تحلیل می‌کنم…")
        try:
            result = await run_pending_import(self.store, save_dir=self._uploads_dir())
        except ProfileImportError as exc:
            self._note_import_error(str(exc))
            await self.send(
                chat_id,
                f"⚠️ {escape(str(exc))}\n\n"
                "اطلاعات قبلی پروفایل حفظ شد (چیزی پاک نشد).\n"
                "می‌توانی دوباره با /profile فایل/لینک بدهی، یا «ورود دستی» را انتخاب کنی.",
            )
            return
        except Exception as exc:  # network, quota, bad config…
            log.exception("Profile import failed")
            self._note_import_error(f"تحلیل انجام نشد: {exc}")
            await self.send(
                chat_id,
                f"⚠️ تحلیل پروفایل انجام نشد: {escape(str(exc))}\n"
                "اطلاعات فعلی حفظ شد؛ با /profile دوباره تلاش کن یا دستی وارد کن.",
            )
            return

        if result is None:  # pragma: no cover - defensive
            return
        from .profile_import import clear_import_error

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
        await self.send(chat_id, check.message())
        if not check.ok:
            return
        ok, message = await self.automation.start()
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
            await self.send(
                chat_id,
                "❌ نصب خودکار نشد. این دستور را در پوشه‌ی BidPilot اجرا کن:\n"
                f"<code>{escape(detail[-300:] if detail else '')}</code>",
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

    async def _cmd_start(self, chat_id: str) -> None:
        save_chat_id(self.store, chat_id)
        state = readiness(self.store)
        if not state["setup_completed"] or missing_steps(self.store, include_recommended=False):
            await self.send(
                chat_id,
                "خوش آمدی 👋\n"
                "بیا در چند سؤال همه‌چیز را تنظیم کنیم؛ هیچ فایل .env لازم نیست.\n"
                "هر وقت خواستی با /cancel متوقف شوی.",
            )
            await self._start_flow(chat_id, "setup", all_questions(self.store), mark_complete=True)
            return
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
        await self.send(chat_id, text, self._menu_keyboard())

    async def _cmd_settings(self, chat_id: str) -> None:
        rows = self.store.masked_items()
        if not rows:
            await self.send(chat_id, "هنوز چیزی ذخیره نشده است. /setup")
            return
        body = "\n".join(f"{escape(key)} = {escape(value)}" for key, value in rows[:40])
        await self.send(chat_id, f"<b>تنظیمات ذخیره‌شده</b>\n<pre>{body}</pre>")

    async def _cmd_test(self, chat_id: str) -> None:
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
        ok, message = await verify_provider(
            LLMProvider(
                id=preset.id,
                base_url=str(self.store.get("llm.base_url") or preset.base_url),
                model=str(self.store.get("llm.model") or preset.model),
                api_key=str(key),
                extra_headers=dict(preset.extra_headers),
            )
        )
        await self.send(chat_id, ("✅ " if ok else "❌ ") + escape(message))
        check = await check_site(self.store, config_path=self.config_path)
        await self.send(chat_id, check.message(), self._run_keyboard() if check.ok else None)

    async def _cmd_preview(self, chat_id: str) -> None:
        await self.send(chat_id, "⏳ در حال گرفتن یک پروژه و نوشتن پیشنهاد نمونه…")
        try:
            messages = await self._generate_preview(limit=1)
        except Exception as exc:
            await self.send(chat_id, f"❌ پیش‌نمایش ناموفق بود: {escape(str(exc))}")
            return
        if not messages:
            await self.send(
                chat_id,
                "پروژه‌ای برای پیش‌نمایش پیدا نشد (یا همه با فیلترها/بودجه رد شدند).\n"
                "برای دیدن دلیل رد شدن‌ها: /filters",
            )
            return
        mark_preview_ok(self.store, "preview", note=messages[-1][:200])
        for message in messages:
            await self.send(chat_id, message)
        if self.store.get("runtime.submit_mode") == "live":
            await self.send(
                chat_id,
                "✅ آزمایش انجام شد؛ حالا /run در حالت واقعی باز است.",
            )

    async def _generate_preview(self, limit: int = 1) -> list[str]:
        """Build a throw-away Application and generate real proposals."""
        from .app import build_application
        from .config import load_config, reset_secret_cache

        reset_secret_cache()
        cfg = load_config(self.config_path)
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
            for project in projects:
                if len(messages) >= limit:
                    break
                if not application.filter_preview().evaluate(project):
                    continue
                price = pricing.price(project, project.budget)
                if price is None:
                    continue
                proposal = await generator.generate(project, price.amount, delivery)  # type: ignore[union-attr]
                messages.append(
                    "🧪 <b>پیش‌نمایش پیشنهاد</b>\n"
                    f"<b>{escape(project.title)}</b>\n"
                    f"💵 {price.amount:,} تومان | ⏱ {delivery} روز | 🤖 {escape(proposal.generated_by)}\n\n"
                    f"<pre>{escape(proposal.text)}</pre>"
                )
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

        try:
            apply_answer(self.store, question, value)
        except Exception as exc:  # disk full, permissions, corrupt file…
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
        if question.store_key(self.store) in {"profile.resume_path", "profile.source_url"}:
            await self._import_profile_from_pending(chat_id)
        flow.index += 1
        await self._advance(chat_id)

    async def _finish_flow(self, chat_id: str, flow: Flow) -> None:
        self._flows.pop(chat_id, None)
        if flow.mark_complete:
            mark_setup_complete(self.store, complete=not missing_steps(self.store, include_recommended=False))
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

    # ------------------------------------------------------------------ misc
    @staticmethod
    def _menu_hint() -> str:
        return (
            "چه کاری می‌خواهی؟\n"
            "• /run شروع فعالیت (اول یک /preview یا دور آزمایشی لازم است)\n"
            "• /style تغییر لحن، سبک و درصد قیمت\n"
            "• /preview دیدن یک پیشنهاد نمونه\n"
            "• /filters تنظیم اینکه چه پروژه‌هایی بررسی شوند\n"
            "• /stop توقف ارسال\n"
            "• /mode آزمایشی یا ارسال واقعی\n"
            "• /settings دیدن مقادیر ذخیره‌شده\n"
            "• /deps نصب بسته‌های لازم (pypdf و…)\n"
            "• /test تست کلید هوش مصنوعی"
        )

    @staticmethod
    def _menu_keyboard() -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [{"text": "🚀 شروع فعالیت", "callback_data": "run:ask"}],
                [
                    {"text": "🧪 پیش‌نمایش", "callback_data": "cmd:preview"},
                    {"text": "🎭 لحن", "callback_data": "cmd:style"},
                ],
                [
                    {"text": "🌐 سایت‌ها", "callback_data": "cmd:sites"},
                    {"text": "🔑 حساب سایت", "callback_data": "cmd:market"},
                ],
                [
                    {"text": "👤 پروفایل", "callback_data": "cmd:profile"},
                    {"text": "📊 وضعیت", "callback_data": "cmd:status"},
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
    api = TelegramAPI(str(token))
    return BotService(store, api, config_path=config_path)


__all__ = ["BotAPIError", "BotService", "Flow", "TelegramAPI", "build_bot", "HELP_TEXT"]
