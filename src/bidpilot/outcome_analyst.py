"""Understand *what the marketplace told us* and say it clearly to the user.

Every marketplace words its answers differently: a daily quota, an expired
subscription, a validation error, a layout change, a slow page, or plain success
with no confirmation text. Fixed regexes cover the common cases instantly; for
everything else the user's **own** LLM key reads the evidence (the notices the page
showed and a cleaned excerpt of the response) and classifies it.

The result is an :class:`Outcome`: a stable ``kind``, a short title, an
explanation, one concrete action, whether/when the bot will retry by itself, and
who produced the verdict (``rules`` or ``ai``) so the user can tell a certain
answer from an interpretation.

Safety rules for the AI path:
* it only ever *explains*; it never sends or retries anything,
* a limit/pause is trusted only when the evidence actually contains text (no
  evidence → no invented "daily limit"), and waits are clamped,
* one call per distinct message signature, 12 s budget, failures are silent.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .error_analyzer import analyze_error
from .jsonparse import parse_json_object

log = logging.getLogger(__name__)

#: kinds the bot knows how to act on
KINDS = (
    "submitted",
    "daily_limit",
    "subscription",
    "login",
    "validation",
    "duplicate",
    "slow_site",
    "site_changed",
    "browser",
    "javascript",
    "rejected",
    "unknown",
)

#: kinds after which polling the same site again soon is pointless or harmful
PAUSING_KINDS = frozenset({"daily_limit"})

_TEHRAN = timezone(timedelta(hours=3, minutes=30))  # Iran has no DST since 2022
_MIN_WAIT = 30 * 60
_MAX_WAIT = 24 * 3600


def seconds_until_site_reset(now: datetime | None = None) -> int:
    """Seconds until just after the next Tehran midnight (when daily quotas reset)."""
    now = (now or datetime.now(timezone.utc)).astimezone(_TEHRAN)
    nxt = (now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
    return max(_MIN_WAIT, int((nxt - now).total_seconds()))


def clamp_wait(seconds: float | int | None, default: int | None = None) -> int:
    try:
        value = int(float(seconds or 0))
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        return default if default is not None else seconds_until_site_reset()
    return max(_MIN_WAIT, min(value, _MAX_WAIT))


@dataclass
class Outcome:
    kind: str = "unknown"
    title: str = ""
    explanation: str = ""
    action: str = ""
    #: "now" | "later" | "never" — what the bot itself will do with this project
    retry: str = "later"
    #: for pausing kinds: how long the *site* should be left alone
    wait_seconds: int = 0
    #: "rules" (certain) | "ai" (interpretation of the page text)
    source: str = "rules"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def pauses_site(self) -> bool:
        return self.kind in PAUSING_KINDS


# ------------------------------------------------------------------ evidence
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def evidence_from_html(html: str, *, limit: int = 1200) -> dict[str, Any]:
    """Notices + a short cleaned excerpt of a response, for the analyst (never stored raw)."""
    from .sources import html_parser

    html = str(html or "")
    notices: list[str] = []
    try:
        notices = [*html_parser.extract_response_notices(html), *html_parser.extract_json_notices(html)]
    except Exception:  # noqa: BLE001 - evidence is best effort
        notices = []
    text = _WS.sub(" ", _TAG.sub(" ", html)).strip()
    return {
        "notices": [str(n).strip()[:240] for n in notices if str(n).strip()][:6],
        "excerpt": text[:limit],
    }


def evidence_from_notices(notices: list[str]) -> dict[str, Any]:
    return {"notices": [str(n).strip()[:240] for n in notices if str(n).strip()][:6], "excerpt": ""}


def _evidence_text(evidence: dict[str, Any] | None) -> str:
    if not evidence:
        return ""
    parts = [*(evidence.get("notices") or [])]
    if evidence.get("excerpt"):
        parts.append(str(evidence["excerpt"]))
    return " | ".join(str(p) for p in parts if p).strip()


# --------------------------------------------------------------------- rules
_DAILY = re.compile(
    r"روزانه\s*فقط|روزی\s*(?:یک|1|۱|\d+|[۰-۹]+)\s*پیشنهاد|محدودیت\s*روزانه|سقف\s*(?:ارسال\s*)?روزانه|"
    r"حد\s*نصاب\s*روزانه|امروز.*(?:سقف|حداکثر|بیش\s*از)|daily\s*limit",
    re.IGNORECASE,
)
_EXPIRED = re.compile(
    r"اشتراک.*(?:پایان|اتمام|منقضی|تمام)|اعتبار.*(?:کافی\s*نیست|ندارید|تمام)|نیاز\s*به\s*خرید\s*اشتراک|"
    r"subscription\s*(?:expired|required)|insufficient\s*credit",
    re.IGNORECASE,
)
_VALIDATION = re.compile(
    r"(?:حداقل|حداکثر).*(?:کاراکتر|کلمه|مبلغ|تومان|روز)|الزامی|نامعتبر|مجاز\s*نیست|باید.*باشد|required|invalid",
    re.IGNORECASE,
)


def classify_rules(message: str, evidence: dict[str, Any] | None = None, *, http_status: int | None = None) -> Outcome | None:
    """Instant, certain verdicts. ``None`` means "ask the AI (or fall back to generic)"."""
    ev = _evidence_text(evidence)
    text = f"{message} {ev}".strip()

    if _DAILY.search(text):
        return Outcome(
            "daily_limit",
            "سقف ارسال روزانهٔ حساب شما در این سایت پر شده است.",
            "خود سایت اجازهٔ ارسال بیشتر در امروز را نمی‌دهد؛ این محدودیت به حساب/اشتراک شما در همان سایت مربوط است، نه به BidPilot.",
            "ربات تا بازنشانی سایت این سایت را کنار می‌گذارد و خودکار ادامه می‌دهد. برای سقف بالاتر اشتراک خود سایت را ارتقا بده.",
            "later",
            seconds_until_site_reset(),
        )
    if _EXPIRED.search(text):
        return Outcome(
            "subscription",
            "اشتراک یا اعتبار حساب شما در سایت کافی نیست.",
            "سایت ارسال پیشنهاد را به‌خاطر وضعیت اشتراک/اعتبار حساب رد کرد.",
            "اشتراک یا اعتبار را در خود سایت تمدید کن، بعد /run بزن.",
            "never",
        )
    analysis = analyze_error(message, status_code=http_status)
    category = analysis.get("category", "unknown")
    mapping = {
        "submit_button_missing": ("site_changed", "later"),
        "browser_closed": ("browser", "later"),
        "javascript_required": ("javascript", "later"),
        "auth_expired": ("login", "never"),
        "already_bid": ("duplicate", "never"),
        "timeout": ("slow_site", "later"),
        "csrf_error": ("slow_site", "now"),
        "form_rejected": ("rejected", "later"),
    }
    if category in mapping:
        kind, retry = mapping[category]
        return Outcome(kind, analysis["title"], "", analysis.get("action", ""), retry)
    if ev and _VALIDATION.search(ev):
        return Outcome(
            "validation",
            "سایت فرم را به‌خاطر یک فیلد نامعتبر رد کرد.",
            f"پیام سایت: «{ev[:160]}»",
            "متن و مبلغ پیشنهاد را با شرط‌های همین سایت مقایسه کن؛ اگر تکرار شد این پیام را برای بررسی بفرست.",
            "later",
        )
    return None


# ------------------------------------------------------------------------ AI
_SYSTEM = (
    "تو تحلیلگر نتیجهٔ ارسال پیشنهاد در سایت‌های فریلنسری ایرانی هستی. "
    "فقط بر اساس شواهدی که داده می‌شود قضاوت کن؛ اگر شواهد کافی نیست kind را unknown بگذار و چیزی حدس نزن. "
    "خروجی فقط یک JSON است."
)

_CACHE: dict[str, Outcome | None] = {}


def _signature(site: str, message: str, evidence: dict[str, Any] | None) -> str:
    base = f"{site}|{message}|{_evidence_text(evidence)}"
    return re.sub(r"[\d۰-۹]+", "#", base)[:300]


def _build_prompt(site: str, message: str, evidence: dict[str, Any] | None, http_status: int | None) -> str:
    notices = "\n".join(f"- {n}" for n in (evidence or {}).get("notices", [])) or "(پیامی نمایش داده نشد)"
    excerpt = (evidence or {}).get("excerpt", "")[:900] or "(خالی)"
    return (
        f"سایت: {site}\nوضعیت HTTP: {http_status or '—'}\n"
        f"پیام ربات: {message[:400]}\n"
        f"پیام‌های دیده‌شده در صفحه:\n{notices}\n"
        f"گزیدهٔ پاسخ سایت: {excerpt}\n\n"
        "نتیجه را طبقه‌بندی کن. kind یکی از: "
        + ", ".join(k for k in KINDS if k != "submitted")
        + ".\nJSON با این کلیدها: "
        '{"kind": "...", "title": "یک جملهٔ کوتاه فارسی", "explanation": "علت دقیق در یک تا دو جمله", '
        '"action": "یک اقدام مشخص برای کاربر", "retry": "now|later|never", "wait_minutes": عدد یا 0}'
        "\nwait_minutes فقط وقتی پر شود که خود متن مدت انتظار یا بازنشانی را گفته باشد."
    )


def _from_ai(data: dict[str, Any], *, has_evidence: bool) -> Outcome | None:
    kind = str(data.get("kind", "")).strip().lower()
    if kind not in KINDS or kind == "submitted":
        kind = "unknown"  # an explanation is still useful; it just does not trigger any action
    title = str(data.get("title", "")).strip()[:200]
    if not title:
        return None
    retry = str(data.get("retry", "later")).strip().lower()
    if retry not in ("now", "later", "never"):
        retry = "later"
    wait = 0
    if kind in PAUSING_KINDS:
        if not has_evidence:  # no page text → never invent a pause
            return None
        minutes = data.get("wait_minutes") or 0
        wait = clamp_wait(float(minutes) * 60 if str(minutes).replace(".", "", 1).isdigit() else 0)
    return Outcome(
        kind,
        title,
        str(data.get("explanation", "")).strip()[:300],
        str(data.get("action", "")).strip()[:240],
        retry,
        wait,
        "ai",
    )


async def analyze_outcome(
    message: str,
    *,
    site: str = "",
    evidence: dict[str, Any] | None = None,
    http_status: int | None = None,
    llm_client: Any = None,
    timeout: float = 12.0,
) -> Outcome | None:
    """Rules first; the user's own model only for what the rules cannot place."""
    message = (message or "").strip()
    found = classify_rules(message, evidence, http_status=http_status)
    if found is not None and found.kind != "rejected":
        return found
    if llm_client is None or not (message or _evidence_text(evidence)):
        return found
    sig = _signature(site, message, evidence)
    if sig in _CACHE:
        return _CACHE[sig] or found
    outcome: Outcome | None = None
    try:
        resp = await asyncio.wait_for(
            llm_client.complete(_SYSTEM, _build_prompt(site, message, evidence, http_status), max_tokens=350),
            timeout=timeout,
        )
        data = parse_json_object(resp.text)
        if isinstance(data, dict):
            outcome = _from_ai(data, has_evidence=bool(_evidence_text(evidence)))
    except Exception as exc:  # noqa: BLE001 - explaining is a nicety, never a failure source
        log.debug("AI outcome analysis skipped: %s", type(exc).__name__)
    if len(_CACHE) > 256:
        _CACHE.clear()
    _CACHE[sig] = outcome
    return outcome or found


__all__ = [
    "KINDS",
    "Outcome",
    "analyze_outcome",
    "classify_rules",
    "clamp_wait",
    "evidence_from_html",
    "evidence_from_notices",
    "seconds_until_site_reset",
]
