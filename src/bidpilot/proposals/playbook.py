"""Domain playbook: turns project keywords into concrete implementation talking points.

Used by the template fallback (so even without an LLM the message is project-specific),
and injected into the LLM prompt as a hint list when available.
"""

from __future__ import annotations

from ..models import Project
from ..text import has_any

# (trigger keywords) -> (challenge sentence, implementation sentence)
PLAYBOOK: list[tuple[tuple[str, ...], tuple[str, str]]] = [
    (
        ("ربات", "تلگرام", "bot", "telegram"),
        (
            "طراحی جریان مکالمه و مدیریت state کاربر در ربات، تا تجربه ساده و بدون سردرگمی بماند",
            "معماری مبتنی بر هندلرهای ماژولار (aiogram/python-telegram-bot) با دیتابیس سبک و وبهوک امن",
        ),
    ),
    (
        ("درگاه", "پرداخت", "زرین", "payment", "فروشگاه", "سبد خرید"),
        (
            "اتصال مطمئن به درگاه پرداخت و مدیریت تراکنش‌های نیمه‌کاره و بازگشت کاربر",
            "پیاده‌سازی idempotent برای ثبت سفارش و لاگ کامل تراکنش‌ها تا مغایرت مالی پیش نیاید",
        ),
    ),
    (
        ("ووکامرس", "woocommerce", "وردپرس", "wordpress", "فروشگاه‌ساز"),
        (
            "همگام‌سازی محصولات و موجودی بین سایت و کانال‌های دیگر بدون کند شدن سایت",
            "استفاده از REST API ووکامرس همراه با cache و همگام‌سازی زمان‌بندی‌شده (webhook + cron)",
        ),
    ),
    (
        ("api", "بک‌اند", "backend", "fastapi", "django", "rest"),
        (
            "طراحی مدل داده و قرارداد API قبل از کدنویسی، تا تغییرات بعدی هزینه‌ی بازنویسی نداشته باشد",
            "FastAPI/Django REST با مستندات خودکار Swagger، اعتبارسنجی ورودی و تست‌های پایه",
        ),
    ),
    (
        ("اپلیکیشن", "موبایل", "android", "ios", "flutter", "react native"),
        (
            "هماهنگی بک‌اند و اپ در بحث احراز هویت و مدیریت نسخه‌ها",
            "طراحی API نسخه‌بندی‌شده و پشتیبانی از توکن‌های قابل تمدید برای اپ‌های موبایل",
        ),
    ),
    (
        ("سئو", "seo", "سرعت", "بهینه"),
        (
            "ساختار محتوایی و سرعت لود، دو عاملی که بیشترین اثر را روی نتیجه دارند",
            "ساختار سیلو محتوایی، اسکیما، بهینه‌سازی تصاویر و Core Web Vitals در حد امکان",
        ),
    ),
    (
        ("پنل", "داشبورد", "dashboard", "مدیریت"),
        (
            "نقش‌های دسترسی و گزارش‌گیری، بخشی که معمولاً دیرتر از نیاز واقعی طراحی می‌شود",
            "پنل مدیریت با دسترسی نقش‌محور (RBAC) و گزارش‌های قابل خروجی گرفتن",
        ),
    ),
    (
        ("اسکرپ", "scrap", "کراول", "crawl", "داده"),
        (
            "پایداری در برابر تغییر ساختار سایت مقصد و بلاک شدن",
            "لایه‌ی استخراج قابل‌تنظیم، صف و retry، و ذخیره‌سازی نسخه‌دار داده‌ها",
        ),
    ),
    (
        ("هوش مصنوعی", "ai", "chatgpt", "gpt", "مدل زبانی", "llm"),
        (
            "کنترل کیفیت خروجی مدل و هزینه‌ی هر درخواست",
            "استفاده از پرامپت ساختاریافته، اعتبارسنجی خروجی و کش پاسخ‌های تکراری",
        ),
    ),
    (
        ("دیزاین", "طراحی", "ui", "ux", "ریسپانسیو", "قالب"),
        (
            "تجربه‌ی کاربری موبایل و یکدستی کامپوننت‌ها",
            "دیزاین‌سیستم سبک + پیاده‌سازی ریسپانسیو با تست روی مرورگرهای واقعی",
        ),
    ),
    (
        ("داده", "postgres", "mysql", "sql", "دیتابیس"),
        (
            "ساختار دیتابیس و ایندکس‌ها، عامل اصلی سرعت در ماه‌های بعد",
            "طراحی نرمال‌شده + ایندکس روی کوئری‌های پرتکرار و مهاجرت‌های نسخه‌دار",
        ),
    ),
    (
        ("وردپرس", "المنتور", "elementor", "لندینگ"),
        (
            "سرعت و نگه‌داری آسان توسط خود کارفرما",
            "قالب سبک، افزونه‌های حداقلی و تحویل به‌همراه ویدیوی آموزش مدیریت سایت",
        ),
    ),
]

DEFAULT_ENTRIES: list[tuple[str, str]] = [
    (
        "شفاف‌سازی دامنه‌ی کار در ابتدا تا خروجی همان چیزی باشد که در ذهن شماست",
        "تحویل مرحله‌ای با بازخورد در هر مرحله، به‌جای تحویل یک‌باره در پایان",
    ),
    (
        "کیفیت کد و امکان توسعه‌ی بعدی توسط تیم دیگر",
        "کد تمیز و مستند، تحویل در مخزن گیت همراه با راهنمای نصب",
    ),
    (
        "ریسک تأخیر در تحویل",
        "تقسیم کار به اسپرینت‌های کوتاه با دموی قابل بررسی",
    ),
]


def insights_for(project: Project, limit: int = 4) -> list[tuple[str, str]]:
    """Pick playbook entries relevant to this project, padded with safe defaults."""
    haystack = " ".join(
        [
            project.title,
            project.description,
            project.category or "",
            " ".join(project.skills),
        ]
    )
    picked: list[tuple[str, str]] = []
    for triggers, entry in PLAYBOOK:
        if has_any(haystack, triggers) and entry not in picked:
            picked.append(entry)
        if len(picked) >= limit:
            break
    for entry in DEFAULT_ENTRIES:
        if len(picked) >= limit:
            break
        if entry not in picked:
            picked.append(entry)
    return picked[:limit]
