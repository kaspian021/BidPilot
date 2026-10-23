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

SYSTEM_PROMPT = """تو یک برنامه‌نویس حرفه‌ای ایرانی هستی که برای پروژه‌های فریلنسری پیشنهاد (bid) می‌نویسد.
وظیفه: خواندن دقیق متن پروژه کارفرما و نوشتن یک پیام پیشنهاد فارسی، طبیعی، کوتاه و متقاعدکننده.

قواعد سخت‌گیرانه:
- فقط فارسی روان بنویس. اصطلاحات فنی انگلیسی را می‌توانی داخل پرانتز بیاوری.
- هرگز چیزی از خودت نساز: نمونه‌کار، مشتری، مدرک، آمار یا مهارتی که در «پروفایل» نیامده را ادعا نکن.
- اگر پروژه مبهم است، یک یا دو سؤال شفاف‌کننده کوتاه بپرس؛ اما پیام نباید تبدیل به بازجویی شود.
- قیمت و زمان تحویل را همان‌طور که به تو داده می‌شود بنویس و تغییر نده.
- بدون مارک‌داون، بدون بولت‌های ستاره‌ای، بدون تیتر. حداکثر {max_emoji} ایموجی.
- بین ۷۰۰ تا ۱۴۰۰ کاراکتر. پاراگراف‌های کوتاه (۲ تا ۴ خط).
- از عبارت‌های تبلیغاتی توخالی، تعریف الکی از کارفرما و «تضمین» استفاده نکن.
- ساختار ثابت، بدون شماره‌گذاری:
  ۱) سلام و وقت بخیر
  ۲) معرفی یک‌خطی برنامه‌نویس + ارتباط تجربه با همین پروژه
  ۳) ۳ تا ۵ چالش/نکته کلیدی که در همین پروژه دیده‌ای و راه‌حل پیشنهادی تو برای هر کدام
  ۴) جمع‌بندی: قیمت پیشنهادی، زمان تحویل، اینکه آماده‌ای جزئیات را در چت روشن کنی و شروع کنی
- در پایان پیام، امضا را همان‌طور که در پروفایل آمده قرار بده.
- بخش «لحن و سبک نگارش» را دقیقاً رعایت کن؛ اگر با قوانین بالا تضاد داشت، قوانین بالا اولویت دارند."""

USER_PROMPT_TEMPLATE = """# پروفایل برنامه‌نویس (تنها منبع مجاز برای ادعاها)
نام: {full_name}
نقش: {role}
سابقه: {years} سال
تکنولوژی‌ها: {stack}
نمونه‌کار/دستاوردهای قابل ذکر: {highlights}
پورتال: {portfolio}
لحن پیشنهادی: {tone}
دستورهای سبک نگارش کاربر (الزامی): {style}
ممنوع (به هیچ شکل): {forbidden}

# پروژه کارفرما
{project_text}

# تصمیم‌های تجاری (تغییر نده)
قیمت پیشنهادی: {amount} تومان
زمان تحویل: {delivery}
بازه بودجه‌ی اعلامی کارفرما: {budget}

# خروجی
فقط متن نهایی پیام پیشنهاد را بنویس. هیچ توضیح یا برچسبی اضافه نکن."""


def build_prompts(
    project: Project,
    profile: Profile,
    config: ProposalConfig,
    amount: int,
    delivery_days: int | None,
) -> tuple[str, str]:
    """Return (system_prompt, user_prompt)."""
    delivery = f"{delivery_days} روز" if delivery_days else "توافقی"
    project_text = truncate(project.text_for_llm, 4_000)
    system = SYSTEM_PROMPT.format(max_emoji=config.max_emoji)
    # The user's own voice always beats the generic profile tone.
    effective_tone = (config.tone or profile.tone or "حرفه‌ای، صمیمی، بدون اغراق").strip()
    style = (config.style_instructions or "").strip()
    style = truncate(style, 1_000) if style else "خاصی تعیین نشده؛ طبیعی و انسانی بنویس."
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
        amount=to_persian_digits(f"{amount:,}"),
        delivery=to_persian_digits(delivery),
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
