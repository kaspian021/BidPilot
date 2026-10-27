"""Start/stop the live bidder from Telegram.

The bot used to only *configure* things: the actual polling loop was a CLI
command you had to open a second terminal for. Now ``🚀 شروع فعالیت`` in the
chat starts the very same :class:`bidpilot.scheduler.Scheduler` in-process, and
``/stop`` pauses it — with a report to the owner after the first cycle.
"""

from __future__ import annotations

import asyncio
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

    # ------------------------------------------------------------------ state
    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _say(self, text: str) -> None:
        if self.notify is None:
            return
        try:
            await self.notify(text)
        except Exception as exc:  # pragma: no cover - network
            log.warning("Could not notify owner: %s", exc)

    # ------------------------------------------------------------------ start
    async def start(self, chat_id: str | int | None = None) -> tuple[bool, str]:
        if self.running:
            return False, "رصد از قبل در حال اجراست."
        try:
            cfg = load_config(self.config_path, store=self.store)
            secrets = None
            # Per-user LLM/pricing/profile isolation: if chat_id is known, inject its overrides
            if chat_id is not None:
                try:
                    from .per_user import apply_per_user_config
                    from .config import Secrets, secrets_from_store
                    cfg, secrets = apply_per_user_config(self.store, str(chat_id), cfg, None)
                except Exception as e:
                    log.debug("per-user config apply failed: %s", e)
                    secrets = None
            app = build_application(cfg, secrets) if secrets is not None else build_application(cfg)
            await app.setup()
        except Exception as exc:
            log.exception("Could not start automation")
            return False, f"برنامه ساخته نشد: {exc}"

        # Refuse to start LIVE discovery if we cannot prove that the marketplace
        # session is valid. This catches CLI --run as well as the Telegram button.
        if cfg.runtime.live:
            names = self.sources or list(app.sources)
            for name in names:
                source = app.sources.get(name)
                if source is None:
                    continue
                try:
                    session_ok, session_detail = await source.verify_session()
                except Exception as exc:
                    session_ok = False
                    session_detail = f"بررسی نشست با خطای {type(exc).__name__} متوقف شد."
                log.info(
                    "[AUTOMATION][SESSION-CHECK] source=%s live=True ok=%s detail=%r",
                    name,
                    session_ok,
                    session_detail,
                )
                if not session_ok:
                    message = (
                        f"⛔ <b>وارد حساب کاربری «{name}» نیستیم یا نشست معتبر نیست.</b>\n"
                        "جست‌وجو و ارسال پیشنهاد شروع نشد. از /market ورود/کوکی را به‌روز کن، "
                        "با /test وضعیت ورود را بررسی کن و سپس دوباره /run را بزن.\n"
                        f"جزئیات: {session_detail or 'ورود تأیید نشد.'}"
                    )
                    await self._say(message)
                    await app.close()
                    return False, "ورود به حساب تأیید نشد؛ عملیات شروع نشد."

        # Capture chat_id for per-user factory
        _chat_for_factory = chat_id
        def make_app() -> Application:
            cfg2 = load_config(self.config_path, store=self.store)
            secrets2 = None
            if _chat_for_factory is not None:
                try:
                    from .per_user import apply_per_user_config
                    cfg2, secrets2 = apply_per_user_config(self.store, str(_chat_for_factory), cfg2, None)
                except Exception:
                    pass
            return build_application(cfg2, secrets2) if secrets2 is not None else build_application(cfg2)

        self._app = app
        self._scheduler = Scheduler(
            app,
            interval=self.interval,
            sources=self.sources,
            app_factory=make_app,
            store=self.store,
            on_report=self._on_report,
        )
        self.summary = RunSummary(started_at=datetime.now(timezone.utc))
        self._notified_first_cycle = False
        self._task = asyncio.create_task(self._run_scheduler(), name="bidpilot-runner")
        mode = "واقعی 🔴" if cfg.runtime.live else "آزمایشی 🧪"
        return True, f"رصد شروع شد — حالت {mode}، هر {self._scheduler.interval} ثانیه."

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
            # Application._dispatch_digest sends the explicit authentication alert.
            if self._scheduler is not None:
                self._scheduler.request_stop()
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
        return (
            "🚀 <b>رصد در حال اجراست</b>\n"
            f"⏱ {summary.running_for} | {summary.cycles} دور\n"
            f"🔎 {summary.fetched} پروژه | 📨 {summary.submitted} ارسال | "
            f"🧪 {summary.dry_run} آزمایشی\n"
            "توقف: /stop"
        )


__all__ = ["Automation", "RunSummary"]
