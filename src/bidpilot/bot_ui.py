"""BidPilot Telegram UI — premium, clean, compatible.

Why SVG isn't used here:
  Telegram message text (HTML/Markdown) does NOT render SVG. It only renders
  Unicode emoji as high-quality images (Retina). SVG can only be used as a
  *photo/file* (e.g. bot profile picture via @BotFather, or a banner sent as
  photo with caption). Using rare geometric chars (◆ ◇ ▓) looks broken on many
  clients — screenshot above proves it. So we keep message text to standard,
  high-quality colorful emoji only, and use banners as PNG photos when needed.

This file = only safe, universally-rendered emoji + clean Persian typography.
"""

from __future__ import annotations

import html
from datetime import datetime

from .subscriptions.plans import Plan, all_plans, get_plan

BRAND = "BidPilot"
TAGLINE = "دستیار هوشمند پیشنهاددهی فریلنسرها"
FOOTER = f"✦ {BRAND}  ·  {TAGLINE}"
SUPPORT = "پشتیبانی: @BidPilotSupport"

# Only high-quality, colorful emoji that Telegram renders as Retina images.
# No monochrome text symbols (◆ ◇ ▪ ▓ ━) — they look low-res/broken.
ICON = {
    "rocket": "🚀",
    "star": "⭐",
    "diamond": "💎",
    "crown": "👑",
    "spark": "✨",
    "check": "✅",
    "cross": "❌",
    "warn": "⚠️",
    "chart": "📊",
    "shield": "🛡️",
    "link": "🔗",
    "gift": "🎁",
    "fire": "🔥",
    "gem": "💠",
}

def esc(s: str) -> str:
    return html.escape(s or "")

def hr() -> str:
    return "───────────────"

def progress_bar(done: int, total: int, width: int = 10) -> str:
    if total <= 0:
        return "─" * width + "  ۰٪"
    ratio = max(0.0, min(1.0, done / total))
    filled = int(round(ratio * width))
    # Use simple, safe block chars — Telegram renders them well
    bar = "■" * filled + "□" * (width - filled)
    pct = int(ratio * 100)
    return f"{bar}  {pct}٪"

def badge_for_plan(plan: Plan) -> str:
    # High-quality colorful badges — consistent palette
    mp = {
        "free": "🌿",
        "starter": "⭐",
        "pro": "💎",
        "elite": "👑",
    }
    return mp.get(plan.id, "⭐") if not plan.badge else plan.badge

def price_per_day_fa(plan: Plan) -> str:
    return plan.price_per_day.replace("تومان/روز", "هزار تومان / روز")

def svg_note() -> str:
    return (
        "تلگرام داخل متن پیام SVG نشان نمی‌دهد — فقط ایموجی‌های استاندارد.\n"
        "برای هدر گرافیکی، از بنر PNG (ساخته‌شده از SVG) به صورت عکس با کپشن استفاده می‌کنیم."
    )

# ── main menu ──────────────────────────────────────────────────────
def main_menu_text(first_name: str | None = None, plan_title: str | None = None) -> str:
    name = esc(first_name) if first_name else "همراه عزیز"
    plan_line = f"پلن فعال شما: <b>{esc(plan_title)}</b>" if plan_title else "حساب شما آماده است — از همین‌جا شروع کنید"
    return (
        f"<b>{BRAND}</b> — <i>{esc(TAGLINE)}</i>\n"
        f"{hr()}\n"
        f"سلام {name} 👋\n\n"
        f"هر روز صدها پروژه جدید روی پارس‌کدرز، پونیشا و کارلنسر منتشر می‌شود.\n"
        f"<b>{BRAND}</b> آن‌ها را می‌خواند، مرتبط‌ترین‌ها را انتخاب می‌کند و "
        f"پیشنهادی تمیز و انسانی می‌نویسد — شما فقط تایید می‌کنید.\n\n"
        f"<i>{plan_line}</i>\n"
        f"{hr()}\n"
        f"<b>از کجا شروع کنم؟</b>\n"
        f"• <b>شروع فعالیت</b> — رصد زنده و ارسال خودکار\n"
        f"• <b>پروفایل</b> — معرفی یک‌باره، استفاده همیشگی\n"
        f"• <b>پلن‌ها</b> — وقتی آماده رشد بودید\n"
        f"\n<i>بدون .env، بدون پیچیدگی — همه‌چیز امن در همین تلگرام.</i>"
    )

