"""Polling scheduler: jittered intervals, graceful shutdown, error backoff."""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
import signal
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from .app import Application
from .config import reset_secret_cache
from .models import Event, EventKind
from .settings_store import SettingsStore, get_store

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(
        self,
        app: Application,
        *,
        interval: int | None = None,
        jitter: int | None = None,
        sources: list[str] | None = None,
        app_factory: Callable[[], Application] | None = None,
        store: SettingsStore | None = None,
        on_report: Callable[[list[Any]], Any] | None = None,
    ) -> None:
        self.app = app
        self.interval = interval or app.config.runtime.poll_interval_seconds
        self.jitter = jitter if jitter is not None else app.config.runtime.jitter_seconds
        self.sources = sources
        self._stop = asyncio.Event()
        self.consecutive_errors = 0
        # When the user changes something in the Telegram bot (tone, key, live mode,
        # credentials) the running daemon picks it up on the next cycle — no restart.
        self._app_factory = app_factory
        self._store = store or get_store()
        self._settings_stamp = self._store.stamp()
        # Telegram bot: report the first cycle result back to the owner.
        self._on_report = on_report

    def request_stop(self) -> None:
        self._stop.set()

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.request_stop)
            except NotImplementedError:  # pragma: no cover - Windows
                pass

    async def reload_if_settings_changed(self) -> bool:
        """Rebuild the Application when data/settings.json changed underneath us."""
        if self._app_factory is None:
            return False
        stamp = self._store.stamp()
        if stamp == self._settings_stamp:
            return False
        try:
            # the process-wide secrets cache predates this change
            reset_secret_cache()
            new_app = self._app_factory()
            await new_app.setup()
        except Exception as exc:  # keep the old, working app
            log.error("Could not apply new settings: %s", exc)
            return False
        old_app = self.app
        self.app = new_app
        self._settings_stamp = stamp
        log.info("Settings changed — reloaded configuration and sources.")
        try:
            await old_app.close()
        except Exception:  # pragma: no cover
            pass
        return True

    def _remember_safe_test(self, reports: list[Any]) -> None:
        """A dry-run cycle that produced bids unlocks 🔴 live for the same site."""
        if self.app.config.runtime.live:
            return
        if not any(int(getattr(r, "dry_run", 0) or 0) > 0 for r in reports or []):
            return
        try:
            from .onboarding import mark_preview_ok

            mark_preview_ok(self._store, "dry_run", note="دور آزمایشی کامل شد")
        except Exception as exc:  # pragma: no cover - never break the loop
            log.warning("Could not store the dry-run proof: %s", exc)

    async def _emit_report(self, reports: list[Any]) -> None:
        if self._on_report is None:
            return
        try:
            result = self._on_report(reports)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # a broken callback must not stop the bidder
            log.warning("on_report callback failed: %s", exc)

    async def run_forever(self) -> None:
        self._install_signal_handlers()
        mode = "LIVE" if self.app.config.runtime.live else "DRY-RUN"
        log.info(
            "Scheduler started | mode=%s | sources=%s | every %ss (+/-%ss)",
            mode,
            self.sources or list(self.app.sources),
            self.interval,
            self.jitter,
        )
        if not self.app.config.runtime.live:
            log.warning("DRY-RUN: پیشنهادها ارسال نمی‌شوند. برای ارسال واقعی submit_mode: live را بگذارید.")

        while not self._stop.is_set():
            started = datetime.now(timezone.utc)
            try:
                await self.reload_if_settings_changed()
                reports = await self.app.cycle(self.sources)
                self.consecutive_errors = 0
                self._remember_safe_test(reports)
                await self._emit_report(reports)
                for report in reports:
                    log.info(report.summary())
                    for error in report.errors:
                        log.warning("cycle error: %s", error)
                if any(getattr(report, "authentication_failed", False) for report in reports):
                    log.error("[SCHEDULER][STOP-AUTH] marketplace login is not verified; stopping polling")
                    self.request_stop()
                    break
            except Exception as exc:  # keep the daemon alive no matter what
                self.consecutive_errors += 1
                log.exception("Cycle failed (%s in a row)", self.consecutive_errors)
                await self.app.notifiers.dispatch(  # type: ignore[union-attr]
                    Event(kind=EventKind.ERROR, title="خطا در چرخه‌ی اجرا", body=str(exc))
                )

            delay = self.interval + random.randint(-self.jitter, self.jitter)
            if self.consecutive_errors:
                delay = min(delay * (2**self.consecutive_errors), 3_600)
            elapsed = (datetime.now(timezone.utc) - started).total_seconds()
            sleep_for = max(5.0, delay - elapsed)
            log.debug("Sleeping %.0fs", sleep_for)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=sleep_for)
            except asyncio.TimeoutError:
                pass

        log.info("Scheduler stopped.")
