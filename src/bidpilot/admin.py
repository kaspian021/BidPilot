"""Admin Panel — exclusive to owner 7806301378, embedded in /status.

Every capability of the bot is controllable here: revenue logic, plans,
users, payments, referrals, system tuning, reports. Professional, no
clutter — each card shows current value + inline buttons to edit it.

All edits write to SettingsStore or DB so they survive restarts.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .settings_store import SettingsStore

BRAND = "BidPilot"
ADMIN_IDS = {"7806301378"}

# Extra admins can be added via settings: admin.extra_ids = ["..."]
def is_admin(chat_id: str | int, store: SettingsStore | None = None) -> bool:
    cid = str(chat_id).strip()
    if cid in ADMIN_IDS:
        return True
    if store is not None:
        try:
            extra = store.get_list("admin.extra_ids")
            if cid in [str(x) for x in extra]:
                return True
            # also owner alias list
            owner = str(store.get("admin.owner_id") or "")
            if owner and cid == owner:
                return True
        except Exception:
            pass
    return False

def esc(s: Any) -> str:
    return html.escape(str(s or ""))

def hr() -> str:
    return "───────────────"

def h2(title: str, subtitle: str = "") -> str:
    sub = f" — <i>{esc(subtitle)}</i>" if subtitle else ""
    return f"<b>{esc(title)}</b>{sub}\n{hr()}\n"

# ── main text + keyboard ───────────────────────────────────────────
def admin_main_text(stats: dict[str, Any] | None = None) -> str:
    stats = stats or {}
    lines = [
        h2("🔐 پنل ادمین", "فقط برای شما — 7806301378"),
        f"خوش آمدید قربان 👋 — تمام منطق درآمدی و مدیریتی ربات اینجاست.\n",
        f"<b>خلاصه لحظه‌ای</b>",
        f"• کاربران کل: <b>{stats.get('users_total','—')}</b> · فعال ۷روز: <b>{stats.get('users_active_7d','—')}</b>",
        f"• اشتراک فعال: <b>{stats.get('subs_active','—')}</b> · درآمد تاییدشده: <b>{stats.get('revenue_total','—')}</b>",
        f"• پرداخت موفق: <b>{stats.get('payments_ok','—')}</b> · دعوت موفق: <b>{stats.get('referrals_ok','—')}</b>",
        f"• پیشنهاد ارسالی (submitted): <b>{stats.get('bids_submitted','—')}</b>",
        f"{hr()}",
        f"<b>چه چیزی را می‌خواهید تغییر دهید؟</b>\n"
        f"هر بخش مقدار فعلی را نشان می‌دهد و با دکمه‌هایش می‌توانید همان‌جا ویرایش کنید.\n"
        f"<i>تغییرات بلافاصله ذخیره و در همه‌جا اعمال می‌شود.</i>",
    ]
    return "\n".join(lines)

def admin_main_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [{"text": "👥 مدیریت کاربران", "callback_data": "admin:users"} , {"text": "💳 پلن‌ها و قیمت", "callback_data": "admin:plans"}],
            [{"text": "💰 منطق درآمدی", "callback_data": "admin:revenue"}, {"text": "💳 درگاه‌ها", "callback_data": "admin:gateways"}],
            [{"text": "🎁 دعوت و پاداش", "callback_data": "admin:referrals"}, {"text": "📈 گزارشات", "callback_data": "admin:reports"}],
            [{"text": "⚙️ سیستم و فیلترها", "callback_data": "admin:system"}, {"text": "🔧 پیشرفته", "callback_data": "admin:advanced"}],
            [{"text": "📢 پیام همگانی", "callback_data": "admin:broadcast"}, {"text": "↩️ بازگشت به داشبورد", "callback_data": "cmd:status"}],
        ]
    }

# ── users ──────────────────────────────────────────────────────────
def users_text(rows: list[dict], total: int, page: int = 1) -> str:
    lines = [h2("👥 کاربران", f"{total} نفر — صفحه {page}")]
    if not rows:
        lines.append("<i>هنوز کاربری ثبت نشده.</i>")
    else:
        for r in rows:
            plan = r.get("plan_id","free")
            exp = r.get("expires","—")
            status = "🟢" if r.get("is_active") else "🔴"
            lines.append(
                f"{status} <code>{esc(r['chat_id'])}</code> · @{esc(r.get('username') or '—')} — <b>{esc(plan)}</b> · انقضا: {esc(exp)}\n"
                f"  └ دعوت‌کد: <code>{esc(r.get('referral_code') or '—')}</code> · ارجاع‌شده توسط: {esc(r.get('referred_by') or '—')}"
            )
    lines.append(f"{hr()}\n<i>برای مدیریت هر کاربر، روی شناسه‌اش بزنید.</i>")
    return "\n".join(lines)

def users_keyboard(rows: list[dict], page: int = 1, has_more: bool = False) -> dict:
    kb = []
    for r in rows:
        kb.append([{"text": f"👤 {r['chat_id']} · {r.get('plan_id','free')}", "callback_data": f"admin:user:{r['chat_id']}"}])
    nav = []
    if page > 1:
        nav.append({"text": "◀️ قبلی", "callback_data": f"admin:users:{page-1}"})
    if has_more:
        nav.append({"text": "▶️ بعدی", "callback_data": f"admin:users:{page+1}"})
    if nav:
        kb.append(nav)
    kb.append([{"text": "🔍 جستجو کاربر", "callback_data": "admin:users:search"}])
    kb.append([{"text": "↩️ پنل ادمین", "callback_data": "admin:main"}])
    return {"inline_keyboard": kb}

def user_detail_text(u: dict) -> str:
    lines = [
        h2(f"👤 کاربر {u['chat_id']}", u.get('username') or ""),
        f"نام: <b>{esc(u.get('first_name') or '—')}</b> · یوزرنیم: @{esc(u.get('username') or '—')}",
        f"پلن: <b>{esc(u.get('plan_id'))}</b> · فعال: {'بله ✅' if u.get('is_active') else 'خیر ❌'}",
        f"شروع: {esc(u.get('started') or '—')} · انقضا: <b>{esc(u.get('expires') or '—')}</b>",
        f"کد دعوت: <code>{esc(u.get('referral_code') or '—')}</code> · معرف: {esc(u.get('referred_by') or '—')}",
        f"ارسال موفق: {u.get('total_bids',0)} · پیشنهادها: {u.get('successful_bids',0)}",
        f"تاریخ عضویت: {esc(u.get('created') or '—')}",
        hr(),
        "<b>اقدام سریع:</b>",
    ]
    return "\n".join(lines)

def user_detail_keyboard(chat_id: str) -> dict:
    return {
        "inline_keyboard": [
            [{"text": "💳 تغییر پلن", "callback_data": f"admin:user:plan:{chat_id}"},
             {"text": "⏰ تمدید", "callback_data": f"admin:user:extend:{chat_id}"}],
            [{"text": "🚫 مسدود/رفع", "callback_data": f"admin:user:toggle:{chat_id}"},
             {"text": "🗑️ حذف کاربر", "callback_data": f"admin:user:delete:{chat_id}"}],
            [{"text": "🎁 هدیه روز اضافه کن", "callback_data": f"admin:user:bonus:{chat_id}"}],
            [{"text": "↩️ لیست کاربران", "callback_data": "admin:users"}],
        ]
    }

# ── plans ──────────────────────────────────────────────────────────
def plans_admin_text(plans: list[dict], overrides: dict) -> str:
    lines = [h2("💳 پلن‌ها و قیمت‌گذاری", "ویرایش زنده — بدون دیپلوی")]
    for p in plans:
        ov = overrides.get(p["id"], {})
        price = ov.get("price_toman", p["price_toman"])
        price_s = f"{price/1_000_000:g}M" if price >= 1_000_000 else f"{price:,}".replace(",", "٬")
        lines.append(
            f"{p['badge']} <b>{esc(p['title'])}</b> — {esc(p['tagline'])}\n"
            f"  قیمت: <b>{price_s} تومان</b> / {p['period_days']} روز · "
            f"پلن فعلی: <code>{esc(ov.get('price_toman') and 'override' or 'default')}</code>\n"
            f"  سقف: {p['limits']['max_bids_per_hour']}/ساعت · {p['limits']['max_bids_per_day']}/روز · {p['limits']['max_bids_per_month']}/ماه\n"
            f"  ویژگی‌ها: {esc(' · '.join(p['features'][:2]))}"
        )
    lines.append(f"{hr()}\n<i>روی هر پلن بزنید تا قیمت، سقف و ویژگی‌ها را ویرایش کنید.</i>")
    return "\n".join(lines)

def plans_admin_keyboard(plans: list[dict]) -> dict:
    kb = []
    for p in plans:
        kb.append([{"text": f"{p['badge']} {p['title']} — ویرایش", "callback_data": f"admin:plan:{p['id']}"}])
    kb.append([{"text": "➕ پلن جدید (سفارشی)", "callback_data": "admin:plan:new"}])
    kb.append([{"text": "↩️ پنل ادمین", "callback_data": "admin:main"}])
    return {"inline_keyboard": kb}

def single_plan_text(plan: dict, overrides: dict) -> str:
    ov = overrides.get(plan["id"], {})
    lines = [h2(f"✏️ ویرایش پلن {plan['title']}", "تغییرات آنی")]
    lines.append(f"قیمت فعلی: <b>{ov.get('price_toman', plan['price_toman']):,} تومان</b> (پیش‌فرض {plan['price_toman']:,})")
    lines.append(f"دوره: {ov.get('period_days', plan['period_days'])} روز")
    lim = overrides.get(f"{plan['id']}_limits", plan["limits"])
    if isinstance(lim, dict):
        lines.append(f"سقف ساعت: {lim.get('max_bids_per_hour')} · روز: {lim.get('max_bids_per_day')} · ماه: {lim.get('max_bids_per_month')}")
        lines.append(f"فاصله ایمن: {lim.get('min_seconds_between_bids')} ثانیه · سایت فعال: {lim.get('max_active_sites')}")
    lines.append(f"ویژگی‌ها:\n" + "\n".join(f"• {esc(f)}" for f in plan["features"]))
    lines.append(f"{hr()}\n<i>با دستور زیر مقدار را عوض کنید — مثال: /admin_set price starter 350000</i>")
    return "\n".join(lines)

def single_plan_keyboard(plan_id: str) -> dict:
    return {
        "inline_keyboard": [
            [{"text": "💰 تغییر قیمت", "callback_data": f"admin:plan:price:{plan_id}"},
             {"text": "⏱ تغییر دوره", "callback_data": f"admin:plan:period:{plan_id}"}],
            [{"text": "📊 تغییر سقف‌ها", "callback_data": f"admin:plan:limits:{plan_id}"},
             {"text": "⭐ پیشنهاد ویژه", "callback_data": f"admin:plan:recommend:{plan_id}"}],
            [{"text": "↩️ لیست پلن‌ها", "callback_data": "admin:plans"}],
        ]
    }

# ── revenue logic ──────────────────────────────────────────────────
def revenue_text(store: SettingsStore) -> str:
    ratio = store.get("pricing.ratio")
    bid_pct = store.get("pricing.bid_percent")
    strat = store.get("pricing.strategy") or "midpoint_round"
    min_step = store.get("pricing.min_step") or 5000
    default_amt = store.get("pricing.default_amount") or 2000000
    delivery = store.get("pricing.delivery_days") or 14
    # admin overrides
    commission = store.get("admin.revenue.commission_percent")
    fee = store.get("admin.revenue.platform_fee_toman")
    tax = store.get("admin.revenue.vat_percent")
    promo = store.get("admin.promo.active_code")
    lines = [
        h2("💰 منطق درآمدی", "قیمت‌گذاری + کمیسیون + تخفیف"),
        f"<b>استراتژی قیمت پیشنهاد:</b> <code>{esc(strat)}</code>",
        f"نسبت داخل بازه: <b>{esc(ratio) if ratio is not None else '0.5 (پیش‌فرض)'}</b> · درصد تخفیف بودجه ثابت: <b>{esc(bid_pct) if bid_pct else '۵۰٪'}</b>",
        f"گام رند: {esc(min_step)} · مبلغ پیش‌فرض بدون بودجه: {esc(default_amt):,} تومان · تحویل: {esc(delivery)} روز",
        hr(),
        f"<b>کمیسیون پلتفرم (ادمین):</b>",
        f"• درصد کمیسیون: {esc(commission) if commission is not None else '۰٪ (غیرفعال)'}",
        f"• کارمزد ثابت: {esc(fee) if fee is not None else '—'} تومان",
        f"• مالیات/ارزش افزوده: {esc(tax) if tax is not None else '—'}٪",
        f"• کد تخفیف فعال: {esc(promo) if promo else '—'}",
        hr(),
        "<i>مثال: اگر ratio=0.5 و کارفرما ۵M ثابت بگوید → پیشنهاد ۲.۵M. اگر کمیسیون ۱۰٪ باشد، سهم شما از هر پرداخت ۱۰٪ است.</i>",
    ]
    return "\n".join(lines)

def revenue_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [{"text": "📈 تغییر Ratio / درصد", "callback_data": "admin:rev:ratio"},
             {"text": "🧩 تغییر استراتژی", "callback_data": "admin:rev:strategy"}],
            [{"text": "💵 کمیسیون ٪", "callback_data": "admin:rev:commission"},
             {"text": "🏷️ کد تخفیف", "callback_data": "admin:rev:promo"}],
            [{"text": "↩️ پنل ادمین", "callback_data": "admin:main"}],
        ]
    }

# ── gateways ───────────────────────────────────────────────────────
def gateways_text(store: SettingsStore) -> str:
    lines = [h2("💳 درگاه‌های پرداخت", "۴ درگاه شاپرکی — احراز سبک")]
    for gid, label in [("zibal","زیبال"),("idpay","آی‌دی‌پی"),("nextpay","نکست‌پی"),("zarinpal","زرین‌پال")]:
        mid = store.get(f"payments.{gid}.merchant_id") or store.get(f"zarinpal.merchant_id") or store.get("payments.merchant_id") or ""
        sandbox = store.get(f"payments.{gid}.sandbox")
        masked = (mid[:4]+"…"+mid[-4:]) if mid and len(mid)>8 else ("تنظیم نشده" if not mid else "••••")
        lines.append(f"<b>{esc(label)}</b> ({gid}) — مرچنت: <code>{esc(masked)}</code> · سندباکس: {esc('بله' if str(sandbox).lower() in ('1','true','yes') else 'خیر')}")
    lines.append(f"{hr()}\n<i>بدون مرچنت، پرداخت شبیه‌سازی می‌شود — برای تست عالی‌ست.</i>")
    return "\n".join(lines)

def gateways_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [{"text": "🔑 تنظیم مرچنت زیبال", "callback_data": "admin:gw:zibal"},
             {"text": "🔑 آی‌دی‌پی", "callback_data": "admin:gw:idpay"}],
            [{"text": "🔑 نکست‌پی", "callback_data": "admin:gw:nextpay"},
             {"text": "🔑 زرین‌پال", "callback_data": "admin:gw:zarinpal"}],
            [{"text": "↩️ پنل ادمین", "callback_data": "admin:main"}],
        ]
    }

# ── referrals ──────────────────────────────────────────────────────
def referrals_text(store: SettingsStore, stats: dict | None = None) -> str:
    stats = stats or {}
    lines = [h2("🎁 دعوت و پاداش", "میل‌استون + پورسانت خرید")]
    # current milestones from store or defaults
    ms_json = store.get("admin.referrals.milestones")
    if ms_json:
        try:
            ms = json.loads(str(ms_json)) if isinstance(ms_json, str) else ms_json
        except Exception:
            ms = None
    else:
        ms = None
    if not ms:
        ms = [{"count":3,"days":7,"plan":"starter"},{"count":5,"days":15,"plan":"starter"},{"count":10,"days":30,"plan":"pro"},{"count":20,"days":30,"plan":"elite"}]
    for m in ms:
        lines.append(f"• {m['count']} دعوت → <b>{m['days']} روز {esc(m['plan'])}</b>")
    purchase_bonus = store.get("admin.referrals.purchase_bonus_days") or 7
    lines.append(f"پورسانت خرید معرف: <b>{esc(purchase_bonus)} روز</b> از همان پلن (هر خرید)")
    lines.append(f"{hr()}")
    lines.append(f"کل دعوت ثبت‌شده: {stats.get('total', '—')} · تاییدشده: {stats.get('qualified','—')} · خریدکرده: {stats.get('purchased','—')}")
    lines.append(f"<i>لینک نمونه: https://t.me/BidPilotIranbot?start=ref_XXXX</i>")
    return "\n".join(lines)

def referrals_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [{"text": "✏️ تغییر میل‌استون‌ها", "callback_data": "admin:ref:milestones"},
             {"text": "💎 پاداش خرید", "callback_data": "admin:ref:bonus"}],
            [{"text": "📋 لیست دعوت‌ها", "callback_data": "admin:ref:list"}],
            [{"text": "↩️ پنل ادمین", "callback_data": "admin:main"}],
        ]
    }

# ── reports ────────────────────────────────────────────────────────
def reports_text(stats: dict) -> str:
    lines = [h2("📈 گزارشات و آنالیتیکس", "درآمد + تبدیل + ریزش")]
    for k,label in [
        ("revenue_today","درآمد امروز"),
        ("revenue_7d","۷ روز"),
        ("revenue_30d","۳۰ روز"),
        ("revenue_total","کل درآمد تاییدشده"),
        ("conversion_rate","نرخ تبدیل بازدید→خرید"),
        ("churn_30d","ریزش ۳۰روزه"),
        ("avg_bids_per_user","میانگین پیشنهاد/کاربر"),
    ]:
        v = stats.get(k, "—")
        lines.append(f"• {esc(label)}: <b>{esc(v)}</b>")
    lines.append(hr())
    lines.append("<i>برای جزئیات CSV، «دانلود گزارش» را بزنید.</i>")
    return "\n".join(lines)

def reports_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [{"text": "📥 دانلود CSV کاربران", "callback_data": "admin:reports:csv:users"},
             {"text": "📥 CSV پرداخت‌ها", "callback_data": "admin:reports:csv:payments"}],
            [{"text": "📊 نمودار ۷ روزه", "callback_data": "admin:reports:chart"}],
            [{"text": "↩️ پنل ادمین", "callback_data": "admin:main"}],
        ]
    }

# ── system ─────────────────────────────────────────────────────────
def system_text(store: SettingsStore) -> str:
    lines = [h2("⚙️ سیستم و فیلترها", "کنترل ریز به ریز")]
    lines.append(f"پولینگ: {esc(store.get('runtime.poll_interval_seconds') or 120)} ثانیه · جیتر: {esc(store.get('runtime.jitter_seconds') or 30)}")
    lines.append(f"حالت ارسال: <b>{esc(store.get('runtime.submit_mode') or 'dry_run')}</b> · حد ساعت/روز: {esc(store.get('runtime.max_bids_per_hour') or 5)}/{esc(store.get('runtime.max_bids_per_day') or 20)}")
    lines.append(f"LLM: {esc(store.get('llm.provider') or 'groq')} · مدل: {esc(store.get('llm.model') or '—')} · پروکسی: {esc(store.get('network.proxy') or 'خاموش')}")
    lines.append(f"فیلتر بودجه اجباری: {esc(store.get('filters.require_budget'))} · تطابق مهارت: {esc(store.get('filters.require_skill_match'))}")
    lines.append(f"سایت فعال: {esc(store.get('marketplace.name') or 'parscoders')} · نگهداری: {esc('روشن' if store.get_bool('admin.maintenance') else 'خاموش')}")
    lines.append(f"{hr()}\n<i>هر مورد با دکمه‌اش قابل تغییر است — شامل فواصل، کلمات ممنوعه، سایت‌ها و حتی پرامپت پیشنهاد.</i>")
    return "\n".join(lines)

def system_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [{"text": "⏱ پولینگ / جیتر", "callback_data": "admin:sys:poll"},
             {"text": "🔄 حالت ارسال", "callback_data": "admin:sys:mode"}],
            [{"text": "🤖 LLM و مدل", "callback_data": "admin:sys:llm"},
             {"text": "🌐 پروکسی", "callback_data": "admin:sys:proxy"}],
            [{"text": "🧹 فیلترها", "callback_data": "admin:sys:filters"},
             {"text": "🌍 سایت فعال", "callback_data": "admin:sys:site"}],
            [{"text": "🚧 حالت نگهداری", "callback_data": "admin:sys:maintenance"}],
            [{"text": "↩️ پنل ادمین", "callback_data": "admin:main"}],
        ]
    }

# ── advanced ───────────────────────────────────────────────────────
def advanced_text(store: SettingsStore) -> str:
    lines = [h2("🔧 پیشرفته", " broadcast + دیباگ + نگهداری")]
    maint = store.get_bool("admin.maintenance")
    lines.append(f"نگهداری: <b>{'روشن — ربات برای کاربران عادی پاسخ نمی‌دهد' if maint else 'خاموش'}</b>")
    lines.append(f"نسخه: {esc(store.get('app.version') or '0.9.x')} · دیباگ لاگ: {esc('فعال' if store.get_bool('admin.debug') else 'خاموش')}")
    lines.append(f"ادمین‌های اضافه: {esc(', '.join(store.get_list('admin.extra_ids')) or '—')}")
    lines.append(f"بان لیست: {len(store.get_list('admin.banned_ids'))} نفر")
    lines.append(hr())
    lines.append("<i>این بخش فقط برای شماست — هر تغییری لاگ می‌شود.</i>")
    return "\n".join(lines)

def advanced_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [{"text": "📢 پیام همگانی", "callback_data": "admin:broadcast"},
             {"text": "🚧 نگهداری روشن/خاموش", "callback_data": "admin:sys:maintenance"}],
            [{"text": "📜 مشاهده لاگ", "callback_data": "admin:adv:logs"},
             {"text": "🗑️ پاکسازی دیتابیس قدیمی", "callback_data": "admin:adv:cleanup"}],
            [{"text": "👥 افزودن ادمین دوم", "callback_data": "admin:adv:addadmin"}],
            [{"text": "↩️ پنل ادمین", "callback_data": "admin:main"}],
        ]
    }