def main_menu_keyboard(is_live: bool = False) -> dict:
    live_label = "⏸ توقف رصد" if is_live else "⏸ توقف"
    return {
        "inline_keyboard": [
            [{"text": "🚀 شروع فعالیت", "callback_data": "run:ask"}],
            [
                {"text": "🔍 پیش‌نمایش پیشنهاد", "callback_data": "cmd:preview"},
                {"text": "📊 داشبورد", "callback_data": "cmd:status"},
            ],
            [
                {"text": "👤 پروفایل", "callback_data": "cmd:profile"},
                {"text": "✨ لحن و قیمت", "callback_data": "cmd:style"},
            ],
            [
                {"text": "🌐 سایت‌ها", "callback_data": "cmd:sites"},
                {"text": "🎯 فیلترها", "callback_data": "cmd:filters"},
            ],
            [{"text": "💠 پلن‌ها و اشتراک", "callback_data": "cmd:plans"}],
            [{"text": "🎁 دعوت دوستان — ارتقای رایگان", "callback_data": "cmd:invite"}],
            [
                {"text": "❓ راهنما", "callback_data": "cmd:help"},
                {"text": live_label, "callback_data": "run:stop"},
            ],
        ]
    }

def status_card(status_text: str, plan: Plan | None = None, usage: dict | None = None) -> str:
    header = f"<b>📊 داشبورد</b> — <i>{BRAND}</i>\n{hr()}\n"
    plan_box = ""
    if plan:
        plan_box = (
            f"{badge_for_plan(plan)} <b>{esc(plan.title)}</b> — <i>{esc(plan.tagline)}</i>\n"
            f"  {esc(plan.formatted_price)} · <i>{esc(price_per_day_fa(plan))}</i>\n"
        )
        if usage:
            ph, pd = usage.get("per_hour", 0), usage.get("per_day", 0)
            lh, ld = plan.limits.max_bids_per_hour, plan.limits.max_bids_per_day
            plan_box += (
                f"  ساعتی  {progress_bar(ph, lh)}  {ph} / {lh}\n"
                f"  روزانه  {progress_bar(pd, ld)}  {pd} / {ld}\n"
            )
        plan_box += f"{hr()}\n"
    return header + plan_box + status_text + f"\n\n<i>{FOOTER}</i>"

def profile_intro_text(step: str = "1/2", stage: str = "پروفایل و نمونه‌کار") -> str:
    return (
        f"<b>پروفایل شما</b> — <i>{esc(step)} · {esc(stage)}</i>\n"
        f"{hr()}\n"
        f"نیازی به تایپ رزومه نیست. یک فایل یا لینک کافی‌ست — بقیه را با هوش مصنوعی می‌خوانیم "
        f"و پیش‌نمایشتان می‌دهیم.\n\n"
        f"<b>یک گزینه را انتخاب کنید:</b>\n"
        f"{hr()}\n"
        f"<b>۱ · رزومه PDF</b> — <i>پیشنهادی</i>\n"
        f"  فایل را همین‌جا بفرستید؛ نام، سابقه، مهارت و نمونه‌کار خودکار استخراج می‌شود.\n\n"
        f"<b>۲ · لینک گیت‌هاب</b>\n"
        f"  آدرس پروفایل را بفرستید؛ مخازن عمومی خوانده می‌شود.\n\n"
        f"<b>۳ · سایت / پورتفولیو</b>\n"
        f"  یک URL بفرستید؛ محتوای صفحه استخراج می‌شود.\n\n"
        f"<b>۴ · ورود دستی</b>\n"
        f"  نام، سابقه و مهارت‌ها را خودتان می‌نویسید.\n\n"
        f"<b>۵ · فعلاً رد شود</b>\n"
        f"  آنچه اکنون در پروفایل دارید حفظ می‌شود.\n"
        f"{hr()}\n"
        f"<i>حریم خصوصی: فایل شما فقط برای ساختن پروفایل خوانده می‌شود.</i>"
    )

