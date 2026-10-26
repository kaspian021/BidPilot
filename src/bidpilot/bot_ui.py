"""Professional Telegram UI kit — consistent cards, menus, and progress.

This module is pure rendering: no network, no DB logic. It lets bot.py stay
thin while the user sees a premium, cohesive interface.
"""

from __future__ import annotations

import html
from datetime import datetime

from .subscriptions.plans import Plan, all_plans, get_plan

# ── brand ──────────────────────────────────────────────────────────
BRAND = "BidPilot"
TAGLINE = "دستیار هوشمند پیشنهاددهی فریلنسرها"
FOOTER = f"✦ {BRAND} — {TAGLINE}"

# Palette emojis for hierarchy
EMO = {
    "rocket": "🚀",
    "gem": "💎",
    "crown": "👑",
    "star": "⭐",
    "free": "🆓",
    "check": "✅",
    "cross": "❌",
    "warn": "⚠️",
    "info": "ℹ️",
    "chart": "📊",
    "folder": "📁",
    "gear": "⚙️",
    "user": "👤",
    "site": "🌐",
    "filter": "🧹",
    "tone": "🎭",
    "price": "💰",
    "time": "⏱",
    "lock": "🔒",
    "spark": "✨",
    "arrow": "➡️",
    "back": "↩️",
    "home": "🏠",
}

# ── helpers ────────────────────────────────────────────────────────
def esc(s: str) -> str:
    return html.escape(s or "")

def hr() -> str:
    return "─" * 28

def progress_bar(done: int, total: int, width: int = 12) -> str:
    if total <= 0:
        return "░" * width
    ratio = max(0.0, min(1.0, done / total))
    filled = int(round(ratio * width))
    return "█" * filled + "░" * (width - filled) + f" {int(ratio*100)}%"

def badge_for_plan(plan: Plan) -> str:
    return plan.badge or ("🆓" if plan.is_free() else "⭐")

# ── main menu ──────────────────────────────────────────────────────
def main_menu_text(first_name: str | None = None, plan_title: str | None = None) -> str:
    greet = f" سلام {esc(first_name)} عزیز" if first_name else " خوش آمدید"
    plan_line = f"\n{EMO['gem']} پلن فعال: <b>{esc(plan_title)}</b>" if plan_title else ""
    return (
        f"<b>🏠 منوی اصلی — {BRAND}</b>\n"
        f"<i>{TAGLINE}</i>{plan_line}\n"
        f"{hr()}\n"
        f"{greet} 👋\n"
        f"یک دستیار حرفه‌ای برای پیدا کردن پروژه، نوشتن پیشنهاد و ارسال خودکار — بدون نیاز به فایل .env\n\n"
        f"<b>چه کاری می‌خواهید انجام دهید؟</b>\n"
        f"• <b>شروع فعالیت</b> برای رصد خودکار پروژه‌ها\n"
        f"• <b>پروفایل</b> برای معرفی مهارت‌ها\n"
        f"• <b>پلن‌ها</b> برای افزایش سقف پیشنهاد\n"
    )

def main_menu_keyboard(is_live: bool = False) -> dict:
    return {
        "inline_keyboard": [
            [{"text": "🚀  شروع فعالیت", "callback_data": "run:ask"}],
            [{"text": "🧪  پیش‌نمایش پیشنهاد", "callback_data": "cmd:preview"}, {"text": "📊  داشبورد", "callback_data": "cmd:status"}],
            [{"text": "👤  پروفایل", "callback_data": "cmd:profile"}, {"text": "🎭  لحن و سبک", "callback_data": "cmd:style"}],
            [{"text": "🌐  سایت‌های فریلنسری", "callback_data": "cmd:sites"}, {"text": "🧹  فیلترها", "callback_data": "cmd:filters"}],
            [{"text": "💳  پلن‌ها و اشتراک", "callback_data": "cmd:plans"}, {"text": "⚙️  تنظیمات", "callback_data": "cmd:settings"}],
            [{"text": "ℹ️  راهنما", "callback_data": "cmd:help"}, {"text": "⏸  توقف", "callback_data": "run:stop"}],
        ]
    }

# ── status / dashboard ───────────────────────────────────────────
def status_card(status_text: str, plan: Plan | None = None, usage: dict | None = None) -> str:
    # status_text already is rich; wrap it in a card frame
    header = f"<b>📊 داشبورد {BRAND}</b>  <i>· زنده</i>\n{hr()}\n"
    plan_box = ""
    if plan:
        plan_box = (
            f"{badge_for_plan(plan)} <b>پلن {esc(plan.title)}</b> — {esc(plan.tagline)}\n"
            f"   {esc(plan.formatted_price)} · <i>{plan.price_per_day}</i>\n"
        )
        if usage:
            # usage expected keys: per_hour, per_day, per_month counts
            ph, pd = usage.get("per_hour", 0), usage.get("per_day", 0)
            lh, ld = plan.limits.max_bids_per_hour, plan.limits.max_bids_per_day
            plan_box += f"   ساعتی {progress_bar(ph, lh)} {ph}/{lh}  ·  روزانه {progress_bar(pd, ld)} {pd}/{ld}\n"
        plan_box += f"{hr()}\n"
    return header + plan_box + status_text + f"\n\n<i>{FOOTER}</i>"

