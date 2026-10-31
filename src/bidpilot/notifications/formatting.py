"""Event -> human-readable message (Telegram HTML flavour, reused by console/webhook)."""

from __future__ import annotations

from html import escape

from ..error_analyzer import analyze_error
from ..models import BidPlan, Event, EventKind, Project, SubmissionStatus
from ..pricing.budget import format_toman
from ..text import to_persian_digits, truncate

_STATUS_FA = {
    SubmissionStatus.SUBMITTED.value: "✅ پیشنهاد ثبت شد",
    SubmissionStatus.DRY_RUN.value: "🧪 آزمایشی (بدون ارسال)",
    SubmissionStatus.FAILED.value: "❌ ارسال ناموفق",
    SubmissionStatus.SKIPPED.value: "⏭ رد شد",
}


def _e(text: str | None) -> str:
    return escape(text or "", quote=False)


def _project_block(project: Project) -> str:
    budget = project.budget.as_text() if project.budget else "اعلام نشده"
    lines = [
        f"<b>{_e(project.title)}</b>",
        f"💰 بودجه کارفرما: {_e(budget)}",
    ]
    if project.skills:
        lines.append(f"🧩 مهارت‌ها: {_e('، '.join(project.skills[:6]))}")
    if project.category:
        lines.append(f"📂 دسته: {_e(project.category)}")
    if project.url:
        lines.append(f"🔗 {_e(project.url)}")
    return "\n".join(lines)


def _bid_block(bid: BidPlan) -> str:
    status = bid.result.status.value if bid.result else SubmissionStatus.SKIPPED.value
    lines = [
        f"<b>{_STATUS_FA.get(status, status)}</b>",
        f"💵 قیمت پیشنهادی: {_e(format_toman(bid.amount))}",
    ]
    if bid.delivery_days:
        lines.append(f"⏱ تحویل: {to_persian_digits(str(bid.delivery_days))} روز")
    if bid.rationale:
        lines.append(f"📐 منطق قیمت: {_e(bid.rationale)}")
    if bid.result and bid.result.message:
        lines.append(f"📝 نتیجه سایت: {_e(bid.result.message)}")
        if status == SubmissionStatus.FAILED.value:
            ai = (bid.result.payload_debug or {}).get("analysis")
            analysis = ai if isinstance(ai, dict) and ai.get("title") else analyze_error(
                bid.result.message, status_code=bid.result.http_status
            )
            if analysis.get("category") != "unknown":
                lines.append(f"🔍 علت: {_e(analysis.get('title'))}")
                if analysis.get("action"):
                    lines.append(f"🛠 اقدام: {_e(analysis.get('action'))}")
    if bid.proposal:
        lines.append(f"🤖 منبع متن: {_e(bid.proposal.generated_by)}")
    return "\n".join(lines)


def render_event(event: Event, *, full_proposal: bool = True) -> str:
    header = {
        EventKind.NEW_PROJECT: "🆕 پروژه جدید",
        EventKind.BID_SUBMITTED: "🚀 پیشنهاد ارسال شد",
        EventKind.BID_DRY_RUN: "🧪 پیشنهاد آماده شد (حالت آزمایشی)",
        EventKind.BID_SKIPPED: "⏭ پروژه رد شد",
        EventKind.ERROR: "⚠️ خطا",
        EventKind.DIGEST: "📊 گزارش",
    }.get(event.kind, event.kind.value)

    parts = [f"{header}", "──────────────"]
    if event.project:
        parts.append(_project_block(event.project))
        parts.append("──────────────")
    if event.bid:
        parts.append(_bid_block(event.bid))
    if event.body:
        parts.append("──────────────")
        parts.append(_e(truncate(event.body, 1_600)))
    if full_proposal and event.bid and event.bid.proposal and event.bid.proposal.text:
        parts.append("──────────────")
        parts.append("<b>متن پیشنهاد:</b>")
        parts.append(_e(truncate(event.bid.proposal.text, 3_200)))
    return "\n".join(p for p in parts if p)