def plans_overview_text() -> str:
    lines = [
        f"<b>پلن‌ها</b> — <i>هر کاربر، حساب و سقف مستقل</i>",
        f"<i>{esc(TAGLINE)}</i>",
        hr(),
        "",
        "<i>روی هر پلن بزنید تا جزئیات و پرداخت امن را ببینید. پرداخت بدون احراز سنگین — ۴ درگاه شاپرکی.</i>",
        "",
    ]
    for plan in all_plans():
        rec = "  <b>· پیشنهاد ما</b>" if plan.recommended else ""
        count_label = f"{plan.limits.max_bids_per_hour} / ساعت · {plan.limits.max_bids_per_day} / روز"
        if plan.limits.max_bids_per_month < 999999:
            count_label += f" · {plan.limits.max_bids_per_month} / ماه"
        else:
            count_label += " · ماهانه نامحدود"
        lines.append(
            f"{badge_for_plan(plan)} <b>{esc(plan.title)}</b>{rec} — <i>{esc(plan.tagline)}</i>\n"
            f"  <b>{esc(plan.formatted_price)}</b> · <i>{esc(price_per_day_fa(plan))}</i>\n"
            f"  <i>{count_label}</i>\n"
        )
        feats = " · ".join(esc(f) for f in plan.features[:3])
        lines.append(f"  {feats}\n")
    lines.append(f"{hr()}")
    lines.append("پلن رایگان برای آشنایی عالی‌ست؛ وقتی حجم گرفت، ارتقا یک‌لمسه است.\n" f"<i>{FOOTER}</i>")
    return "\n".join(lines)

def plans_keyboard() -> dict:
    rows = []
    for plan in all_plans():
        label = f"{badge_for_plan(plan)} {plan.title} — {plan.formatted_price}"
        if plan.recommended:
            label += " · پیشنهادی"
        rows.append([{"text": label, "callback_data": f"plan:{plan.id}"}])
    rows.append([{"text": "📋 مقایسه دقیق پلن‌ها", "callback_data": "plans:compare"}])
    rows.append([{"text": "🧾 پرداخت‌های من", "callback_data": "payments:history"}])
    rows.append([{"text": "↩️ بازگشت به منو", "callback_data": "cmd:home"}])
    return {"inline_keyboard": rows}

def plan_detail_text(plan: Plan) -> str:
    header = (
        f"{badge_for_plan(plan)} <b>{esc(plan.title)}</b> — <i>{esc(plan.tagline)}</i>\n"
        f"<b>{esc(plan.formatted_price)}</b> <i>· {plan.period_days} روزه · {esc(price_per_day_fa(plan))}</i>\n"
    )
    site_label = "نامحدود" if plan.limits.max_active_sites > 3 else str(plan.limits.max_active_sites)
    limits = (
        f"{hr()}\n"
        f"<b>ظرفیت</b>\n"
        f"• {plan.limits.max_bids_per_hour} پیشنهاد در ساعت\n"
        f"• {plan.limits.max_bids_per_day} در روز\n"
        f"• {plan.limits.max_bids_per_month if plan.limits.max_bids_per_month < 999999 else 'نامحدود'} در ماه\n"
        f"• {site_label} سایت فعال هم‌زمان\n"
        f"• فاصله ایمن {plan.limits.min_seconds_between_bids} ثانیه\n"
    )
    feats = f"{hr()}\n<b>آنچه می‌گیرید</b>\n" + "\n".join(f"• {esc(f)}" for f in plan.features)
    note = ""
    if plan.is_free():
        note = (
            f"\n{hr()}\n"
            f"<i>پلن رایگان شما همین حالا فعال است. برای ظرفیت بالاتر، یکی از پلن‌های بالا را انتخاب کنید "
            f"یا از دعوت دوستان به‌صورت رایگان ارتقا بگیرید.</i>"
        )
    return header + limits + "\n" + feats + note + f"\n\n<i>{FOOTER}</i>"

def plan_detail_keyboard(plan: Plan) -> dict:
    if plan.is_free():
        return {
            "inline_keyboard": [
                [{"text": "✅ فعال است · رایگان", "callback_data": "cmd:plans"}],
                [{"text": "🎁 دعوت دوستان → ارتقای رایگان", "callback_data": "cmd:invite"}],
                [{"text": "↩️ بازگشت", "callback_data": "cmd:plans"}],
            ]
        }
    return {
        "inline_keyboard": [
            [{"text": f"💳 پرداخت {plan.formatted_price} — انتخاب درگاه امن", "callback_data": f"pay_pick:{plan.id}"}],
            [{"text": "🎁 یا با دعوت دوستان رایگان بگیرید", "callback_data": "cmd:invite"}],
            [{"text": "📋 مقایسه پلن‌ها", "callback_data": "plans:compare"}],
            [{"text": "↩️ بازگشت", "callback_data": "cmd:plans"}],
        ]
    }

