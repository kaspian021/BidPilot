"""Polling scheduler: jittered intervals, graceful shutdown, error backoff."""

from __future__ import annotations

import asyncio
import logging
import random
import signal
from datetime import datetime, timezone

from .app import Application
from .models import Event, EventKind

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(
        self,
        app: Application,
        *,
        interval: int | None = None,
        jitter: int | None = None,
        sources: list[str] | None = None,
    ) -> None:
        self.app = app
        self.interval = interval or app.config.runtime.poll_interval_seconds
        self.jitter = jitter if jitter is not None else app.config.runtime.jitter_seconds
        self.sources = sources
        self._stop = asyncio.Event()
        self.consecutive_errors = 0

    def request_stop(self) -> None:
        self._stop.set()

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.request_stop)
            except NotImplementedError:  # pragma: no cover - Windows
                pass

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
                reports = await self.app.cycle(self.sources)
                self.consecutive_errors = 0
                for report in reports:
                    log.info(report.summary())
                    for error in report.errors:
                        log.warning("cycle error: %s", error)
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