# ── plans showcase ───────────────────────────────────────────────
def plans_overview_text() -> str:
    lines = [f"<b>💳 پلن‌های {BRAND}</b> — هر کاربر حساب و سقف جدا دارد", f"<i>{TAGLINE}</i>", hr(), ""]
    for plan in all_plans():
        rec = "  <b>★ پیشنهاد ما</b>" if plan.recommended else ""
        lines.append(
            f"{badge_for_plan(plan)} <b>{esc(plan.title)}</b>{rec} — {esc(plan.tagline)}\n"
            f"   {esc(plan.formatted_price)}  ·  {esc(plan.price_per_day)}\n"
            f"   {plan.limits.max_bids_per_hour}/ساعت · {plan.limits.max_bids_per_day}/روز · {plan.limits.max_bids_per_month}/ماه\n"
        )
        # first 3 features only to keep card compact
        feats = " · ".join(plan.features[:3])
        lines.append(f"   <i>{esc(feats)}</i>\n")
    lines.append(f"{hr()}\n" "روی هر پلن بزنید تا جزئیات کامل و پرداخت زرین‌پال را ببینید.\n<i>پلن رایگان: ۵ پیشنهاد/ساعت — فقط برای شروع.</i>")
    return "\n".join(lines)

def plans_keyboard() -> dict:
    rows = []
    for plan in all_plans():
        label = f"{badge_for_plan(plan)} {plan.title} — {plan.formatted_price}"
        if plan.recommended:
            label += " ★"
        rows.append([{"text": label, "callback_data": f"plan:{plan.id}"}])
    rows.append([{"text": "🧾 پرداخت‌های من", "callback_data": "payments:history"}])
    rows.append([{"text": "↩️ بازگشت به منو", "callback_data": "cmd:home"}])
    return {"inline_keyboard": rows}

def plan_detail_text(plan: Plan) -> str:
    header = f"{badge_for_plan(plan)} <b>{esc(plan.title)}</b> — {esc(plan.tagline)}\n"
    price = f"<b>{esc(plan.formatted_price)}</b>  <i>({plan.period_days} روزه · {esc(plan.price_per_day)})</i>\n"
    limits = (
        f"{hr()}\n<b>سقف‌ها</b>\n"
        f"• {plan.limits.max_bids_per_hour} پیشنهاد در ساعت\n"
        f"• {plan.limits.max_bids_per_day} در روز\n"
        f"• {plan.limits.max_bids_per_month} در ماه\n"
        f"• فاصله ایمن: {plan.limits.min_seconds_between_bids} ثانیه\n"
        f"• سایت فعال: {'نامحدود' if plan.limits.max_active_sites > 3 else plan.limits.max_active_sites}\n"
    )
    feats = f"{hr()}\n<b>ویژگی‌ها</b>\n" + "\n".join(f"• {esc(f)}" for f in plan.features)
    note = ""
    if plan.is_free():
        note = f"\n\n<i>این پلن رایگان است — همین حالا فعال است. برای سقف بالاتر یکی از پلن‌های پولی را انتخاب کنید.</i>"
    return header + price + limits + feats + note + f"\n\n<i>{FOOTER}</i>"

def plan_detail_keyboard(plan: Plan) -> dict:
    if plan.is_free():
        return {"inline_keyboard": [[{"text": "✅ فعال است (رایگان)", "callback_data": "cmd:plans"}], [{"text": "↩️ بازگشت", "callback_data": "cmd:plans"}]]}
    return {
        "inline_keyboard": [
            [{"text": f"💳 پرداخت {plan.formatted_price} با زرین‌پال", "callback_data": f"buy:{plan.id}"}],
            [{"text": "📊 مقایسه پلن‌ها", "callback_data": "plans:compare"}],
            [{"text": "↩️ بازگشت", "callback_data": "cmd:plans"}],
        ]
    }