def gateway_picker_text(plan: Plan) -> str:
    from .subscriptions.gateways import GATEWAYS_META, GATEWAY_ORDER
    lines = [
        f"<b>پرداخت امن</b> — <i>{esc(plan.title)} · {esc(plan.formatted_price)}</i>",
        f"<i>{plan.period_days} روزه · {esc(price_per_day_fa(plan))}</i>",
        hr(),
        "یک درگاه شاپرکی را انتخاب کنید. هر چهار مورد تایید شاپرک دارند؛ تفاوت فقط در سرعت احراز است. "
        "اگر هنوز حساب نساخته‌اید، نگران نباشید — بدون کلید هم پرداخت به‌صورت <i>شبیه‌سازی‌شده</i> ساخته می‌شود تا جریان را تست کنید.",
        "",
    ]
    for gid in GATEWAY_ORDER:
        m = GATEWAYS_META[gid]
        level = esc(m.kyc_level)
        lines.append(f"<b>{esc(m.label_fa)}</b> — <i>{esc(m.label)}</i>")
        lines.append(f"  {esc(m.description)}")
        lines.append(f"  <i>احراز: {level} · {esc(m.website)}</i>\n")
    lines.append(f"<i>پیشنهاد ما برای شروع بی‌دردسر: <b>زیبال</b> — احراز فقط با موبایل و شبا، فعال‌سازی زیر ۲ ساعت.</i>")
    lines.append(f"{hr()}\n<i>{FOOTER} · پرداخت‌ها با رمزنگاری شاپرک محافظت می‌شوند</i>")
    return "\n".join(lines)

def gateway_picker_keyboard(plan_id: str) -> dict:
    from .subscriptions.gateways import GATEWAYS_META, GATEWAY_ORDER
    rows = []
    for gid in GATEWAY_ORDER:
        m = GATEWAYS_META[gid]
        short_kyc = m.kyc_level.split("—")[0].strip() if "—" in m.kyc_level else m.kyc_level
        rows.append([{"text": f"{m.label_fa} · {short_kyc}", "callback_data": f"buy:{plan_id}:{gid}"}])
    rows.append([{"text": "↩️ بازگشت", "callback_data": f"plan:{plan_id}"}])
    return {"inline_keyboard": rows}

def compare_table_text() -> str:
    lines = [f"<b>مقایسه پلن‌ها</b> — <i>ظرفیت به زبان ساده</i>", hr(), ""]
    header = "ویژگی" + "".join(f"  |  {esc(p.title)}" for p in all_plans())
    lines.append(f"<pre>{header}</pre>")
    lines.append(hr())
    rows = [
        ("در ساعت", [str(p.limits.max_bids_per_hour) for p in all_plans()]),
        ("در روز", [str(p.limits.max_bids_per_day) for p in all_plans()]),
        ("در ماه", [str(p.limits.max_bids_per_month if p.limits.max_bids_per_month < 999999 else "∞") for p in all_plans()]),
        ("سایت فعال", [("نامحدود" if p.limits.max_active_sites > 3 else str(p.limits.max_active_sites)) for p in all_plans()]),
        ("فاصله ایمن", [f"{p.limits.min_seconds_between_bids} ثانیه" for p in all_plans()]),
    ]
    for label, vals in rows:
        line = f"{label:<10}" + "".join(f"  |  {v:>8}" for v in vals)
        lines.append(f"<pre>{line}</pre>")
    lines.append(f"\n<i>برای خرید، روی نام هر پلن بزنید — پرداخت با درگاه امن شاپرک.</i>\n<i>{FOOTER}</i>")
    return "\n".join(lines)

