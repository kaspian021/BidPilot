"""Polling scheduler: jittered intervals, graceful shutdown, error backoff.

Failure policy (deliberately asymmetric):

* **Authentication failure** -> hard stop. Bidding logged-out risks duplicate
  or misdirected proposals, and only the user can fix the cookie.
* **JavaScript-required interstitial** -> pause *that one source* with an
  exponentially growing cooldown and keep everything else running. These
  pages are usually transient anti-bot responses; the old behaviour killed
  the whole daemon forever on the first one (and, worse, the detector could
  false-fire on a healthy page), leaving the bot dead until a manual /run.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
import signal
import time
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
        javascript_cooldown: int | None = None,
    ) -> None:
        self.app = app
        self.interval = interval or app.config.runtime.poll_interval_seconds
        self.jitter = jitter if jitter is not None else app.config.runtime.jitter_seconds
        self.sources = sources
        if javascript_cooldown is None:
            javascript_cooldown = int(getattr(app.config.runtime, "javascript_cooldown_seconds", 1_800) or 0)
        self.javascript_cooldown = max(0, int(javascript_cooldown))
        #: source name -> monotonic deadline until which it is not polled
        self._paused_until: dict[str, float] = {}
        #: source name -> consecutive JavaScript-required reports (backoff input)
        self._js_streak: dict[str, int] = {}
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

    # ------------------------------------------------- JavaScript circuit breaker
    def _active_sources(self) -> list[str] | None:
        """Sources to poll this cycle (None = application's own default list)."""
        names = self.sources or list(getattr(self.app, "sources", {}) or [])
        if not names:
            return self.sources
        now = time.monotonic()
        return [name for name in names if self._paused_until.get(name, 0.0) <= now]

    def _pause_javascript_sources(self, reports: list[Any]) -> None:
        for report in reports or []:
            name = str(getattr(report, "source", "") or "")
            if not name:
                continue
            if not getattr(report, "javascript_required", False):
                self._js_streak.pop(name, None)  # healthy again: reset backoff
                continue
            streak = self._js_streak.get(name, 0) + 1
            self._js_streak[name] = streak
            if not self.javascript_cooldown:
                continue
            cooldown = min(
                self.javascript_cooldown * (2 ** (streak - 1)),
                self.javascript_cooldown * 8,
            )
            self._paused_until[name] = time.monotonic() + cooldown
            log.error(
                "[SCHEDULER][PAUSE-JAVASCRIPT] source=%s paused_for=%ss streak=%s "
                "(other sources keep polling; this one retries automatically)",
                name,
                cooldown,
                streak,
            )

    def _seconds_until_next_resume(self) -> float | None:
        if not self._paused_until:
            return None
        now = time.monotonic()
        remaining = [until - now for until in self._paused_until.values() if until > now]
        return min(remaining) if remaining else None

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
            all_paused = False
            try:
                await self.reload_if_settings_changed()
                active = self._active_sources()
                if active == []:
                    all_paused = True
                    log.warning(
                        "[SCHEDULER][PAUSED] every source is in JavaScript cooldown; next retry in %.0fs",
                        self._seconds_until_next_resume() or 0,
                    )
                    reports = []
                else:
                    reports = await self.app.cycle(active)
                self.consecutive_errors = 0
                self._remember_safe_test(reports)
                if reports:
                    await self._emit_report(reports)
                for report in reports:
                    log.info(report.summary())
                    for error in report.errors:
                        log.warning("cycle error: %s", error)
                if any(getattr(report, "authentication_failed", False) for report in reports):
                    log.error("[SCHEDULER][STOP-AUTH] marketplace login is not verified; stopping polling")
                    self.request_stop()
                    break
                # JavaScript interstitials pause the offending source only;
                # they are usually transient anti-bot pages and must not kill
                # the daemon (or the user's other marketplaces) forever.
                self._pause_javascript_sources(reports)
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
            if all_paused:
                # Wake up exactly when the first paused source may retry
                # instead of sleeping a whole extra poll interval past it.
                resume_in = self._seconds_until_next_resume()
                if resume_in is not None:
                    sleep_for = min(sleep_for, max(5.0, resume_in))
            log.debug("Sleeping %.0fs", sleep_for)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=sleep_for)
            except asyncio.TimeoutError:
                pass

        log.info("Scheduler stopped.")
