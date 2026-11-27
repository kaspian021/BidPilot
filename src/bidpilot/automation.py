"""Start/stop the live bidder from Telegram.

The bot used to only *configure* things: the actual polling loop was a CLI
command you had to open a second terminal for. Now ``🚀 شروع فعالیت`` in the
chat starts the very same :class:`bidpilot.scheduler.Scheduler` in-process, and
``/stop`` pauses it — with a report to the owner after the first cycle.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .app import Application, build_application
from .config import load_config
from .scheduler import Scheduler
from .settings_store import SettingsStore

log = logging.getLogger(__name__)

Notify = Callable[[str], Awaitable[Any]]


def _accepts_chat_id(callback: Any) -> bool:
    """True when ``callback`` can take ``chat_id=`` (older/simple notifiers take only the text)."""
    try:
        params = inspect.signature(callback).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == "chat_id" or p.kind is inspect.Parameter.VAR_KEYWORD for p in params)


def scope_notifications(cfg: Any, chat_id: str | int | None) -> Any:
    """Send this run's Telegram events only to the chat that started it.

    ``telegram.chat_ids`` is the *allow-list* of everybody who may use the bot.
    Using it as the recipient list made every user receive every other user's
    bid reports (projects, prices, proposal text). With no known starter (CLI
    ``--run``) the configured list is kept as the operator set it.
    """
    if chat_id is not None and str(chat_id).strip():
        cfg.notifications.chat_ids = [str(chat_id).strip()]
    return cfg


@dataclass
class RunSummary:
    cycles: int = 0
    fetched: int = 0
    submitted: int = 0
    dry_run: int = 0
    skipped: int = 0
    failed: int = 0
    started_at: datetime | None = None
    last_cycle_at: datetime | None = None
    last_error: str = ""
    sites: list[str] = field(default_factory=list)

    def add(self, reports: list[Any]) -> None:
        self.cycles += 1
        self.last_cycle_at = datetime.now(timezone.utc)
        for report in reports or []:
            self.fetched += int(getattr(report, "fetched", 0) or 0)
            self.submitted += int(getattr(report, "submitted", 0) or 0)
            self.dry_run += int(getattr(report, "dry_run", 0) or 0)
            self.skipped += int(getattr(report, "skipped", 0) or 0)
            self.failed += int(getattr(report, "failed", 0) or 0)

    @property
    def running_for(self) -> str:
        if self.started_at is None:
            return ""
        seconds = int((datetime.now(timezone.utc) - self.started_at).total_seconds())
        minutes, seconds = divmod(max(0, seconds), 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours} ساعت و {minutes} دقیقه"
        if minutes:
            return f"{minutes} دقیقه"
        return f"{seconds} ثانیه"


@dataclass
class Automation:
    """Owns the background task that polls the marketplace and sends bids."""

    store: SettingsStore
    config_path: str = "config/config.yaml"
    notify: Notify | None = None
    interval: int | None = None
    sources: list[str] | None = None
    summary: RunSummary = field(default_factory=RunSummary)
    _task: asyncio.Task | None = None
    _app: Application | None = None
    _scheduler: Scheduler | None = None
    _notified_first_cycle: bool = False
    #: chat that pressed /run: its reports go to it alone, never to every linked chat
    _owner_chat_id: str | None = None
    #: the plan this run started under (shown by /status)
    _ent: Any = None

    # ------------------------------------------------------------------ state
    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _say(self, text: str) -> None:
        if self.notify is None:
            return
        try:
            if self._owner_chat_id is not None and _accepts_chat_id(self.notify):
                await self.notify(text, chat_id=self._owner_chat_id)
            else:
                await self.notify(text)
        except Exception as exc:  # pragma: no cover - network
            log.warning("Could not notify owner: %s", exc)

    # ------------------------------------------------------------------ start
    def _build_for(self, chat_id, sites: list[str] | None):
        """Config + application for one user, built under that user's identity."""
        from .per_user import apply_per_user_config, user_scope

        cfg = load_config(self.config_path, store=self.store)
        secrets = None
        if chat_id is not None:
            try:
                cfg, secrets = apply_per_user_config(self.store, str(chat_id), cfg, None, sites=sites)
            except Exception as exc:  # noqa: BLE001
                log.debug("per-user config apply failed: %s", exc)
                secrets = None
        scope_notifications(cfg, chat_id)
        # sources read the account of the *ambient* user while they are constructed
        with user_scope(chat_id):
            app = build_application(cfg, secrets) if secrets is not None else build_application(cfg)
        try:
            app.user_chat_id = str(chat_id) if chat_id is not None else None
        except Exception:  # pragma: no cover - exotic fakes
            pass
        return cfg, app

    async def _plan_sites(self, chat_id, cfg) -> tuple[list[str] | None, Any]:
        """(sites to run, entitlements) for ``chat_id``; (None, None) without a user."""
        if chat_id is None:
            return None, None
        from .subscriptions.entitlements import resolve_entitlements
        from .user_sites import user_sites

        ent = await resolve_entitlements(cfg.database_url, chat_id)
        kept, paused = ent.allowed_sites(user_sites(self.store, chat_id))
        if paused:
            log.info("[AUTOMATION][PLAN-SITES] chat=%s plan=%s paused_by_plan=%s", chat_id, ent.plan_id, paused)
        return kept, ent

    async def start(self, chat_id: str | int | None = None) -> tuple[bool, str]:
        if self.running:
            return False, "رصد از قبل در حال اجراست."
        try:
            cfg0 = load_config(self.config_path, store=self.store)
            planned, ent = await self._plan_sites(chat_id, cfg0)
            self._owner_chat_id = str(chat_id) if chat_id is not None else None
            cfg, app = self._build_for(chat_id, planned)
            await app.setup()
        except Exception as exc:
            log.exception("Could not start automation")
            return False, f"برنامه ساخته نشد: {exc}"

        # Refuse to start LIVE discovery for a marketplace whose session cannot be
        # proven valid (CLI --run and the Telegram button alike). Sites are checked
        # at the same time; one broken login only drops that site.
        broken: dict[str, str] = {}
        if cfg.runtime.live:
            names = self.sources or list(app.sources)

            async def check(name: str) -> tuple[str, bool, str]:
                source = app.sources.get(name)
                if source is None:
                    return name, True, ""
                try:
                    ok, detail = await source.verify_session()
                except Exception as exc:
                    ok, detail = False, f"بررسی نشست با خطای {type(exc).__name__} متوقف شد."
                log.info("[AUTOMATION][SESSION-CHECK] source=%s live=True ok=%s detail=%r", name, ok, detail)
                return name, ok, detail

            for name, ok, detail in await asyncio.gather(*(check(n) for n in names)):
                if not ok:
                    broken[name] = detail or "ورود تأیید نشد."
            if broken and len(broken) >= len(names):
                label = self._label(next(iter(broken)))
                message = (
                    f"⛔ <b>وارد حساب کاربری «{label}» نیستیم یا نشست معتبر نیست.</b>\n"
                    "جست‌وجو و ارسال پیشنهاد شروع نشد. از /market ورود/کوکی را به‌روز کن، "
                    "با /test وضعیت ورود را بررسی کن و سپس دوباره /run را بزن.\n"
                    f"جزئیات: {next(iter(broken.values()))}"
                )
                await self._say(message)
                await app.close()
                return False, "ورود به حساب تأیید نشد؛ عملیات شروع نشد."
            for name in broken:  # keep running the healthy sites
                source = app.sources.pop(name, None)
                try:
                    if source is not None and hasattr(source, "aclose"):
                        await source.aclose()
                except Exception:  # pragma: no cover
                    pass

        _chat = chat_id

        async def make_app() -> Application:
            """Rebuilt on settings / plan changes: re-reads the plan and the site list."""
            cfg2 = load_config(self.config_path, store=self.store)
            planned2, _ = await self._plan_sites(_chat, cfg2)
            _, app2 = self._build_for(_chat, planned2)
            return app2

        self._app = app
        self._ent = ent
        self._scheduler = Scheduler(
            app,
            interval=self.interval,
            sources=self.sources,
            app_factory=make_app,
            store=self.store,
            on_report=self._on_report,
        )
        self.summary = RunSummary(started_at=datetime.now(timezone.utc))
        self.summary.sites = list(app.sources)
        self._notified_first_cycle = False
        self._task = asyncio.create_task(self._run_scheduler(), name="bidpilot-runner")
        mode = "واقعی 🔴" if cfg.runtime.live else "آزمایشی 🧪"
        text = f"رصد شروع شد — حالت {mode}، هر {self._scheduler.interval} ثانیه."
        text += self._start_details(chat_id, ent, planned, broken)
        return True, text

    @staticmethod
    def _label(site: str) -> str:
        try:
            from .onboarding import site_label

            return site_label(site)
        except Exception:  # noqa: BLE001
            return site

    def _start_details(self, chat_id, ent, planned, broken: dict[str, str]) -> str:
        """Plain-language lines after the start: which sites run, what the plan allows."""
        if ent is None or not planned:
            return ""
        from .user_sites import user_sites

        running = [n for n in planned if n not in broken]
        lines = [""]
        if running:
            joined = "، ".join(self._label(n) for n in running)
            lines.append(f"🌐 سایت‌های فعال ({len(running)} از {ent.max_sites} مجاز): {joined}")
        for name, why in broken.items():
            lines.append(f"⚠️ «{self._label(name)}» فعلاً کنار گذاشته شد: {why} — از /market درستش کن.")
        waiting = [n for n in user_sites(self.store, chat_id) if n not in planned]
        if waiting:
            lines.append(
                f"⏸ {('، '.join(self._label(n) for n in waiting))} منتظر پلن بالاتر است؛ "
                "با ارتقا (/plans) یا دعوت دوستان (/invite) خودکار شروع می‌شود."
            )
        month = "نامحدود" if ent.monthly_unlimited else str(ent.per_month)
        lines.append(f"📊 پلن «{ent.plan_title}»: {ent.per_hour} پیشنهاد در ساعت · {ent.per_day} در روز · {month} در ماه")
        return "\n".join(lines)

    async def _run_scheduler(self) -> None:
        try:
            if self._scheduler is not None:
                await self._scheduler.run_forever()
        finally:
            # The scheduler also exits automatically if authentication expires.
            # Release the app/database/HTTP clients in either shutdown path.
            await self._shutdown_app()

    # ------------------------------------------------------------------- stop
    async def stop(self) -> tuple[bool, str]:
        if not self.running:
            return False, "الان چیزی در حال اجرا نیست."
        assert self._scheduler is not None
        self._scheduler.request_stop()
        task = self._task
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=30)  # type: ignore[arg-type]
        except (asyncio.TimeoutError, asyncio.CancelledError):
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        await self._shutdown_app()
        return True, "رصد متوقف شد."

    async def _shutdown_app(self) -> None:
        self._task = None
        self._scheduler = None
        app, self._app = self._app, None
        if app is not None:
            try:
                await app.close()
            except Exception:  # pragma: no cover
                pass

    # ---------------------------------------------------------------- reports
    async def _on_report(self, reports: list[Any]) -> None:
        self.summary.add(reports)
        if any(getattr(report, "authentication_failed", False) for report in reports):
            # Application._dispatch_digest sends the explicit authentication alert; the
            # Scheduler parks only that site and stops the run when none is left.
            return
        if any(getattr(report, "javascript_required", False) for report in reports):
            # Application._dispatch_digest sends the JavaScript alert; the
            # Scheduler pauses only that source with a cooldown and retries
            # automatically — unlike an expired login this needs no hard stop.
            return
        if not self._notified_first_cycle:
            self._notified_first_cycle = True
            await self._say(self.first_cycle_text())
            return
        if self.summary.submitted:
            await self._say(self.status_text())

    def first_cycle_text(self) -> str:
        summary = self.summary
        lines = ["✅ <b>اولین بررسی انجام شد.</b>"]
        lines.append(f"🔎 {summary.fetched} پروژه بررسی شد.")
        if summary.dry_run:
            lines.append(f"🧪 {summary.dry_run} پیشنهاد ساخته شد (ارسال نشد — حالت آزمایشی).")
        if summary.submitted:
            lines.append(f"📨 {summary.submitted} پیشنهاد ارسال شد.")
        if not summary.fetched:
            lines.append("پروژه‌ی جدیدی پیدا نشد؛ دور بعد دوباره بررسی می‌شود.")
        lines.append("")
        lines.append("برای توقف: /stop — وضعیت: /status")
        return "\n".join(lines)

    def status_text(self) -> str:
        summary = self.summary
        if not self.running:
            head = "⏸ رصد متوقف است."
            if summary.cycles:
                head += f" (آخرین اجرا: {summary.cycles} دور)"
            return head + "\nبرای شروع: /run"
        lines = [
            "🚀 <b>رصد در حال اجراست</b>",
            f"⏱ {summary.running_for} | {summary.cycles} دور",
        ]
        app = self._app
        sites = list(getattr(app, "sources", {}) or summary.sites)
        if sites:
            lines.append("🌐 سایت‌ها: " + "، ".join(self._label(n) for n in sites))
        ent = getattr(app, "_last_entitlements", None) or self._ent
        if ent is not None:
            lines.append(f"{ent.plan_badge} پلن «{ent.plan_title}» · {ent.per_hour} در ساعت · {ent.max_sites} سایت همزمان")
        lines.append(
            f"🔎 {summary.fetched} پروژه | 📨 {summary.submitted} ارسال | 🧪 {summary.dry_run} آزمایشی"
        )
        if summary.failed:
            lines.append(f"❌ {summary.failed} ارسال ناموفق — علت هر کدام در پیام خودش آمده است.")
        lines.append("توقف: /stop")
        return "\n".join(lines)


__all__ = ["Automation", "RunSummary"]