def payment_created_text(plan: Plan, payment_url: str, authority: str, gateway: str = "zarinpal") -> str:
    from .subscriptions.gateways import GATEWAYS_META
    g = GATEWAYS_META.get(gateway, GATEWAYS_META["zarinpal"])
    return (
        f"<b>پرداخت · {esc(plan.title)}</b> — <i>{esc(plan.formatted_price)}</i>\n"
        f"{hr()}\n"
        f"لینک پرداخت امن شما با <b>{esc(g.label_fa)}</b> ساخته شد.\n"
        f"پرداخت را تکمیل کنید و سپس <b>بررسی پرداخت</b> را بزنید تا پلن فوراً فعال شود.\n\n"
        f"🔗 <a href=\"{payment_url}\">{esc(payment_url)}</a>\n"
        f"کد پیگیری: <code>{esc(authority)}</code>\n"
        f"درگاه: {esc(g.label_fa)} · {esc(g.kyc_level)}\n\n"
        f"<i>هنوز حساب درگاه ندارید؟ این لینک شبیه‌سازی‌شده است تا پیش از فعال‌سازی، تجربه کامل را ببینید.</i>"
    )

def payment_keyboard(authority: str, payment_url: str, gateway: str = "zarinpal") -> dict:
    from .subscriptions.gateways import GATEWAYS_META
    g = GATEWAYS_META.get(gateway, GATEWAYS_META["zarinpal"])
    return {
        "inline_keyboard": [
            [{"text": f"🔗 رفتن به درگاه {g.label_fa}", "url": payment_url}],
            [{"text": "✅ بررسی پرداخت", "callback_data": f"verify:{authority}"}],
            [{"text": "❓ راهنمای درگاه", "callback_data": "zarinpal:guide"}],
            [{"text": "↩️ بازگشت به پلن‌ها", "callback_data": "cmd:plans"}],
        ]
    }

def payment_verified_text(plan: Plan, ref_id: str, expires: datetime | None) -> str:
    exp_line = f"\nاعتبار تا · <b>{expires.strftime('%Y/%m/%d %H:%M') if expires else '—'}</b>" if expires else ""
    return (
        f"<b>پرداخت تایید شد ✨</b>\n"
        f"{hr()}\n"
        f"{badge_for_plan(plan)} پلن <b>{esc(plan.title)}</b> فعال شد.\n"
        f"کد مرجع شاپرک: <code>{esc(ref_id)}</code>{exp_line}\n\n"
        f"از این لحظه ظرفیت شما افزایش یافت:\n"
        f"• {plan.limits.max_bids_per_hour} پیشنهاد در ساعت\n"
        f"• {plan.limits.max_bids_per_day} در روز\n\n"
        f"<i>سپاس از اعتمادتان — {FOOTER}</i>"
    )

def referral_card(
    code: str,
    link: str,
    bot_username: str,
    total_qualified: int,
    pending: int,
    next_milestone: dict | None,
    recent: list,
    milestones: list[dict] | None = None,
) -> str:
    from .subscriptions.referrals import MILESTONES as _MS
    ms = milestones or _MS
    lines = [
        f"<b>🎁 باشگاه دعوت</b> — <i>دوستانتان را بیاورید، پلن هدیه بگیرید</i>",
        hr(),
        f"هر دوستی که با لینک شما بیاید و اتصال به پارس‌کدرز را کامل کند، برای شما <b>روز هدیه</b> می‌سازد.\n"
        f"بدون خرید، بدون قرعه‌کشی — کاملاً شفاف.",
        f"{hr()}",
        f"<b>لینک اختصاصی شما</b>",
        f"<code>{esc(link)}</code>",
        f"<i>کد: {esc(code)} · @{esc(bot_username or 'BidPilotIranbot')}</i>",
        "",
        f"<b>پیشرفت شما</b>",
        f"تاییدشده: <b>{total_qualified}</b> · در انتظار تکمیل: {pending}",
    ]
    if next_milestone:
        need = next_milestone["count"] - total_qualified
        bar = progress_bar(total_qualified, next_milestone["count"])
        lines.append(f"هدف بعدی: {next_milestone['emoji']} <b>{esc(next_milestone['title'])}</b> — {need} نفر دیگر مانده")
        lines.append(f"{bar}  {total_qualified} / {next_milestone['count']}")
    else:
        if total_qualified >= (ms[-1]["count"] if ms else 20):
            lines.append(f"تمام سطوح تکمیل شد ✨ هر خرید دوست = +۷ روز هدیه، بدون سقف.")
        else:
            lines.append(progress_bar(total_qualified, ms[-1]["count"]) + f"  {total_qualified} / {ms[-1]['count']}")
    lines.append("")
    lines.append("<b>سطوح هدیه</b>")
    for m in ms:
        done = "✅" if total_qualified >= m["count"] else "○"
        lines.append(f"{done} {m['emoji']} {m['count']} نفر → {esc(m['title'])}")
    lines.append(f"{hr()}")
    lines.append(
        f"<i>نکته: اگر دوست دعوت‌شده پلن پولی بخرد، شما <b>+۷ روز از همان پلن</b> هدیه می‌گیرید — "
        f"این پاداش جدای سطوح بالاست.</i>"
    )
    if recent:
        lines.append("")
        lines.append("<b>دعوت‌های اخیر</b>")
        for r in recent[:5]:
            st_icon = {"pending": "○", "qualified": "✅", "purchased": "💎"}.get(getattr(r, "status", ""), "•")
            uname = esc(getattr(r, "invitee_username", "") or getattr(r, "invitee_chat_id", "") or "—")
            if uname.isdigit() and len(uname) > 4:
                uname = uname[:3] + "···" + uname[-2:]
            status_fa = {"pending": "در انتظار", "qualified": "تاییدشده", "purchased": "خرید کرده"}.get(getattr(r, "status", ""), getattr(r, "status", ""))
            lines.append(f"{st_icon} {uname} — <i>{esc(status_fa)}</i>")
    lines.append(f"\n{hr()}\n<i>{FOOTER}</i>")
    return "\n".join(lines)

