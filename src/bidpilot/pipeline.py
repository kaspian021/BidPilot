"""The bid pipeline: fetch → filter → dedupe → price → write → submit → notify.

Every step is independently observable (DB rows + log + notification), so a failure in
one project never stops the batch and every decision can be explained later.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import AppConfig
from .filters import ProjectFilter
from .models import (
    BidPlan,
    Event,
    EventKind,
    Project,
    SkipReason,
    SubmissionResult,
    SubmissionStatus,
)
from .notifications.base import NotifierDispatcher
from .pricing.strategy import PricingConfig, PricingStrategy
from .proposals.generator import ProposalError, ProposalGenerator
from .ratelimit import RateLimiter
from .repositories import Repository
from .sources.base import Source
from .text import parse_days

log = logging.getLogger(__name__)


@dataclass
class RunReport:
    source: str = ""
    fetched: int = 0
    new: int = 0
    duplicates: int = 0
    submitted: int = 0
    dry_run: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    #: human-readable reason -> how many projects were skipped for it
    skip_reasons: dict[str, int] = field(default_factory=dict)
    #: titles of the projects that made it all the way to a proposal
    proposed: list[str] = field(default_factory=list)
    #: the real reason the LLM failed (shown to the user instead of "template")
    llm_error: str = ""
    #: pages the AI read while discovering projects (0 = selector mode)
    ai_pages: int = 0
    ai_calls: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def has_news(self) -> bool:
        return bool(self.new or self.submitted or self.failed or self.errors)

    def digest_lines(self, *, limit: int = 4) -> list[str]:
        """Compact, group-by-reason report — one message per cycle instead of 50."""
        head = (
            f"گزارش {self.source}\n"
            f"🔎 {self.fetched} پروژه بررسی شد · 🆕 {self.new} جدید · "
            f"✅ {self.submitted} ارسال · 🧪 {self.dry_run} آزمایشی · ⏭ {self.skipped} رد"
        )
        lines = [head]
        if self.proposed:
            shown = "، ".join(self.proposed[:limit])
            extra = len(self.proposed) - limit
            lines.append(f"📝 پیشنهاد برای: {shown}" + (f" و {extra} مورد دیگر" if extra > 0 else ""))
        for reason, count in sorted(self.skip_reasons.items(), key=lambda kv: -kv[1])[:limit]:
            lines.append(f"⏭ {count}× {reason}")
        if self.errors:
            lines.append(f"⚠️ {len(self.errors)} خطا: {self.errors[0][:120]}")
        if self.ai_pages:
            lines.append(
                f"🧭 جست‌وجوی هوشمند: {self.ai_pages} صفحه خوانده شد ({self.ai_calls} درخواست به مدل)"
            )
        if self.llm_error:
            lines.append(f"🤖 هوش مصنوعی جواب نداد: {self.llm_error[:160]}")
            lines.append("متن‌ها از قالب ساخته شدند — /test و /llm را ببین.")
        return lines

    def summary(self) -> str:
        return (
            f"{self.source}: دریافت {self.fetched} | جدید {self.new} | تکراری {self.duplicates}"
            f" | ارسال {self.submitted} | آزمایشی {self.dry_run} | رد {self.skipped} | خطا {self.failed}"
        )


class BidPipeline:
    def __init__(
        self,
        config: AppConfig,
        repo: Repository,
        generator: ProposalGenerator,
        notifiers: NotifierDispatcher,
        rate_limiter: RateLimiter,
        sources: dict[str, Source],
        pricing: PricingStrategy | None = None,
        *,
        live: bool | None = None,
        max_projects: int | None = None,
    ) -> None:
        self.config = config
        self.repo = repo
        self.generator = generator
        self.notifiers = notifiers
        self.rate_limiter = rate_limiter
        self.sources = sources
        self.pricing = pricing or PricingConfig(**config.pricing).build()
        self.project_filter = ProjectFilter(
            config.filters,
            profile_skills=list(config.profile.stack),
            profile_role=config.profile.role,
        )
        self.live = config.runtime.live if live is None else live
        self.max_projects = max_projects or config.runtime.max_projects_per_poll
        self.delivery_days = int(config.pricing.get("delivery_days", 14))

    # ------------------------------------------------------------------ runs
    async def run_once(self, source_names: list[str] | None = None) -> list[RunReport]:
        names = source_names or list(self.sources)
        reports: list[RunReport] = []
        for name in names:
            source = self.sources.get(name)
            if source is None:
                log.warning("Source %s not configured; skipping", name)
                continue
            reports.append(await self.run_source(source))
        return reports

    async def run_source(self, source: Source) -> RunReport:
        report = RunReport(source=source.name)
        try:
            projects = await source.fetch_projects()
            report.fetched = len(projects)
            discovery = getattr(source, "last_discovery", None)
            if discovery is not None:
                report.ai_pages = int(getattr(discovery, "pages_read", 0) or 0)
                report.ai_calls = int(getattr(discovery, "llm_calls", 0) or 0)
            for project in projects[: self.max_projects]:
                try:
                    await self.process_project(project, report)
                except Exception as exc:  # never let one project kill the batch
                    log.exception("Project %s failed", project.uid)
                    report.errors.append(f"{project.uid}: {exc}")
        except Exception as exc:  # fetch/parse failure for the whole source
            report.errors.append(str(exc))
            log.error("Fetch failed for %s: %s", source.name, exc, exc_info=log.isEnabledFor(logging.DEBUG))
            await self._notify_error(source.name, f"دریافت لیست پروژه‌ها ناموفق بود: {exc}")
        finally:
            await self.repo.commit()  # persist whatever happened (incl. error events)
        log.info(report.summary())
        return report

    # -------------------------------------------------------------- one item
    async def process_project(self, project: Project, report: RunReport) -> BidPlan | None:
        # 1) dedupe on *scraped* projects (we store everything we saw, even if filtered)
        if await self.repo.project_exists(project.source, project.external_id):
            report.duplicates += 1
            return None

        # 2) filtering
        decision = self.project_filter.evaluate(project)
        project_row = await self.repo.save_project(project)
        report.new += 1

        if not decision.allowed:
            report.skipped += 1
            plan = BidPlan(
                project=project,
                result=SubmissionResult(status=SubmissionStatus.SKIPPED, message=decision.reason),
            )
            await self.repo.save_bid(project_row.id, plan)
            await self._emit_skip(project, decision.reason, report)
            return plan

        if not self.config.runtime.notify_digest:
            await self.notifiers.dispatch(
                Event(kind=EventKind.NEW_PROJECT, title=project.title, project=project)
            )

        # 3) pricing
        pricing = self.pricing.price(project, project.budget)
        if pricing is None:
            report.skipped += 1
            await self._emit_skip(project, "قیمت‌گذاری امکان‌پذیر نبود.", report)
            return None

        amount = pricing.amount
        delivery = self.delivery_days
        # respect the timeline the employer asked for (never exceed it)
        hint = parse_days(project.employer_deadline)
        if hint and hint < delivery:
            delivery = hint

        # 4) proposal
        try:
            proposal = await self.generator.generate(project, amount, delivery)
        except ProposalError as exc:
            report.skipped += 1
            plan = BidPlan(project=project, amount=amount, rationale=pricing.rationale)
            plan.result = SubmissionResult(status=SubmissionStatus.SKIPPED, message=str(exc))
            await self.repo.save_bid(project_row.id, plan)
            await self._emit_skip(project, f"متن پیشنهاد تولید نشد: {exc}", report)
            return plan

        if getattr(self.generator, "last_llm_error", "") and not report.llm_error:
            report.llm_error = str(self.generator.last_llm_error)
        report.proposed.append(project.title)
        plan = BidPlan(
            project=project,
            amount=amount,
            delivery_days=delivery,
            proposal=proposal,
            rationale=pricing.rationale,
        )

        # 5) rate limit / caps
        rate = await self.rate_limiter.check()
        if not rate.allowed:
            report.skipped += 1
            plan.result = SubmissionResult(status=SubmissionStatus.SKIPPED, message=rate.reason)
            await self.repo.save_bid(project_row.id, plan)
            await self._emit_skip(project, rate.reason, report)
            return plan

        # 6) submit (or dry run)
        started = time.monotonic()
        if not self.live:
            plan.result = SubmissionResult(
                status=SubmissionStatus.DRY_RUN, message="حالت آزمایشی: پیشنهاد ارسال نشد."
            )
            report.dry_run += 1
            await self.repo.save_bid(project_row.id, plan)
            await self.notifiers.dispatch(
                Event(
                    kind=EventKind.BID_DRY_RUN,
                    title=project.title,
                    project=project,
                    bid=plan,
                )
            )
            return plan

        try:
            result = await self.sources[project.source].submit_bid(project, proposal, amount, delivery)
        except Exception as exc:
            log.exception("Submit crashed for %s", project.uid)
            result = SubmissionResult(status=SubmissionStatus.FAILED, message=f"خطای غیرمنتظره: {exc}")
        plan.result = result

        bid_row = await self.repo.save_bid(project_row.id, plan)
        if result.status == SubmissionStatus.SUBMITTED:
            report.submitted += 1
        else:
            report.failed += 1
            await self.repo.update_bid_status(bid_row.id, result.status, result.message)
            log.warning(
                "Bid failed for %s: %s (%.0f ms)",
                project.uid,
                result.message,
                (time.monotonic() - started) * 1000,
            )

        await self.notifiers.dispatch(
            Event(
                kind=EventKind.BID_SUBMITTED
                if result.status == SubmissionStatus.SUBMITTED
                else EventKind.ERROR,
                title=project.title,
                project=project,
                bid=plan,
            )
        )
        return plan

    # ------------------------------------------------------------- helpers
    def _note_skip(self, report: RunReport, reason: str) -> None:
        key = (reason or "دلیل نامشخص").strip()
        report.skip_reasons[key] = report.skip_reasons.get(key, 0) + 1

    async def _emit_skip(self, project: Project, reason: str, report: RunReport | None = None) -> None:
        if report is not None:
            self._note_skip(report, reason)
        # In digest mode the cycle sends one grouped message instead of one per
        # project; the decision is still stored in the DB as an event.
        if self.config.runtime.notify_digest:
            await self.repo.log_event(
                Event(kind=EventKind.BID_SKIPPED, title=project.title, project=project, body=reason)
            )
            return
        if not self.config.runtime.notify_on_skip:
            return
        await self.repo.log_event(
            Event(kind=EventKind.BID_SKIPPED, title=project.title, project=project, body=reason)
        )
        await self.notifiers.dispatch(
            Event(
                kind=EventKind.BID_SKIPPED,
                title=project.title,
                project=project,
                body=f"دلیل: {reason}",
                bid=BidPlan(
                    project=project,
                    result=SubmissionResult(status=SubmissionStatus.SKIPPED, message=reason),
                ),
            )
        )

    async def _notify_error(self, source: str, message: str) -> None:
        """Log every failure, but only ping Telegram once per cooldown window."""
        title = f"خطا در منبع {source}"
        cooldown = self.config.runtime.notify_error_cooldown_seconds
        last = await self.repo.last_event_at(EventKind.ERROR.value, title)
        await self.repo.log_event(Event(kind=EventKind.ERROR, title=title, body=message))
        now = datetime.now(timezone.utc)
        if last is not None:  # `last` may be naive (SQLite) -> normalize
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if (now - last).total_seconds() < cooldown:
                log.debug("Error notification throttled for %s", source)
                return
        await self.notifiers.dispatch(Event(kind=EventKind.ERROR, title=title, body=message))

    @property
    def skip_reasons(self) -> list[str]:
        return [reason.value for reason in SkipReason]
