"""Prompt construction for Persian bid messages.

Structure is fixed on purpose (short, scannable, no fluff):
  1. سلام و وقت بخیر
  2. معرفی کوتاه برنامه‌نویس (from profile.yaml only — never invented)
  3. چالش‌های پروژه و نحوه پیاده‌سازی (grounded in the employer's own words)
  4. جمع‌بندی + قیمت و زمان تحویل + دعوت به گفت‌وگو
"""

from __future__ import annotations

from ..config import Profile, ProposalConfig
from ..models import Project
from ..pricing.budget import format_toman
from ..text import to_persian_digits, truncate

SYSTEM_PROMPT = """تو یک مهندس نرم‌افزار ارشد و فریلنسر برجسته ایرانی هستی که بهترین پیشنهادها (proposals) را برای پروژه‌های فریلنسری می‌نویسد.
هدف اصلی تو: تحلیل بسیار دقیق تمامی جزئیات متن پروژه کارفرما و نوشتن یک پیشنهادی کاملاً اختصاصی، فنی، متقاعدکننده و کاملاً طبیعی.

قواعد سخت‌گیرانه و غیرقابل‌تخلف:
- فقط فارسی روان، روان‌شناسانه و بدون کلمات قلم‌سلمبه زاید بنویس. اصطلاحات تخصصی انگلیسی فقط داخل پرانتز.
- تحلیل همه‌جانبه پروژه: متای کامل پروژه را بخوان. اگر کارفرما سؤال صریحی پرسیده، الزامی تعیین کرده یا خروجی خاصی خواسته، حتماً در متن پیشنهاد به آن‌ها پاسخ مستقیم و فنی بده.
- صداقت مطلق: هرگز مهارتی، تجربه، مشتری یا نمونه‌کاری که صریحاً در بخش «پروفایل» نیامده را ادعا نکن یا ناهماهنگی در تکنولوژی ایجاد نکن.
- عدم تغییر اعداد تجاری: قیمت پیشنهادی ({amount_token}) و زمان تحویل ({delivery_token}) را دقیقاً با همان فرمت بنویس و هرگز تغییر نده.
- فرمت و ظاهری: بدون هیچ‌گونه مارک‌داون (بدون **، ##، ```، <b>)، بدون تیتر و بدون بولت‌های ستاره‌ای (* یا •). حداکثر {max_emoji} ایموجی.
- طول استاندارد: بین ۷۰۰ تا ۱۴۰۰ کاراکتر. پاراگراف‌های کوتاه (۲ تا ۴ خط) و خوانا برای کارفرما.
- ساختار روان و یکپارچه بدون شماره‌گذاری:
  ۱) سلام و وقت بخیر
  ۲) معرفی یک‌خطی کاملاً مرتبط با موضوع همین پروژه (از اطلاعات پروفایل)
  ۳) تحلیل فنی ۳ تا ۵ چالش اصلی همین پروژه و راه‌حل‌های معماری و عملیاتی تو برای هر کدام (همراه با پاسخ صریح به سؤالات یا الزامات کارفرما در صورت وجود)
  ۴) جمع‌بندی: اعلام قیمت و زمان تحویل مشخص‌شده + دعوت به گفت‌وگو در چت
  ۵) امضا مطابق پروفایل
- اولویت سبک: دستورهای بخش «لحن و سبک نگارش کاربر» حتماً اعمال شوند مگر آنکه با قوانین عدم ادعای دروغین یا عدم وجود مارک‌داون تداخل داشته باشند."""

USER_PROMPT_TEMPLATE = """# پروفایل برنامه‌نویس (تنها منبع مجاز برای ادعاهای سابقه و مهارت)
نام: {full_name}
نقش: {role}
سابقه: {years} سال
تکنولوژی‌ها: {stack}
نمونه‌کار/دستاوردهای قابل ذکر: {highlights}
پورتال/لینک: {portfolio}
لحن مورد علاقه: {tone}
دستورهای سبک نگارش کاربر (الزامی): {style}
ممنوعیت‌ها: {forbidden}

# اطلاعات و جزئیات پروژه کارفرما
{project_text}

# نکات فنی و پیشنهادی راهنما (Playbook hints)
{playbook_hints}

# تصمیم‌های تجاری (عیناً استفاده شود)
قیمت پیشنهادی: {amount} تومان
زمان تحویل: {delivery}
بازه بودجه‌ی اعلامی کارفرما: {budget}

# دستورالعمل نهایی خروجی
قبل از نوشتن، تمام متن و الزامات کارفرما را تحلیل کن. متن پیشنهاد را بنویس که مشخص باشد تمام توضیحات کارفرما عمیقاً خوانده شده است.
فقط متن نهایی پیام پیشنهاد را بنویس؛ هیچ عنوان، مقدمه، توضیح اضافی یا برچسبی قبل یا بعد از متن قرار نده."""


def build_prompts(
    project: Project,
    profile: Profile,
    config: ProposalConfig,
    amount: int,
    delivery_days: int | None,
    playbook_hints: str = "",
) -> tuple[str, str]:
    """Return (system_prompt, user_prompt)."""
    delivery = f"{delivery_days} روز" if delivery_days else "توافقی"
    project_text = truncate(project.text_for_llm, 5_000)
    amount_str = to_persian_digits(f"{amount:,}")
    delivery_str = to_persian_digits(delivery)
    system = SYSTEM_PROMPT.format(
        max_emoji=config.max_emoji,
        amount_token=amount_str,
        delivery_token=delivery_str,
    )
    # The user's own voice always beats the generic profile tone.
    effective_tone = (config.tone or profile.tone or "حرفه‌ای، صمیمی، مسلط و بدون اغراق").strip()
    style = (config.style_instructions or "").strip()
    style = truncate(style, 1_000) if style else "خاصی تعیین نشده؛ بسیار طبیعی، دقیق و تخصصی بنویس."
    user = USER_PROMPT_TEMPLATE.format(
        full_name=profile.full_name,
        role=profile.role,
        years=profile.years_experience,
        stack="، ".join(profile.stack),
        highlights="؛ ".join(profile.highlights) or "ذکر نشده (هیچ ادعایی نکن)",
        portfolio=profile.portfolio_url or "ذکر نشده (لینک نساز)",
        tone=effective_tone,
        style=style,
        forbidden="؛ ".join(profile.forbidden_claims) or "—",
        project_text=project_text,
        playbook_hints=playbook_hints.strip() or "نکته خاصی موجود نیست؛ بر اساس جزئیات خود پروژه تحلیل کن.",
        amount=amount_str,
        delivery=delivery_str,
        budget=project.budget.as_text() if project.budget else "اعلام نشده",
    )
    return system, user


REPAIR_PROMPT = """پیام زیر به‌عنوان پیشنهاد پروژه نوشته شده اما این ایرادها را دارد: {issues}

متن را بازنویسی کن تا همه ایرادها رفع شوند و ساختار (سلام → معرفی → چالش‌ها و راه‌حل → جمع‌بندی با قیمت و زمان) حفظ شود.
فقط متن اصلاح‌شده را برگردان.

---
{text}
"""

__all__ = ["build_prompts", "REPAIR_PROMPT", "SYSTEM_PROMPT", "format_toman"]