def referral_keyboard(link: str, code: str) -> dict:
    import urllib.parse
    share_text = urllib.parse.quote(f"من با BidPilot پیشنهادهای فریلنسری رو خودکار می‌فرستم — تو هم بیا و پلن رایگان بگیر:\n{link}")
    tg_share = f"https://t.me/share/url?url={urllib.parse.quote(link)}&text={share_text}"
    return {
        "inline_keyboard": [
            [{"text": "📤 اشتراک‌گذاری در تلگرام", "url": tg_share}],
            [{"text": "📋 کپی لینک", "callback_data": f"ref:copy:{code}"}],
            [{"text": "🔄 بروزرسانی آمار", "callback_data": "cmd:invite"}],
            [{"text": "💠 مشاهده پلن‌ها", "callback_data": "cmd:plans"}],
            [{"text": "↩️ منو", "callback_data": "cmd:home"}],
        ]
    }

def help_text() -> str:
    return (
        f"<b>{BRAND}</b> — <i>راهنمای سریع</i>\n"
        f"{hr()}\n"
        f"<b>شروع در ۶۰ ثانیه</b>\n"
        f"۱ · <code>/start</code> — منوی اصلی\n"
        f"۲ · پروفایل را کامل کنید — PDF یا لینک کافی‌ست\n"
        f"۳ · حساب پارس‌کدرز را وصل کنید — <code>/market</code>\n"
        f"۴ · پلن را انتخاب کنید — رایگان برای تست\n"
        f"۵ · <b>شروع فعالیت</b> را بزنید\n"
        f"{hr()}\n"
        f"<b>دستورات پرکاربرد</b>\n"
        f"• <code>/status</code> — داشبورد زنده و مصرف\n"
        f"• <code>/plans</code> — پلن‌ها و پرداخت (۴ درگاه شاپرک)\n"
        f"• <code>/invite</code> — لینک دعوت و هدیه\n"
        f"• <code>/preview</code> — پیش‌نمایش یک پیشنهاد واقعی\n"
        f"• <code>/run</code> / <code>/stop</code> — روشن / خاموش\n"
        f"• <code>/filters</code> — بودجه و مهارت\n"
        f"• <code>/profile</code> — ویرایش پروفایل\n"
        f"{hr()}\n"
        f"<i>فاصله ایمن بین پیشنهادها خودکار رعایت می‌شود تا حساب شما در سایت محدود نشود.</i>\n"
        f"<i>{SUPPORT}</i>"
    )

def welcome_back_text(name: str | None, plan_title: str | None = None) -> str:
    n = f" {esc(name)}" if name else ""
    p = f" · <b>{esc(plan_title)}</b>" if plan_title else ""
    return f"خوش برگشتید{n} ✨{p}\n<i>{FOOTER}</i>\n\nمنوی اصلی آماده است — از دکمه‌های زیر ادامه دهید."

def error_text(title: str, detail: str = "", hint: str = "") -> str:
    parts = [f"<b>{esc(title)}</b>", hr()]
    if detail:
        parts.append(esc(detail))
    if hint:
        parts.append(f"\n<i>{esc(hint)}</i>")
    return "\n".join(parts)
