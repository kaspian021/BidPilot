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
from .sources.base import (
    AuthenticationRequiredError,
    JavaScriptRequiredError,
    Source,
    SourceError,
)
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
    #: True when the account session is invalid; the scheduler must stop searching.
    authentication_failed: bool = False
    #: True when the site (transiently) demands browser JavaScript; the
    #: scheduler pauses this source with a cooldown instead of stopping.
    javascript_required: bool = False
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
        # Authentication failures are surfaced by the runner as a dedicated,
        # actionable message rather than a duplicate generic digest.
        if self.authentication_failed or self.javascript_required:
            return False
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
            # Never enter live project polling/submission with an unverified session.
            if self.live:
                try:
                    session_ok, session_detail = await source.verify_session()
                except Exception as exc:
                    session_ok = False
                    session_detail = f"بررسی نشست با خطای {type(exc).__name__} متوقف شد."
                log.info(
                    "[PIPELINE][SESSION-CHECK] source=%s live=%s ok=%s detail=%r",
                    source.name,
                    self.live,
                    session_ok,
                    session_detail,
                )
                if not session_ok:
                    detail = session_detail or "وضعیت ورود تأیید نشد."
                    raise SourceError(f"ارسال واقعی متوقف شد؛ احراز هویت «{source.name}» تأیید نشد: {detail}")

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
                    report.errors.append(f"{project.uid}: {str(exc).strip() or type(exc).__name__}")
                if report.authentication_failed or report.javascript_required:
                    stop_reason = "STOP-AUTH" if report.authentication_failed else "PAUSE-JAVASCRIPT"
                    log.error(
                        "[PIPELINE][%s] source=%s stopping the current project batch",
                        stop_reason,
                        source.name,
                    )
                    break
        except Exception as exc:  # fetch/auth failure for the whole source
            detail = str(exc).strip() or type(exc).__name__
            report.errors.append(detail)
            report.authentication_failed = isinstance(exc, AuthenticationRequiredError) or any(
                marker in detail.casefold()
                for marker in ("احراز هویت", "نشست منقضی", "وارد حساب کاربری", "ورود به حساب")
            )
            # A JS interstitial on the *listing* page used to fall through as a
            # generic error and be retried (hammered) every poll interval.
            report.javascript_required = isinstance(exc, JavaScriptRequiredError)
            log.error(
                "Source cycle failed for %s: %s",
                source.name,
                detail,
                exc_info=log.isEnabledFor(logging.DEBUG),
            )
            if not (report.authentication_failed or report.javascript_required):
                await self._notify_error(source.name, f"اجرای منبع «{source.name}» ناموفق بود: {detail}")
            else:
                log.error(
                    "[PIPELINE][AUTH-REQUIRED] source=%s no project search or bidding will continue",
                    source.name,
                )
        finally:
            await self.repo.commit()  # persist whatever happened (incl. error events)
        log.info(report.summary())
        return report

    async def _filter_skip_plan(self, project: Project, report: RunReport) -> BidPlan | None:
        """Evaluate the filters; return a SKIPPED plan when the project is out."""
        decision = self.project_filter.evaluate(project)
        log.debug(
            "[PIPELINE][FILTER] source=%s id=%s title=%r allowed=%s reason=%r "
            "require_budget=%s require_skill_match=%s profile_skills=%r",
            project.source,
            project.external_id,
            project.title,
            decision.allowed,
            decision.reason,
            self.config.filters.require_budget,
            self.config.filters.require_skill_match,
            self.project_filter.skill_keywords,
        )
        if decision.allowed:
            return None
        log.info(
            "[PIPELINE][SKIPPED] source=%s id=%s title=%r reason=%r",
            project.source,
            project.external_id,
            project.title,
            decision.reason,
        )
        report.skipped += 1
        plan = BidPlan(
            project=project,
            result=SubmissionResult(status=SubmissionStatus.SKIPPED, message=decision.reason),
        )
        await self._emit_skip(project, decision.reason, report)
        return plan

    # -------------------------------------------------------------- one item
    async def process_project(self, project: Project, report: RunReport) -> BidPlan | None:
        # 1) Filter first; a rejected/observed project is never persisted.
        budget_debug = project.budget.model_dump() if project.budget else None
        raw = project.raw or {}
        log.debug(
            "[PIPELINE][CANDIDATE] source=%s id=%s title=%r url=%s "
            "budget=%r raw_budget_text=%r detail_budget_selector_text=%r "
            "detail_budget_text_used=%r detail_budget_source=%r skills=%r "
            "category=%r deadline=%r description_chars=%s",
            project.source,
            project.external_id,
            project.title,
            project.url,
            budget_debug,
            raw.get("budget_text", ""),
            raw.get("detail_budget_selector_text", ""),
            raw.get("detail_budget_text_used", ""),
            raw.get("detail_budget_source", ""),
            project.skills,
            project.category,
            project.employer_deadline,
            len(project.description or ""),
        )
        skipped_plan = await self._filter_skip_plan(project, report)
        if skipped_plan is not None:
            return skipped_plan

        # 2) Only now — for a project we might actually bid on — pay for the
        # detail page (full description, exact budget range, deadline).
        # Fetching details for every listing card used to mean ~25 extra
        # requests per poll for projects that were mostly filtered out, which
        # is what got the client throttled/disconnected by the marketplace.
        enrich = getattr(self.sources.get(project.source), "enrich_project", None)
        if enrich is not None:
            try:
                enriched = await enrich(project)
            except Exception as exc:  # detail data is an upgrade, not a gate
                log.warning(
                    "[PIPELINE][ENRICH-FAILED] source=%s id=%s error_type=%s",
                    project.source,
                    project.external_id,
                    type(exc).__name__,
                )
                enriched = project
            if enriched is not project:
                project = enriched
                budget_debug = project.budget.model_dump() if project.budget else None
                raw = project.raw or {}
                # The exact budget range/skills from the detail page can
                # legitimately change the verdict (e.g. max-only -> range).
                skipped_plan = await self._filter_skip_plan(project, report)
                if skipped_plan is not None:
                    return skipped_plan

        # The marketplace project page is the only authority for whether we
        # already bid. The database stores submission history for rate limits,
        # but must never determine whether a scanned project is a duplicate.
        bid_state: bool | None = False
        if self.live:
            try:
                bid_state = await self.sources[project.source].check_bid_status(project)
            except SourceError as exc:
                detail = str(exc).strip() or type(exc).__name__
                folded = detail.casefold()
                # Prefer the typed errors; substring matching on human-readable
                # Persian text stays only as a fallback for third-party sources.
                if isinstance(exc, AuthenticationRequiredError) or any(
                    marker in folded
                    for marker in ("احراز هویت", "نشست منقضی", "وارد حساب کاربری", "ورود به حساب")
                ):
                    if await self._session_really_dead(project.source):
                        report.authentication_failed = True
                        report.errors.append("ورود به حساب تأیید نشد؛ رصد و ارسال پیشنهاد متوقف می‌شود.")
                        log.error(
                            "[PIPELINE][AUTH-REQUIRED] source=%s id=%s stage=bid_status_check",
                            project.source,
                            project.external_id,
                        )
                    else:
                        # The account page proves we are logged in; the status
                        # check hit something else (JS gate/wrong endpoint).
                        # Skip this project once, never kill the scheduler.
                        reason = (
                            "بررسی وضعیت پیشنهاد ناموفق بود، اما ورود به حساب سالم است؛ "
                            "این پروژه این دور رد شد."
                        )
                        report.skipped += 1
                        await self._emit_skip(project, reason, report)
                        log.warning(
                            "[PIPELINE][AUTH-FALSE-ALARM] source=%s id=%s stage=bid_status_check",
                            project.source,
                            project.external_id,
                        )
                elif isinstance(exc, JavaScriptRequiredError):
                    report.javascript_required = True
                    report.errors.append(
                        "صفحهٔ پروژه اجرای JavaScript در مرورگر را لازم دانست؛ "
                        "این منبع موقتاً متوقف می‌شود و خودکار دوباره تلاش می‌کند."
                    )
                    log.error(
                        "[PIPELINE][PAUSE-JAVASCRIPT] source=%s id=%s stage=bid_status_check",
                        project.source,
                        project.external_id,
                    )
                else:
                    reason = f"وضعیت پیشنهاد از صفحه قابل بررسی نبود؛ این بار ارسال نمی‌شود ({detail[:100]})."
                    report.skipped += 1
                    await self._emit_skip(project, reason, report)
                    log.warning(
                        "[PIPELINE][BID-STATUS-UNKNOWN] source=%s id=%s error=%r",
                        project.source,
                        project.external_id,
                        detail[:180],
                    )
                return None

        if bid_state is True:
            report.duplicates += 1
            log.info(
                "[PIPELINE][ALREADY-BIDDED] source=%s id=%s detected_by=project_page",
                project.source,
                project.external_id,
            )
            return None

        if bid_state is None and getattr(self.sources[project.source], "requires_bid_status_check", False):
            reason = "وضعیت پیشنهاد در صفحهٔ پروژه قابل تشخیص نبود؛ برای جلوگیری از ارسال تکراری رد شد."
            report.skipped += 1
            await self._emit_skip(project, reason, report)
            log.warning(
                "[PIPELINE][BID-STATUS-UNKNOWN] source=%s id=%s action=skip_to_avoid_duplicate",
                project.source,
                project.external_id,
            )
            return None

        # This count describes the current run; it is not persisted or deduped.
        report.new += 1

        if not self.config.runtime.notify_digest:
            await self.notifiers.dispatch(
                Event(kind=EventKind.NEW_PROJECT, title=project.title, project=project)
            )

        # 3) pricing
        pricing = self.pricing.price(project, project.budget)
        log.info(
            "[PIPELINE][PRICING] source=%s id=%s title=%r budget=%r strategy=%s result=%r",
            project.source,
            project.external_id,
            project.title,
            budget_debug,
            getattr(self.pricing, "name", type(self.pricing).__name__),
            pricing.model_dump() if pricing is not None else None,
        )
        if pricing is None:
            log.warning(
                "[PIPELINE][NO-PRICE] source=%s id=%s title=%r budget=%r raw=%r",
                project.source,
                project.external_id,
                project.title,
                budget_debug,
                raw,
            )
            report.skipped += 1
            plan = BidPlan(
                project=project,
                result=SubmissionResult(
                    status=SubmissionStatus.SKIPPED,
                    message="قیمت‌گذاری امکان‌پذیر نبود.",
                ),
            )
            await self._emit_skip(project, "قیمت‌گذاری امکان‌پذیر نبود.", report)
            return plan

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
            await self._emit_skip(project, rate.reason, report)
            return plan

        # 6) submit (or dry run)
        started = time.monotonic()
        if not self.live:
            plan.result = SubmissionResult(
                status=SubmissionStatus.DRY_RUN, message="حالت آزمایشی: پیشنهاد ارسال نشد."
            )
            report.dry_run += 1
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
        if self.live and any(
            marker in str(result.message or "").casefold()
            for marker in (
                "نشست منقضی شده",
                "نشست فعلی به‌عنوان واردشده تأیید نشد",
                "به مسیر ورود هدایت می‌شود",
                "ورود به حساب کاربری نیست",
            )
        ):
            if await self._session_really_dead(project.source):
                report.authentication_failed = True
                report.errors.append("ورود به حساب کاربری تأیید نشد؛ جست‌وجو و ارسال پیشنهاد متوقف می‌شود.")
                log.error(
                    "[PIPELINE][AUTH-REQUIRED] source=%s id=%s stopping after response indicated login/session failure",
                    project.source,
                    project.external_id,
                )
            else:
                # The POST bounced to /login but the session verifies fine:
                # an ordinary bid failure (JS gate / changed flow), not a
                # dead cookie. Count it as failed and keep the batch alive.
                log.warning(
                    "[PIPELINE][AUTH-FALSE-ALARM] source=%s id=%s submit bounced to login "
                    "but the session is verified; treating as an ordinary bid failure",
                    project.source,
                    project.external_id,
                )
        if result.javascript_required:
            report.javascript_required = True
            report.errors.append(
                "سایت اجرای JavaScript در مرورگر را لازم دانست؛ "
                "این منبع موقتاً متوقف می‌شود و خودکار دوباره تلاش می‌کند."
            )
            log.error(
                "[PIPELINE][PAUSE-JAVASCRIPT] source=%s id=%s pausing after JavaScript requirement",
                project.source,
                project.external_id,
            )

        if result.status == SubmissionStatus.SUBMITTED:
            # Keep a minimal project identity only, so the bid row can reference
            # the marketplace item without storing the full scraped page data.
            project_row = await self.repo.get_project_row_by_uid(project.source, project.external_id)
            if project_row is None:
                project_row = await self.repo.save_project(project)
            await self.repo.save_bid(project_row.id, plan)
            report.submitted += 1
        else:
            # Failed attempts stay in logs/reports only; persist accepted bids only.
            report.failed += 1
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
    async def _session_really_dead(self, source_name: str) -> bool:
        """Confirm a suspected auth failure with the account page itself.

        A submit/status redirect to /login is *not* proof the cookie died:
        in production the ParsCoders POST bounced to /login (JavaScript
        gate / wrong endpoint) while /dashboard verified the same session
        seconds earlier. Hard-stopping the scheduler on that guess sent the
        user through futile cookie-refresh loops. Only a failed
        ``verify_session`` may declare authentication dead; a verification
        *error* stays conservative (treats the session as dead).
        """
        source = self.sources.get(source_name)
        if source is None:
            return True
        try:
            ok, detail = await source.verify_session()
        except Exception as exc:
            log.warning(
                "[PIPELINE][AUTH-CONFIRM-FAILED] source=%s error_type=%s",
                source_name,
                type(exc).__name__,
            )
            return True
        log.info(
            "[PIPELINE][AUTH-CONFIRM] source=%s session_ok=%s detail=%r",
            source_name,
            ok,
            detail,
        )
        return not ok

    def _note_skip(self, report: RunReport, reason: str) -> None:
        key = (reason or "دلیل نامشخص").strip()
        report.skip_reasons[key] = report.skip_reasons.get(key, 0) + 1

    async def _emit_skip(self, project: Project, reason: str, report: RunReport | None = None) -> None:
        if report is not None:
            self._note_skip(report, reason)
        # Skip decisions are summarized in logs/digests, never persisted per project.
        if self.config.runtime.notify_digest or not self.config.runtime.notify_on_skip:
            return
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
