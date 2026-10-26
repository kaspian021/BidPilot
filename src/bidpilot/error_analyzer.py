"""AI-assisted error analysis — turns raw site/LLM messages into clear Persian user guidance."""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# Known patterns -> (category, user_message, action)
_KNOWN: list[tuple[re.Pattern[str], str, str, str]] = [
    (
        re.compile(r"اشتراک.*(?:پایان|اتمام|منقضی|تمام)", re.IGNORECASE),
        "subscription_expired",
        "اشتراک پارس‌کدرز شما به پایان رسیده است.",
        "برای ادامهٔ ارسال پیشنهاد، اشتراک خود را در سایت تمدید کنید. تا تمدید نشود، رصد متوقف می‌ماند.",
    ),
    (
        re.compile(r"اعتبار.*کافی.*نیست|اعتبار.*ندارید", re.IGNORECASE),
        "insufficient_credit",
        "اعتبار حساب شما برای ارسال پیشنهاد کافی نیست.",
        "حساب خود را شارژ کنید یا اشتراک تهیه کنید.",
    ),
    (
        re.compile(r"سقف.*ارسال|محدودیت.*روزانه|ظرفیت.*ارسال|روزانه.*فقط.*(?:یک|1|۱)", re.IGNORECASE),
        "daily_limit",
        "سقف ارسال روزانه پر شده (حساب رایگان روزی ۱ پیشنهاد).",
        "فردا دوباره تلاش می‌شود؛ برای ارسال نامحدود اشتراک تهیه کنید.",
    ),
    (
        re.compile(r"قبل[اًا].*پیشنهاد", re.IGNORECASE),
        "already_bid",
        "شما قبلاً برای این پروژه پیشنهاد داده‌اید.",
        "نیازی به اقدام نیست.",
    ),
    (
        re.compile(r"javascript|جاوا اسکریپت.*(?:فعال|غیر فعال)", re.IGNORECASE),
        "javascript_required",
        "سایت اجرای JavaScript در مرورگر را لازم دانست.",
        "این معمولاً موقتی است؛ سیستم پس از مکث خودکار دوباره تلاش می‌کند.",
    ),
    (
        re.compile(r"احراز هویت|نشست.*منقضی|وارد.*حساب", re.IGNORECASE),
        "auth_expired",
        "نشست ورود شما منقضی شده است.",
        "کوکی/ورود را در /market به‌روزرسانی و با /test بررسی کنید.",
    ),
    (
        re.compile(r"CSRF|توکن.*امنیتی|Page Expired|419", re.IGNORECASE),
        "csrf_error",
        "توکن امنیتی فرم رد شد (ممکن است فرم قدیمی باشد).",
        "در اجرای بعدی خودکار دوباره تلاش می‌شود.",
    ),
    (
        re.compile(r"پاسخ.*دریافت نشد|timeout|زمان.*تمام", re.IGNORECASE),
        "timeout",
        "پاسخ سایت به موقع دریافت نشد (ممکن است درخواست ثبت شده باشد).",
        "وضعیت پروژه را دستی در سایت بررسی کنید؛ برای جلوگیری از تکرار، دوباره تلاش نشد.",
    ),
]


def analyze_error(raw_message: str, *, html_snippet: str = "", status_code: int | None = None) -> dict[str, str]:
    """Classify a raw error and return a user-friendly Persian explanation.

    Returns dict with keys: category, title, message, action, raw.
    The LLM hook is intentionally not called here synchronously — pipeline
    latency is critical, so we use heuristics first and let the caller decide
    when to invoke the model for unknown errors.
    """
    text = f"{raw_message} {html_snippet}".strip()
    if status_code:
        text += f" HTTP {status_code}"

    for pattern, category, title, action in _KNOWN:
        if pattern.search(text):
            return {
                "category": category,
                "title": title,
                "message": f"{title} {action}",
                "action": action,
                "raw": raw_message[:300],
            }

    # Unknown: provide a generic but still helpful message
    # (caller may optionally invoke LLM later for deeper analysis)
    if status_code and 400 <= status_code < 500:
        return {
            "category": "client_error",
            "title": f"سایت درخواست را رد کرد (HTTP {status_code}).",
            "message": f"سایت درخواست را رد کرد (HTTP {status_code}): {raw_message[:180]}",
            "action": "پیام کامل در لاگ ثبت شد؛ اگر تکرار شد، جزئیات را بررسی کنید.",
            "raw": raw_message[:300],
        }
    if status_code and status_code >= 500:
        return {
            "category": "server_error",
            "title": "خطای سرور سایت (موقتی).",
            "message": "سایت فعلاً خطای داخلی دارد؛ در دور بعدی دوباره تلاش می‌شود.",
            "action": "نیازی به اقدام دستی نیست.",
            "raw": raw_message[:300],
        }

    return {
        "category": "unknown",
        "title": "خطای نامشخص هنگام ارسال پیشنهاد.",
        "message": raw_message[:200] or "دلیل مشخصی از سایت دریافت نشد.",
        "action": "جزئیات در لاگ ثبت شد؛ در صورت تکرار با پشتیبانی تماس بگیرید.",
        "raw": raw_message[:300],
    }


def format_for_user(analysis: dict[str, str], *, with_action: bool = True) -> str:
    """One-line or two-line Persian message for Telegram/log."""
    title = analysis.get("title", "")
    action = analysis.get("action", "")
    if with_action and action and action not in title:
        return f"{title} {action}"
    return title


async def analyze_with_llm(raw_message: str, llm_client=None, *, system_prompt: str = "") -> dict[str, str] | None:
    """Optional: ask the LLM to explain an unknown error in Persian.

    This is intentionally separate from the sync heuristic path so the
    scheduler never blocks on it. Call it only for uncategorized errors
    when the user explicitly wants AI analysis.
    """
    if llm_client is None:
        return None
    try:
        prompt = (
            "یک خطای ارسال پیشنهاد از سایت پارس‌کدرز رخ داده است. "
            "آن را در یک جمله فارسی واضح برای کاربر توضیح بده و یک اقدام پیشنهادی بگو.\n"
            f"متن خطا: {raw_message[:500]}\n"
            "خروجی فقط JSON با کلیدهای title و action"
        )
        system = system_prompt or "تو دستیار پشتیبانی فارسی BidPilot هستی."
        resp = await llm_client.complete(system, prompt, max_tokens=200)
        import json

        data = json.loads(resp.text)
        if isinstance(data, dict) and "title" in data:
            return {
                "category": "llm_analyzed",
                "title": str(data.get("title", ""))[:200],
                "message": f"{data.get('title','')} {data.get('action','')}".strip(),
                "action": str(data.get("action", ""))[:200],
                "raw": raw_message[:300],
            }
    except Exception as exc:
        log.debug("LLM error analysis failed: %s", exc)
    return None
