"""Application wiring — the single place where config becomes live objects."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .config import AppConfig, Secrets, get_secrets
from .db import Database
from .filters import ProjectFilter
from .models import Event, EventKind
from .notifications import ConsoleNotifier, NotifierDispatcher, TelegramNotifier
from .pipeline import BidPipeline, RunReport
from .pricing.strategy import PricingConfig
from .proposals.generator import ProposalGenerator
from .ratelimit import RateLimiter
from .repositories import Repository
from .sources import Source, create_source
from .sources.base import SourceError

log = logging.getLogger(__name__)


@dataclass
class Application:
    config: AppConfig
    secrets: Secrets
    db: Database
    sources: dict[str, Source] = field(default_factory=dict)
    generator: ProposalGenerator | None = None
    notifiers: NotifierDispatcher | None = None
    #: source -> monotonic time of the last JavaScript alert (anti-spam: the
    #: scheduler now retries after a cooldown, so re-alerting every cycle
    #: would flood the owner with the same message)
    _js_alerted_at: dict[str, float] = field(default_factory=dict)

    # ------------------------------------------------------------- lifecycle
    @classmethod
    def build(cls, config: AppConfig, secrets: Secrets | None = None) -> Application:
        secrets = secrets or get_secrets()
        app = cls(config=config, secrets=secrets, db=Database(config.database_url))
        app.generator = ProposalGenerator(
            config,
            llm_api_key=secrets.llm_api_key,
            llm_keys=secrets.llm_keys,
        )
        app.notifiers = app._build_notifiers()
        for source_cfg in config.sources:
            if not source_cfg.enabled:
                continue
            try:
                app.sources[source_cfg.type] = create_source(
                    source_cfg.type, source_cfg.settings, config, secrets
                )
            except KeyError as exc:
                log.error("Source %s is not registered: %s", source_cfg.type, exc)
        return app

    def _build_notifiers(self) -> NotifierDispatcher:
        notifiers = [ConsoleNotifier(enabled=self.config.notifications.enabled, full_proposal=False)]
        notifiers.append(
            TelegramNotifier(
                token=self.secrets.telegram_bot_token,
                chat_ids=self.config.notifications.chat_ids,
                enabled=self.config.notifications.enabled,
                send_full_proposal=self.config.notifications.send_full_proposal,
            )
        )
        return NotifierDispatcher(notifiers)

    async def setup(self) -> None:
        await self.db.init()
        for source in self.sources.values():
            login = getattr(source, "login", None)
            if login is None:
                continue
            try:
                await login()
            except Exception as exc:  # pragma: no cover - network
                log.warning("%s login check failed: %s", source.name, exc)

    async def close(self) -> None:
        for source in self.sources.values():
            await source.aclose()
        if self.generator is not None:
            await self.generator.aclose()
        if self.notifiers is not None:
            await self.notifiers.aclose()
        await self.db.dispose()

    # ---------------------------------------------------------------- running
    async def cycle(
        self,
        source_names: list[str] | None = None,
        *,
        live: bool | None = None,
        max_projects: int | None = None,
        user_chat_id: str | None = None,
        subscription_manager=None,
    ) -> list[RunReport]:
        """One poll cycle with a fresh DB session (safe for long-running daemons)."""
        session = self.db.session()
        repo = Repository(session)
        # Per-user rate limits: if we know who triggered /run, enforce their plan
        base_limiter = RateLimiter(self.config.runtime, repo, user_chat_id=user_chat_id, subscription_manager=subscription_manager)
        pipeline = BidPipeline(
            config=self.config,
            repo=repo,
            generator=self.generator,  # type: ignore[arg-type]
            notifiers=self.notifiers,  # type: ignore[arg-type]
            rate_limiter=base_limiter,
            sources=self.sources,
            pricing=PricingConfig(**self.config.pricing).build(),
            live=live,
            max_projects=max_projects,
            user_chat_id=user_chat_id,
        )
        try:
            reports = await pipeline.run_once(source_names)
            await self._dispatch_digest(reports)
            return reports
        finally:
            await session.close()

    async def _dispatch_digest(self, reports: list[RunReport]) -> None:
        """One grouped message per cycle (see ``runtime.notify_digest``)."""
        if self.notifiers is None:
            return
        auth_failures = [r for r in reports if getattr(r, "authentication_failed", False)]
        if auth_failures:
            source = auth_failures[0].source or "marketplace"
            detail = next(
                (str(error) for report in auth_failures for error in report.errors if str(error).strip()),
                "نشست ورود معتبر نیست.",
            )
            await self.notifiers.dispatch(
                Event(
                    kind=EventKind.ERROR,
                    title="ورود به حساب کاربری لازم است",
                    body=(
                        f"⛔ ورود به حساب «{source}» تأیید نشد؛ جست‌وجو و ارسال پیشنهاد متوقف شد.\n"
                        f"دلیل: {detail}\n"
                        "برای ادامه، از /market کوکی/ورود را به‌روزرسانی و با /test بررسی کن؛ "
                        "سپس دوباره /run را بزن."
                    ),
                )
            )
            return
        subscription_failures = [r for r in reports if getattr(r, "subscription_required", False)]
        if subscription_failures:
            source = subscription_failures[0].source or "marketplace"
            detail = getattr(subscription_failures[0], "subscription_message", "") or next(
                (str(error) for report in subscription_failures for error in report.errors if str(error).strip()),
                "اشتراک حساب به پایان رسیده است.",
            )
            await self.notifiers.dispatch(
                Event(
                    kind=EventKind.ERROR,
                    title="اشتراک پارس‌کدرز به پایان رسیده",
                    body=(
                        f"⛔ اشتراک حساب «{source}» به پایان رسیده یا سقف ارسال روزانه پر شده است.\\n"
                        f"دلیل: {detail}\\n"
                        "جست‌وجو و ارسال پیشنهاد متوقف شد تا اشتراک را تمدید کنی.\\n"
                        "حساب رایگان روزی ۱ پیشنهاد اجازه می‌دهد؛ برای ارسال نامحدود باید اشتراک بخری.\\n"
                        "پس از تمدید اشتراک، دوباره /run را بزن."
                    ),
                )
            )
            return
        javascript_failures = [r for r in reports if getattr(r, "javascript_required", False)]
        if javascript_failures:
            source = javascript_failures[0].source or "marketplace"
            # The scheduler retries this source after a cooldown; do not
            # re-send the identical alert on every failed retry.
            cooldown = max(0, int(self.config.runtime.notify_error_cooldown_seconds or 0))
            last_alert = self._js_alerted_at.get(source, 0.0)
            if cooldown and time.monotonic() - last_alert < cooldown:
                log.debug("JavaScript alert throttled for %s", source)
                return
            self._js_alerted_at[source] = time.monotonic()
            detail = next(
                (
                    str(error)
                    for report in javascript_failures
                    for error in report.errors
                    if str(error).strip()
                ),
                "سایت اجرای JavaScript در مرورگر را لازم دانست.",
            )
            pause = int(getattr(self.config.runtime, "javascript_cooldown_seconds", 1800) or 0)
            pause_text = f" حدود {max(1, pause // 60)} دقیقه" if pause else ""
            await self.notifiers.dispatch(
                Event(
                    kind=EventKind.ERROR,
                    title="اجرای JavaScript در مرورگر لازم است",
                    body=(
                        f"⛔ سایت «{source}» پاسخ داده که JavaScript مرورگر باید فعال باشد.\n"
                        f"دلیل: {detail}\n"
                        "این نسخه از BidPilot با HTTP مستقیم کار می‌کند و JavaScript اجرا نمی‌کند؛ "
                        "ثبت پیشنهاد تأیید نشده است.\n"
                        f"این منبع{pause_text} موقتاً متوقف می‌شود و سپس خودش دوباره تلاش می‌کند؛ "
                        "لازم نیست دستی /run بزنی. اگر این پیام مداوم تکرار شد، یعنی سایت واقعاً "
                        "به مرورگر نیاز دارد و فعلاً باید پیشنهاد را دستی از خود سایت بفرستی."
                    ),
                )
            )
            return
        if not self.config.runtime.notify_digest:
            return
        interesting = [r for r in reports if r.has_news()]
        if not interesting:
            return
        lines: list[str] = []
        for report in interesting:
            lines.extend(report.digest_lines())
        mode = "🔴 واقعی" if self.config.runtime.live else "🧪 آزمایشی"
        lines.append(f"حالت: {mode} · توقف: /stop")
        try:
            await self.notifiers.dispatch(
                Event(kind=EventKind.DIGEST, title="گزارش دور", body="\n".join(lines))
            )
        except Exception as exc:  # a report must never break the bidder
            log.warning("Could not send digest: %s", exc)

    async def healthcheck(self) -> dict[str, tuple[bool, str]]:
        results: dict[str, tuple[bool, str]] = {}
        for name, source in self.sources.items():
            try:
                results[name] = await source.healthcheck()
            except (SourceError, Exception) as exc:
                results[name] = (False, str(exc))
        return results

    def filter_preview(self):
        return ProjectFilter(
            self.config.filters,
            profile_skills=list(self.config.profile.stack),
            profile_role=self.config.profile.role,
        )


def build_application(config: AppConfig, secrets: Secrets | None = None) -> Application:
    return Application.build(config, secrets)


def configure_logging(level: str = "INFO") -> None:
    level_value = getattr(logging, str(level or "INFO").upper(), logging.INFO)
    logging.basicConfig(
        level=level_value,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # basicConfig is a no-op when an embedding host already installed a handler;
    # still apply the requested level so INFO diagnostics are not silently hidden.
    logging.getLogger().setLevel(level_value)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
