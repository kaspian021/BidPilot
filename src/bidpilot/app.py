"""Application wiring — the single place where config becomes live objects."""

from __future__ import annotations

import asyncio
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
from .priority_lock import submit_priority
from .proposals.generator import ProposalGenerator
from .ratelimit import RateLimiter, format_wait, gate_for
from .repositories import Repository
from .sources import Source, create_source
from .sources.base import SourceError
from .subscriptions.entitlements import Entitlements, describe_change, entitlements_from_session

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
    #: the Telegram chat this application works for (plan limits, per-site accounts)
    user_chat_id: str | None = None
    #: plan seen in the previous cycle (to announce upgrades / expiry exactly once)
    _last_entitlements: Entitlements | None = None
    #: set when the plan grew in a way that needs more marketplaces than were built
    needs_rebuild: bool = False
    #: (kind, source) -> monotonic time until which the same alert is not repeated
    _alerted_until: dict[tuple[str, str], float] = field(default_factory=dict)
    #: seconds between the starts of simultaneously running marketplaces (smooths bursts)
    site_stagger_seconds: float = 1.5

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
    async def entitlements(self, user_chat_id: str | None = None) -> Entitlements | None:
        """The running user's plan, or ``None`` when no user is known (CLI)."""
        user = user_chat_id or self.user_chat_id
        if not user:
            return None
        session = self.db.session()
        try:
            found = await entitlements_from_session(session, user)
            await session.commit()
            return found
        except Exception as exc:  # noqa: BLE001 - a broken accounts table must not stop bidding
            log.warning("could not read the plan of %s (%s); using config limits", user, type(exc).__name__)
            return None
        finally:
            await session.close()

    async def _announce_plan_change(self, ent: Entitlements) -> None:
        previous, self._last_entitlements = self._last_entitlements, ent
        if previous is None or previous.signature == ent.signature:
            return
        if ent.max_sites > previous.max_sites:
            self.needs_rebuild = True  # more marketplaces may now run: rebuild the sources
        text = describe_change(previous, ent)
        if text and self.notifiers is not None:
            try:
                await self.notifiers.dispatch(Event(kind=EventKind.DIGEST, title="تغییر پلن", body=text))
            except Exception as exc:  # noqa: BLE001
                log.warning("could not announce the plan change: %s", exc)

    def _make_pipeline(self, repo: Repository, session, user: str | None, subscription_manager, live, max_projects) -> BidPipeline:
        limiter = RateLimiter(
            self.config.runtime,
            repo,
            user_chat_id=user,
            subscription_manager=subscription_manager,
            gate=gate_for(user),
        )
        return BidPipeline(
            config=self.config,
            repo=repo,
            generator=self.generator.fork() if hasattr(self.generator, "fork") else self.generator,  # type: ignore[union-attr,arg-type]
            notifiers=self.notifiers,  # type: ignore[arg-type]
            rate_limiter=limiter,
            sources=self.sources,
            pricing=PricingConfig(**self.config.pricing).build(),
            live=live,
            max_projects=max_projects,
            user_chat_id=user,
        )

    async def _run_one(
        self,
        names: list[str],
        *,
        user: str | None,
        subscription_manager,
        live,
        max_projects,
        delay: float = 0.0,
    ) -> list[RunReport]:
        """One marketplace (or an ordered group) with its own DB session."""
        if delay > 0:
            await asyncio.sleep(delay)
        session = self.db.session()
        try:
            manager = subscription_manager
            if manager is None and user:
                from .subscriptions.manager import SubscriptionManager

                manager = SubscriptionManager(session)
            repo = Repository(session)
            pipeline = self._make_pipeline(repo, session, user, manager, live, max_projects)
            return await pipeline.run_once(names)
        finally:
            await session.close()

    async def cycle(
        self,
        source_names: list[str] | None = None,
        *,
        live: bool | None = None,
        max_projects: int | None = None,
        user_chat_id: str | None = None,
        subscription_manager=None,
    ) -> list[RunReport]:
        """One poll cycle. With a known user every marketplace gets its own session.

        What the user's plan allows is applied here: at most ``max_sites`` marketplaces,
        ``max_projects_per_poll`` projects per marketplace, the submit-queue priority,
        and the hourly/daily/monthly caps (through the rate limiter). Several
        marketplaces run *simultaneously*, so one slow site never delays the others.
        """
        user = str(user_chat_id) if user_chat_id is not None else self.user_chat_id
        ent = await self.entitlements(user) if user else None
        names = list(source_names) if source_names else list(self.sources)
        if ent is not None:
            await self._announce_plan_change(ent)
            if len(names) > ent.max_sites:
                log.info("[APP][PLAN-SITES] user=%s plan=%s runs %s of %s sites", user, ent.plan_id, ent.max_sites, len(names))
                names = names[: ent.max_sites]
            cap = int(max_projects or self.config.runtime.max_projects_per_poll or ent.max_projects_per_poll)
            max_projects = max(1, min(cap, ent.max_projects_per_poll))
            submit_priority.set(ent.priority)

        if user and ent is not None and len(names) > 1:
            groups = await asyncio.gather(
                *(
                    self._run_one(
                        [name],
                        user=user,
                        subscription_manager=subscription_manager,
                        live=live,
                        max_projects=max_projects,
                        delay=index * float(self.site_stagger_seconds),
                    )
                    for index, name in enumerate(names)
                ),
                return_exceptions=True,
            )
            reports: list[RunReport] = []
            for name, group in zip(names, groups, strict=True):
                if isinstance(group, BaseException):
                    if isinstance(group, asyncio.CancelledError):
                        raise group
                    log.error("[APP][SITE-CRASH] source=%s %s", name, type(group).__name__, exc_info=group)
                    reports.append(RunReport(source=name, errors=[f"{type(group).__name__}: {group}"]))
                else:
                    reports.extend(group)
        else:
            reports = await self._run_one(
                names,
                user=user,
                subscription_manager=subscription_manager,
                live=live,
                max_projects=max_projects,
            )
        await self._dispatch_digest(reports)
        return reports

    def _first_time(self, kind: str, source: str, ttl: float) -> bool:
        """True when this alert was not sent for ``ttl`` seconds (anti-spam, per site)."""
        key = (kind, source)
        now = time.monotonic()
        if self._alerted_until.get(key, 0.0) > now:
            return False
        self._alerted_until[key] = now + max(0.0, ttl)
        return True

    @staticmethod
    def _label(source: str) -> str:
        try:
            from .onboarding import site_label

            return site_label(source or "")
        except Exception:  # noqa: BLE001
            return source or "سایت"

    async def _send(self, event: Event) -> None:
        try:
            await self.notifiers.dispatch(event)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001 - a report must never break the bidder
            log.warning("Could not send notification: %s", exc)

    async def _dispatch_digest(self, reports: list[RunReport]) -> None:
        """Grouped messages per cycle: one alert per *site* problem, then one digest.

        A problem on one marketplace (login expired, subscription over, JavaScript
        wall, plan cap) is reported by name and never hides the news of the others.
        """
        if self.notifiers is None:
            return
        cooldown = max(0, int(self.config.runtime.notify_error_cooldown_seconds or 0))
        healthy: list[RunReport] = []
        for report in reports:
            source = report.source or "marketplace"
            label = self._label(source)
            if getattr(report, "authentication_failed", False):
                detail = next((str(e) for e in report.errors if str(e).strip()), "نشست ورود معتبر نیست.")
                await self._send(
                    Event(
                        kind=EventKind.ERROR,
                        title="ورود به حساب کاربری لازم است",
                        body=(
                            f"⛔ ورود به حساب «{label}» تأیید نشد؛ این سایت فعلاً کنار گذاشته شد"
                            + (" و سایت‌های دیگر ادامه می‌دهند." if len(reports) > 1 else ".")
                            + f"\nدلیل: {detail}\n"
                            "برای ادامه: /market ← کوکی/ورود را به‌روزرسانی کن، با /test بررسی کن، "
                            "بعد دوباره /run بزن."
                        ),
                    )
                )
            elif getattr(report, "subscription_required", False):
                detail = getattr(report, "subscription_message", "") or next(
                    (str(e) for e in report.errors if str(e).strip()), "اشتراک حساب به پایان رسیده است."
                )
                await self._send(
                    Event(
                        kind=EventKind.ERROR,
                        title=f"اشتراک {label} به پایان رسیده",
                        body=(
                            f"⛔ اشتراک حساب «{label}» تمام شده یا سقف ارسال روزانهٔ خود سایت پر شده است.\n"
                            f"دلیل: {detail}\n"
                            f"فقط «{label}» متوقف شد"
                            + ("؛ بقیهٔ سایت‌ها ادامه می‌دهند.\n" if len(reports) > 1 else ".\n")
                            + "این محدودیت از طرف خود سایت است (نه پلن BidPilot). پس از تمدید اشتراک سایت، دوباره /run بزن."
                        ),
                    )
                )
            elif getattr(report, "javascript_required", False):
                # the scheduler retries after a cooldown: do not repeat the identical alert
                if cooldown and not self._first_time("js", source, cooldown):
                    log.debug("JavaScript alert throttled for %s", source)
                    continue
                detail = next((str(e) for e in report.errors if str(e).strip()), "سایت اجرای JavaScript در مرورگر را لازم دانست.")
                pause = int(getattr(self.config.runtime, "javascript_cooldown_seconds", 1800) or 0)
                pause_text = f" حدود {max(1, pause // 60)} دقیقه" if pause else ""
                await self._send(
                    Event(
                        kind=EventKind.ERROR,
                        title="اجرای JavaScript در مرورگر لازم است",
                        body=(
                            f"⛔ سایت «{label}» پاسخ داده که JavaScript مرورگر باید فعال باشد.\n"
                            f"دلیل: {detail}\n"
                            f"این سایت{pause_text} موقتاً متوقف می‌شود و سپس خودش دوباره تلاش می‌کند؛ "
                            "لازم نیست دستی /run بزنی. اگر این پیام مداوم تکرار شد، سایت واقعاً به مرورگر "
                            "نیاز دارد و فعلاً باید پیشنهاد را دستی از خود سایت بفرستی."
                        ),
                    )
                )
            elif report.limit_code in ("hour", "day", "month"):
                # one message per cap window, not one per poll
                if self._first_time("limit-" + report.limit_code, "", min(max(report.limit_retry_after, 300), 3600)):
                    ent = self._last_entitlements
                    upsell = ""
                    if ent is not None and ent.plan_id != "elite":
                        upsell = "\nظرفیت بیشتر می‌خواهی؟ /plans — یا با دعوت دوستان رایگان ارتقا بگیر: /invite"
                    await self._send(
                        Event(
                            kind=EventKind.DIGEST,
                            title="سقف پلن پر شد",
                            body=(
                                f"⏳ <b>{report.limit_reason}</b>\n"
                                f"ربات متوقف نشده؛ همین که ظرفیت آزاد شود (حدود {format_wait(report.limit_retry_after)}) "
                                f"خودکار ادامه می‌دهد.{upsell}"
                            ),
                        )
                    )
                healthy.append(report)
            else:
                healthy.append(report)
        if not self.config.runtime.notify_digest:
            return
        interesting = [r for r in healthy if r.has_news() and not r.limit_code]
        if not interesting:
            return
        lines: list[str] = []
        for report in interesting:
            lines.extend(report.digest_lines())
        mode = "🔴 واقعی" if self.config.runtime.live else "🧪 آزمایشی"
        lines.append(f"حالت: {mode} · توقف: /stop")
        await self._send(Event(kind=EventKind.DIGEST, title="گزارش دور", body="\n".join(lines)))

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