def compare_table_text() -> str:
    lines = [f"<b>📊 مقایسه پلن‌ها</b>", hr(), ""]
    # header
    header = "ویژگی" + "".join(f"  |  {esc(p.title)}" for p in all_plans())
    lines.append(f"<code>{header}</code>")
    lines.append(hr())
    rows = [
        ("ساعت", [str(p.limits.max_bids_per_hour) for p in all_plans()]),
        ("روز", [str(p.limits.max_bids_per_day) for p in all_plans()]),
        ("ماه", [str(p.limits.max_bids_per_month) for p in all_plans()]),
        ("سایت", [("نامحدود" if p.limits.max_active_sites > 3 else str(p.limits.max_active_sites)) for p in all_plans()]),
        ("فاصله", [f"{p.limits.min_seconds_between_bids}s" for p in all_plans()]),
    ]
    for label, vals in rows:
        line = f"{label:<6}" + "".join(f"  |  {v:>6}" for v in vals)
        lines.append(f"<code>{line}</code>")
    lines.append(f"\n<i>برای خرید: روی هر پلن بزنید → پرداخت زرین‌پال (درگاه امن شاپرک)</i>")
    return "\n".join(lines)

# ── payment ──────────────────────────────────────────────────────
def payment_created_text(plan: Plan, payment_url: str, authority: str) -> str:
    return (
        f"<b>💳 پرداخت {esc(plan.title)} — {esc(plan.formatted_price)}</b>\n"
        f"{hr()}\n"
        f"لینک امن زرین‌پال برای شما ساخته شد. روی دکمه زیر بزنید، پرداخت را کامل کنید و سپس <b>بررسی پرداخت</b> را بزنید.\n\n"
        f"🔗 <a href=\"{payment_url}\">{payment_url}</a>\n"
        f"کد پیگیری: <code>{esc(authority)}</code>\n\n"
        f"<i>نکته: اگر درگاه زرین‌پال هنوز فعال نکرده‌اید، راهنما را ببینید — بدون مرچنت‌کد، پرداخت شبیه‌سازی می‌شود.</i>"
    )

def payment_keyboard(authority: str, payment_url: str) -> dict:
    return {
        "inline_keyboard": [
            [{"text": "🔗 رفتن به درگاه زرین‌پال", "url": payment_url}],
            [{"text": "✅ بررسی پرداخت", "callback_data": f"verify:{authority}"}],
            [{"text": "📖 راهنمای زرین‌پال", "callback_data": "zarinpal:guide"}],
            [{"text": "↩️ بازگشت به پلن‌ها", "callback_data": "cmd:plans"}],
        ]
    }

def payment_verified_text(plan: Plan, ref_id: str, expires: datetime | None) -> str:
    exp_line = f"\n⏰ اعتبار تا: <b>{expires.strftime('%Y/%m/%d %H:%M') if expires else '—'}</b>" if expires else ""
    return (
        f"<b>✅ پرداخت تایید شد!</b>\n"
        f"{hr()}\n"
        f"{badge_for_plan(plan)} پلن <b>{esc(plan.title)}</b> فعال شد.\n"
        f"کد مرجع: <code>{esc(ref_id)}</code>{exp_line}\n\n"
        f"از این لحظه سقف ارسال شما افزایش یافت:\n"
        f"• {plan.limits.max_bids_per_hour}/ساعت · {plan.limits.max_bids_per_day}/روز\n\n"
        f"<i>ممنون از اعتماد شما — {FOOTER}</i>"
    )

# ── misc ─────────────────────────────────────────────────────────
def help_text() -> str:
    return (
        f"<b>{BRAND} — راهنمای حرفه‌ای</b>\n"
        f"<i>{TAGLINE}</i>\n{hr()}\n"
        f"<b>شروع سریع (۱ دقیقه)</b>\n"
        f"1️⃣  /start → منوی اصلی\n"
        f"2️⃣  پروفایل را کامل کنید (نام، سابقه، مهارت)\n"
        f"3️⃣  حساب سایت را وصل کنید (/market)\n"
        f"4️⃣  پلن را انتخاب کنید — رایگان برای تست، پولی برای حجم بالا\n"
        f"5️⃣  روی <b>شروع فعالیت</b> بزنید\n\n"
        f"<b>دستورات کلیدی</b>\n"
        f"• /status — داشبورد زنده\n"
        f"• /plans — مشاهده و خرید پلن (زرین‌پال)\n"
        f"• /preview — پیش‌نمایش پیشنهاد بدون ارسال\n"
        f"• /run /stop — کنترل رصد\n"
        f"• /filters — فیلتر بودجه و مهارت\n"
        f"• /profile — ویرایش پروفایل\n\n"
        f"<i>نکته حرفه‌ای: برای جلوگیری از بن شدن در سایت، فاصله ایمن بین پیشنهادها را ربات خودکار رعایت می‌کند.</i>"
    )

def welcome_back_text(name: str | None, plan_title: str | None = None) -> str:
    n = f" {esc(name)}" if name else ""
    p = f" — پلن <b>{esc(plan_title)}</b>" if plan_title else ""
    return f"👋 خوش برگشتی{n}!{p}\n{FOOTER}\n\nمنوی اصلی را باز کنید:"
