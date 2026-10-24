"""Guided setup — every question the user must answer, in one place.

The same question catalogue powers two frontends:

* ``bidpilot setup`` — an interactive terminal wizard;
* the Telegram bot (``/setup``, ``/style``, ``/profile``, ``/llm``, …).

Nothing is read from ``.env``: answers are written to
:class:`bidpilot.settings_store.SettingsStore` and immediately take effect
because :func:`bidpilot.config.load_config` overlays the store on the YAML
defaults.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .llm_providers import (
    DEFAULT_PROVIDER,
    PRESETS,
    ProviderPreset,
    get_preset,
    provider_choices,
    verify_provider,
)
from .settings_store import SettingsStore, parse_chat_ids, parse_list_answer
from .text import to_persian_digits

KEEP = object()  #: answer means "leave the stored value untouched"

STAGE_TITLES: dict[str, str] = {
    "telegram": "اتصال تلگرام",
    "llm": "هوش مصنوعی رایگان",
    "market": "سایت فریلنسری و حساب کاربری",
    "profile": "پروفایل و نمونه‌کار",
    "style": "لحن و سبک نوشتن پیشنهاد",
    "pricing": "قیمت پیشنهادی",
    "mode": "حالت ارسال",
}


class QuestionError(ValueError):
    """Invalid answer — the message is Persian and safe to show to the user."""


@dataclass(frozen=True)
class Choice:
    value: str
    label: str
    description: str = ""


@dataclass
class Question:
    key: str
    prompt: str
    kind: str = "text"  # text | secret | int | list | choice | confirm
    stage: str = "profile"
    choices: list[Choice] = field(default_factory=list)
    default: Any = None
    help: str = ""
    optional: bool = False
    allow_custom: bool = False
    when: Callable[[SettingsStore], bool] | None = None
    validator: Callable[[Any], str | None] | None = None
    dynamic_prompt: Callable[[SettingsStore], str] | None = None
    dynamic_help: Callable[[SettingsStore], str] | None = None
    dynamic_default: Callable[[SettingsStore], Any] | None = None
    key_fn: Callable[[SettingsStore], str] | None = None

    # ------------------------------------------------------------- helpers
    def store_key(self, store: SettingsStore) -> str:
        return self.key_fn(store) if self.key_fn else self.key

    def applicable(self, store: SettingsStore) -> bool:
        return self.when(store) if self.when else True

    def text(self, store: SettingsStore) -> str:
        return self.dynamic_prompt(store) if self.dynamic_prompt else self.prompt

    def help_text(self, store: SettingsStore) -> str:
        return self.dynamic_help(store) if self.dynamic_help else self.help

    def current_default(self, store: SettingsStore) -> Any:
        if self.dynamic_default is not None:
            return self.dynamic_default(store)
        if self.default is not None:
            return self.default
        return store.get(self.store_key(store))

    def choice_list(self, store: SettingsStore) -> list[Choice]:
        return list(self.choices)


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------
def _validate_bot_token(value: Any) -> str | None:
    text = str(value).strip()
    if not re.match(r"^\d{5,}:[A-Za-z0-9_-]{25,}$", text):
        return "فرمت توکن درست نیست. توکن شبیه «123456789:AAH...» است و از @BotFather گرفته می‌شود."
    return None


def _validate_years(value: Any) -> str | None:
    try:
        years = int(value)
    except (TypeError, ValueError):
        return "یک عدد بنویس (مثلاً ۵)."
    if not 0 < years <= 60:
        return "عدد سال تجربه باید بین ۱ و ۶۰ باشد."
    return None


def _validate_url(value: Any) -> str | None:
    text = str(value).strip()
    if text and not re.match(r"^https?://\S+$", text):
        return "لینک باید با http:// یا https:// شروع شود."
    return None


def _validate_short_text(value: Any) -> str | None:
    if len(str(value).strip()) < 2:
        return "این مقدار خیلی کوتاه است."
    return None


def _validate_chat_ids(value: Any) -> str | None:
    if not parse_chat_ids(str(value)):
        return "حداقل یک chat_id عددی لازم است."
    return None


# ---------------------------------------------------------------------------
# Question catalogue
# ---------------------------------------------------------------------------
def telegram_questions(store: SettingsStore) -> list[Question]:
    return [
        Question(
            key="telegram.bot_token",
            prompt="توکن ربات تلگرام را بفرست",
            kind="secret",
            stage="telegram",
            validator=_validate_bot_token,
            help=(
                "در تلگرام به @BotFather پیام بده → /newbot → نام و آیدی انتخاب کن → "
                "توکن را همین‌جا بفرست. این توکن ذخیره می‌شود و دیگر لازم نیست جایی وارد شود."
            ),
            when=lambda s: not s.get("telegram.bot_token"),
        ),
        Question(
            key="telegram.chat_ids",
            prompt="chat_id خودت را بفرست",
            kind="list",
            stage="telegram",
            validator=_validate_chat_ids,
            help=(
                "اگر ربات را در تلگرام استارت کرده باشی، خودکار پر می‌شود. در غیر این صورت "
                "یک پیام به ربات بده و با «bidpilot telegram-whoami» پیدایش کن."
            ),
            when=lambda s: not s.get("telegram.chat_ids"),
        ),
    ]


def _provider_choices() -> list[Choice]:
    return [Choice(preset.id, preset.title, preset.note) for preset in provider_choices()]


def _current_provider(store: SettingsStore) -> str:
    provider = str(store.get("llm.provider") or DEFAULT_PROVIDER).strip().lower()
    return provider if provider in PRESETS else DEFAULT_PROVIDER


def _provider_default(store: SettingsStore) -> str:
    return _current_provider(store)


def _provider_of(store: SettingsStore) -> ProviderPreset:
    return get_preset(_current_provider(store))


def _provider_requires_key(store: SettingsStore) -> bool:
    return _provider_of(store).requires_key


def _provider_key_help(store: SettingsStore) -> str:
    preset = _provider_of(store)
    lines = [f"مدل پیش‌فرض: {preset.model}"]
    if preset.note:
        lines.append(preset.note)
    if preset.key_url:
        lines.append(f"گرفتن کلید رایگان: {preset.key_url}")
    lines.append("کلید فقط روی همین دستگاه ذخیره می‌شود. با /skip از تغییر صرف‌نظر می‌کنی.")
    return "\n".join(lines)


def _provider_key_prompt(store: SettingsStore) -> str:
    return f"کلید API {_provider_of(store).title} را بفرست"


def llm_questions(store: SettingsStore) -> list[Question]:
    return [
        Question(
            key="llm.provider",
            prompt="کدام هوش مصنوعی رایگان را استفاده کنیم؟",
            kind="choice",
            stage="llm",
            choices=_provider_choices(),
            dynamic_default=_provider_default,
            help="همه‌ی این گزینه‌ها API رایگان با توکن ماندگار دارند. پیشنهاد: Groq.",
        ),
        Question(
            key="llm.api_key",
            prompt="کلید API",
            kind="secret",
            stage="llm",
            optional=True,
            dynamic_prompt=_provider_key_prompt,
            dynamic_help=_provider_key_help,
            when=_provider_requires_key,
        ),
    ]


#: Persian labels/descriptions for the built-in marketplaces.
SITE_LABELS: dict[str, tuple[str, str]] = {
    "parscoders": ("پارس‌کدرز (parscoders.com)", "بزرگ‌ترین بازار پروژه‌های برنامه‌نویسی ایران"),
    "ponisha": ("پونیشا (ponisha.ir)", "پروژه‌های متنوع، از طراحی تا برنامه‌نویسی"),
    "karlancer": ("کارلنسر (karlancer.com)", "پروژه‌های کوچک و متوسط، تسویه سریع"),
    "mock": ("سایت آزمایشی (بدون شبکه)", "برای تست کامل بدون هیچ حساب واقعی"),
}
DEFAULT_SITE = "parscoders"


def site_label(site: str) -> str:
    return SITE_LABELS.get(site, (site, ""))[0]


def _marketplace_choices() -> list[Choice]:
    from .sources import available_sources

    choices: list[Choice] = []
    for name in available_sources():
        label, description = SITE_LABELS.get(name, (name, "افزودن سایت جدید فقط با یک فایل YAML."))
        choices.append(Choice(name, label, description))
    # the mock source is useful first, the real ones after
    choices.sort(key=lambda c: (c.value == "mock", c.value))
    return choices


def _current_marketplace(store: SettingsStore) -> str:
    return str(store.get("marketplace.name") or DEFAULT_SITE)


def _market_default(store: SettingsStore) -> str:
    return _current_marketplace(store)


def _needs_account(store: SettingsStore) -> bool:
    return _current_marketplace(store) != "mock"


def _cred_key(field_name: str) -> Callable[[SettingsStore], str]:
    """Credentials are per site: credentials.<site>.<field>."""
    return lambda store: f"credentials.{_current_marketplace(store)}.{field_name}"


def site_credential(store: SettingsStore, field_name: str, site: str | None = None) -> Any:
    return store.get(f"credentials.{site or _current_marketplace(store)}.{field_name}")


def site_method(store: SettingsStore, site: str | None = None) -> str:
    """Login method of a site — the question default counts as "not answered yet"."""
    return str(site_credential(store, "method", site) or "cookie")


def marketplace_questions(store: SettingsStore) -> list[Question]:
    return [
        Question(
            key="marketplace.name",
            prompt="روی کدام سایت فریلنسری پیشنهاد بدهیم؟",
            kind="choice",
            stage="market",
            choices=_marketplace_choices(),
            dynamic_default=_market_default,
        ),
        Question(
            key="credentials.method",
            prompt="ورود به سایت چطور انجام شود؟",
            kind="choice",
            stage="market",
            key_fn=_cred_key("method"),
            choices=[
                Choice(
                    "credentials",
                    "نام کاربری و رمز عبور",
                    "ربات خودش لاگین می‌کند؛ اگر سایت کپچا داشت، از روش کوکی استفاده کن.",
                ),
                Choice(
                    "cookie",
                    "کوکی نشست (ساده‌تر و مطمئن‌تر)",
                    "از مرورگر، بعد از لاگین، مقدار PHPSESSID را کپی کن.",
                ),
            ],
            default="cookie",
            when=_needs_account,
        ),
        Question(
            key="credentials",
            prompt="نام کاربری حساب",
            stage="market",
            key_fn=_cred_key("username"),
            help="فقط برای لاگین ربات استفاده می‌شود.",
            when=lambda s: _needs_account(s) and site_method(s) != "cookie",
        ),
        Question(
            key="credentials",
            prompt="رمز عبور حساب",
            kind="secret",
            stage="market",
            key_fn=_cred_key("password"),
            when=lambda s: _needs_account(s) and site_method(s) != "cookie",
        ),
        Question(
            key="credentials",
            prompt="کوکی نشست سایت (PHPSESSID=…)",
            kind="secret",
            stage="market",
            key_fn=_cred_key("cookie"),
            help=(
                "در مرورگر لاگین کن → DevTools → Application → Cookies → کوکی نشست "
                "(معمولاً PHPSESSID) را کپی و همین‌جا بفرست. "
                "اگر منقضی شد، دوباره /market را بزن — برای هر سایت، حساب جداست."
            ),
            when=lambda s: _needs_account(s) and site_method(s) == "cookie",
        ),
    ]


def _profile_field(name: str) -> Any:
    defaults = {
        "first_name": "",
        "full_name": "",
        "role": "برنامه‌نویس",
        "years_experience": 5,
        "stack": [],
        "highlights": [],
        "portfolio_url": "",
        "signature": "",
    }
    return defaults.get(name)


#: How the user wants their profile filled in — one input instead of eight questions.
PROFILE_SOURCE_CHOICES = (
    Choice(
        "resume",
        "رزومه PDF (پیشنهادی)",
        "فایل PDF رزومه را همین‌جا بفرست؛ نام، سابقه، مهارت‌ها و نمونه‌کارها خودکار استخراج می‌شوند.",
    ),
    Choice(
        "github",
        "لینک گیت‌هاب",
        "پروفایل و مخزن‌های عمومی‌ات را می‌خوانم و مهارت‌ها و نمونه‌کارها را برمی‌دارم.",
    ),
    Choice(
        "portfolio",
        "لینک سایت/پورتفولیو",
        "صفحه را می‌خوانم و هر چه در آن هست (مهارت، نمونه‌کار، لینک) را استخراج می‌کنم.",
    ),
    Choice("manual", "ورود دستی", "خودت نام، سابقه، مهارت‌ها و نمونه‌کارها را می‌نویسی."),
    Choice("skip", "فعلاً رد کن", "هر چه الان در پروفایل هست حفظ می‌شود."),
)

PROFILE_SOURCE_VALUES = tuple(choice.value for choice in PROFILE_SOURCE_CHOICES)
_PROFILE_KEYS = ("first_name", "full_name", "role", "years_experience", "stack", "highlights")


def _profile_source(store: SettingsStore) -> str:
    stored = str(store.get("profile.source") or "").strip().lower()
    if stored in PROFILE_SOURCE_VALUES:
        return stored
    # Existing users answered the old manual questions: don't force a re-import.
    if any(store.get(f"profile.{key}") for key in _PROFILE_KEYS):
        return "manual"
    return "resume"


def _profile_source_default(store: SettingsStore) -> str:
    return _profile_source(store)


def _profile_url_prompt(store: SettingsStore) -> str:
    if _profile_source(store) == "github":
        return "لینک پروفایل گیت‌هابت را بفرست"
    return "لینک سایت شخصی یا پورتفولیوت را بفرست"


def _profile_url_help(store: SettingsStore) -> str:
    if _profile_source(store) == "github":
        return (
            "مثال: https://github.com/kaspian021 — از API عمومی گیت‌هاب (بدون توکن) "
            "مخزن‌ها و توضیحاتشان خوانده می‌شود."
        )
    return (
        "مثال: https://kaspian021.github.io/portfolio — صفحه خوانده و متنش با همان کلید "
        "هوش مصنوعی تحلیل می‌شود. اگر قبلاً پروفایل را ساختی، با /skip رد کن."
    )


def profile_questions(store: SettingsStore) -> list[Question]:
    """One import question + the manual fallback — never eight questions in a row."""
    return [
        Question(
            key="profile.source",
            prompt="پروفایلت را از کدام راه بسازیم؟",
            kind="choice",
            stage="profile",
            choices=list(PROFILE_SOURCE_CHOICES),
            dynamic_default=_profile_source_default,
            help=(
                "لازم نیست رزومه را دستی تایپ کنی: یک فایل PDF، یا لینک گیت‌هاب/پورتفولیو کافی است. "
                "استخراج اطلاعات با همان کلید هوش مصنوعی رایگان انجام می‌شود و بعد نشانت می‌دهم."
            ),
        ),
        Question(
            key="profile.resume_path",
            prompt="فایل PDF رزومه را همین‌جا بفرست",
            kind="file",
            stage="profile",
            optional=True,
            help=(
                "فایل را در تلگرام به‌صورت Document (نه عکس) بفرست. حداکثر ۲۰ مگابایت. "
                "در ترمینال مسیر فایل را بنویس، مثلاً C:\\Users\\me\\resume.pdf — "
                "با /skip رد می‌شوی."
            ),
            when=lambda s: _profile_source(s) == "resume",
        ),
        Question(
            key="profile.source_url",
            prompt="لینک را بفرست",
            stage="profile",
            optional=True,
            validator=_validate_url,
            dynamic_prompt=_profile_url_prompt,
            dynamic_help=_profile_url_help,
            when=lambda s: _profile_source(s) in {"github", "portfolio"},
        ),
        Question(
            key="profile.first_name",
            prompt="اسم کوچکت چیست؟",
            stage="profile",
            validator=_validate_short_text,
            dynamic_default=lambda s: s.get("profile.first_name") or _profile_field("first_name"),
            when=lambda s: _profile_source(s) == "manual",
        ),
        Question(
            key="profile.full_name",
            prompt="نام و نام خانوادگی کامل",
            stage="profile",
            validator=_validate_short_text,
            dynamic_default=lambda s: s.get("profile.full_name") or _profile_field("full_name"),
            when=lambda s: _profile_source(s) == "manual",
        ),
        Question(
            key="profile.role",
            prompt="تخصص اصلی‌ات چیست؟",
            stage="profile",
            help="مثال: برنامه‌نویس فول‌استک، متخصص وردپرس، طراح UI/UX",
            dynamic_default=lambda s: s.get("profile.role") or _profile_field("role"),
            when=lambda s: _profile_source(s) == "manual",
        ),
        Question(
            key="profile.years_experience",
            prompt="چند سال سابقه داری؟",
            kind="int",
            stage="profile",
            validator=_validate_years,
            dynamic_default=lambda s: s.get("profile.years_experience") or _profile_field("years_experience"),
            when=lambda s: _profile_source(s) == "manual",
        ),
        Question(
            key="profile.stack",
            prompt="مهارت‌ها و تکنولوژی‌هایت را بنویس",
            kind="list",
            stage="profile",
            help="با کاما جدا کن؛ مثال: Python، Django، React، PostgreSQL",
            dynamic_default=lambda s: s.get("profile.stack") or _profile_field("stack"),
            when=lambda s: _profile_source(s) == "manual",
        ),
        Question(
            key="profile.highlights",
            prompt="۲ تا ۴ نمونه‌کار یا دستاورد واقعی‌ات را بنویس",
            kind="list",
            stage="profile",
            help="با کاما جدا کن. هوش مصنوعی فقط همین‌ها را می‌تواند ادعا کند — پس واقعی باشند.",
            dynamic_default=lambda s: s.get("profile.highlights") or _profile_field("highlights"),
            when=lambda s: _profile_source(s) == "manual",
        ),
        Question(
            key="profile.portfolio_url",
            prompt="لینک پورتفولیو یا گیت‌هاب",
            stage="profile",
            optional=True,
            validator=_validate_url,
            dynamic_default=lambda s: s.get("profile.portfolio_url") or _profile_field("portfolio_url"),
            when=lambda s: _profile_source(s) == "manual",
        ),
        Question(
            key="profile.signature",
            prompt="امضای پایان پیام",
            stage="profile",
            optional=True,
            help="مثال: با احترام، سارا محمدی — با /skip رد می‌شوی.",
            dynamic_default=lambda s: s.get("profile.signature") or _profile_field("signature"),
            when=lambda s: _profile_source(s) == "manual",
        ),
    ]


DEFAULT_BID_PERCENT = 60


def bid_percent(store: SettingsStore) -> int:
    """How much of the employer's budget we bid (percent), 10‑100."""
    stored = store.get("pricing.bid_percent")
    if stored is None:
        ratio = store.get("pricing.ratio")
        try:
            stored = round(float(ratio) * 100) if ratio is not None else None
        except (TypeError, ValueError):
            stored = None
    try:
        value = int(stored) if stored is not None else DEFAULT_BID_PERCENT
    except (TypeError, ValueError):
        value = DEFAULT_BID_PERCENT
    return min(100, max(10, value))


def pricing_questions(store: SettingsStore) -> list[Question]:
    return [
        Question(
            key="pricing.bid_percent",
            prompt="چند درصد از بودجه‌ی اعلامی کارفرما را پیشنهاد بدهیم؟",
            kind="int",
            stage="pricing",
            default=DEFAULT_BID_PERCENT,
            validator=lambda value: (
                None if 10 <= int(value) <= 100 else "عددی بین ۱۰ تا ۱۰۰ بنویس (مثلاً ۶۰ برای ۶۰٪ بودجه)."
            ),
            help=(
                "اگر کارفرما بازه بدهد (مثلاً ۵۰۰٬۰۰۰ تا ۱٬۵۰۰٬۰۰۰)، همین درصد از بازه انتخاب می‌شود "
                "و همیشه داخل بازه می‌ماند. ۵۰٪ = وسط بازه، ۳۰٪ = رقابتی‌تر، ۸۰٪ = گران‌تر. "
                "عدد ۱۰۰ یعنی سقف بودجه."
            ),
            dynamic_default=lambda s: bid_percent(s),
        )
    ]


TONE_CHOICES = (
    Choice("حرفه‌ای، صمیمی و بدون اغراق", "حرفه‌ای و صمیمی"),
    Choice("رسمی و کوتاه", "رسمی و کوتاه"),
    Choice("خودمانی و گرم", "خودمانی و گرم"),
    Choice("فنی و مستقیم، بدون تعارف", "فنی و مستقیم"),
    Choice("مشاوره‌ای؛ با پرسیدن یک سؤال شفاف‌کننده", "مشاوره‌ای و سؤال‌محور"),
)


def style_questions(store: SettingsStore) -> list[Question]:
    return [
        Question(
            key="proposal.tone",
            prompt="پیشنهادها را با چه لحنی بنویسیم؟",
            kind="choice",
            stage="style",
            choices=list(TONE_CHOICES),
            allow_custom=True,
            help=("می‌توانی شماره یکی از گزینه‌ها را بفرستی، یا لحن دلخواهت را با جمله‌ی خودت بنویسی."),
            dynamic_default=lambda s: s.get("proposal.tone") or TONE_CHOICES[0].value,
        ),
        Question(
            key="proposal.style_instructions",
            prompt="دستورهای سبک نوشتن (چطور و با چه تأکیدی پیشنهاد بدهیم؟)",
            stage="style",
            optional=True,
            help=(
                "مثال: «همیشه اول یک سؤال درباره‌ی ددلاین بپرس، روی تحویل مرحله‌ای تأکید کن، "
                "از تعریف‌کردن از کارفرما پرهیز کن، جمله‌ها کوتاه باشند.» — با /skip رد می‌شوی."
            ),
            dynamic_default=lambda s: s.get("proposal.style_instructions") or "",
        ),
        Question(
            key="proposal.max_chars",
            prompt="حداکثر طول هر پیشنهاد (کاراکتر)",
            kind="int",
            stage="style",
            default=1500,
            help="بین ۴۰۰ تا ۳۰۰۰. کوتاه‌تر = خواناتر.",
            validator=lambda value: None if 400 <= int(value) <= 3000 else "عدد بین ۴۰۰ تا ۳۰۰۰ بنویس.",
            dynamic_default=lambda s: s.get("proposal.max_chars") or 1500,
        ),
    ]


def mode_questions(store: SettingsStore) -> list[Question]:
    return [
        Question(
            key="runtime.submit_mode",
            prompt="پیشنهادها واقعاً ارسال شوند یا اول آزمایشی؟",
            kind="choice",
            stage="mode",
            choices=[
                Choice("dry_run", "آزمایشی (پیشنهاد ساخته می‌شود، ارسال نمی‌شود)", "پیشنهاد شروع"),
                Choice("live", "واقعی (پیشنهاد روی سایت ثبت می‌شود)", "بعد از دیدن چند پیشنهاد"),
            ],
            default="dry_run",
            dynamic_default=lambda s: s.get("runtime.submit_mode") or "dry_run",
        )
    ]


def all_questions(store: SettingsStore) -> list[Question]:
    """The full catalogue in the order it should be asked."""
    return [
        *telegram_questions(store),
        *llm_questions(store),
        *marketplace_questions(store),
        *profile_questions(store),
        *style_questions(store),
        *pricing_questions(store),
        *mode_questions(store),
    ]


# ---------------------------------------------------------------------------
# Parsing / applying answers
# ---------------------------------------------------------------------------
def _normalise(text: str) -> str:
    translation = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
    return re.sub(r"\s+", " ", str(text).translate(translation).strip()).lower()


def _match_choice(
    question: Question, choices: Sequence[Choice], raw: str, store: SettingsStore
) -> Choice | None:
    text = _normalise(raw)
    if not text:
        return None
    # 1-based index
    if text.isdigit():
        index = int(text) - 1
        if 0 <= index < len(choices):
            return choices[index]
    # exact value / label
    for choice in choices:
        if text in {_normalise(choice.value), _normalise(choice.label)}:
            return choice
    # prefix match, e.g. "پارس" -> parscoders
    matches = [
        choice
        for choice in choices
        if _normalise(choice.value).startswith(text) or _normalise(choice.label).startswith(text)
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def parse_answer(question: Question, raw: Any, store: SettingsStore) -> Any:
    """Validate/convert a raw user answer. Raises :class:`QuestionError`."""
    text = "" if raw is None else str(raw).strip()
    if _normalise(text) in {"/skip", "skip", "رد", "بعدا", "-"}:
        if question.optional or question.current_default(store) not in (None, ""):
            return KEEP
        raise QuestionError("این مورد لازم است و نمی‌توانی رد کنی.")

    default = question.current_default(store)
    if question.kind == "secret" and not text and question.optional:
        return KEEP

    if question.kind == "choice":
        choices = question.choice_list(store)
        if not text:
            if default is None:
                raise QuestionError("یکی از گزینه‌ها را انتخاب کن.")
            return default
        match = _match_choice(question, choices, text, store)
        if match is not None:
            return match.value
        if question.allow_custom:
            if len(text) < 3:
                raise QuestionError("لحن دلخواه را کمی کامل‌تر بنویس (حداقل ۳ کاراکتر).")
            return text
        listing = "، ".join(f"{i + 1}) {c.label}" for i, c in enumerate(choices))
        raise QuestionError(f"گزینه پیدا نشد. یکی از این‌ها را بفرست: {listing}")

    if not text:
        if default not in (None, ""):
            return default
        if question.optional:
            return KEEP
        raise QuestionError("این مورد خالی نمی‌تواند باشد؛ مقدارش را بنویس.")

    if question.kind == "file":
        from pathlib import Path as _Path

        candidate = _Path(text.strip().strip('"').strip("'")).expanduser()
        if not candidate.is_file():
            raise QuestionError(
                "فایل پیدا نشد. مسیر کامل فایل PDF را بنویس (مثلاً C:\\Users\\me\\resume.pdf)."
            )
        if candidate.suffix.lower() != ".pdf":
            raise QuestionError("فقط فایل PDF پذیرفته می‌شود.")
        return str(candidate)

    if question.kind == "int":
        try:
            number = int(_normalise(text).replace(",", ""))
        except ValueError as exc:
            raise QuestionError("یک عدد صحیح بنویس.") from exc
        if question.validator:
            problem = question.validator(number)
            if problem:
                raise QuestionError(problem)
        return number
    if question.kind == "list":
        values = parse_chat_ids(text) if question.key == "telegram.chat_ids" else parse_list_answer(text)
        if not values:
            raise QuestionError("حداقل یک مقدار بنویس.")
        if question.validator:
            # list validators work on the raw answer (e.g. chat ids)
            problem = question.validator(text)
            if problem:
                raise QuestionError(problem)
        return values
    if question.kind == "confirm":
        lowered = _normalise(text)
        if lowered in {"بله", "yes", "y", "1", "true", "اره", "آره"}:
            return True
        if lowered in {"نه", "no", "n", "0", "false", "خیر"}:
            return False
        raise QuestionError("«بله» یا «نه» را بفرست.")

    value: Any = text
    if question.key == "telegram.bot_token":
        value = text.strip()
    if question.validator:
        problem = question.validator(value)
        if problem:
            raise QuestionError(problem)
    return value


def apply_answer(store: SettingsStore, question: Question, value: Any) -> None:
    """Persist one validated answer (plus its side effects)."""
    if value is KEEP or value is None:
        return
    key = question.store_key(store)

    if key == "profile.source" and str(value).strip().lower() == "skip":
        # "فعلاً رد کن" must not pin the choice: next time we ask again
        return
    if key in {"profile.resume_path", "profile.source_url"}:
        # the actual analysis happens in the async layer (bot / terminal wizard)
        import_kind = "file" if key == "profile.resume_path" else "url"
        store.update({key: value, "profile.import_pending": {"type": import_kind, "value": value}})
        reset_secret_cache()
        return
    if key == "pricing.bid_percent":
        percent = min(100, max(10, int(value)))
        store.update({"pricing.bid_percent": percent, "pricing.ratio": round(percent / 100, 2)})
        reset_secret_cache()
        return
    if key == "llm.api_key":
        provider = str(store.get("llm.provider") or DEFAULT_PROVIDER).strip().lower()
        store.update({"llm.api_key": value, f"llm.keys.{provider}": value})
    elif key == "llm.provider":
        provider = str(value).strip().lower()
        existing = store.get(f"llm.keys.{provider}")
        store.set("llm.provider", provider)
        store.set("llm.api_key", existing or None)
        preset = PRESETS.get(provider)
        if preset is not None:
            # keep model/base_url in sync with the chosen provider
            store.update({"llm.model": preset.model, "llm.base_url": preset.base_url})
    else:
        store.set(key, value)

    # the effective config must see the new values immediately
    reset_secret_cache()


def reset_secret_cache() -> None:
    from .config import reset_secret_cache as _reset

    _reset()


def save_chat_id(store: SettingsStore, chat_id: str | int) -> None:
    chat_id = str(chat_id)
    current = [str(c) for c in store.get_list("telegram.chat_ids")]
    if chat_id not in current:
        store.set("telegram.chat_ids", [*current, chat_id])


def switch_site(store: SettingsStore, site: str) -> str:
    """Make ``site`` the active marketplace (credentials stay per site)."""
    from .sources import available_sources

    name = str(site).strip().lower()
    known = available_sources()
    if name not in known:
        options = "، ".join(f"{site_label(item)} = {item}" for item in known if item != "mock")
        raise QuestionError(f"سایت «{site}» ثبت نشده است.\nسایت‌های موجود: {options}")
    store.set("marketplace.name", name)
    reset_secret_cache()
    return name


def sites_overview(store: SettingsStore) -> list[dict[str, Any]]:
    """One row per registered marketplace, for /sites and /status."""
    from .credentials import site_credentials
    from .sources import available_sources

    active = _current_marketplace(store)
    rows: list[dict[str, Any]] = []
    for name in available_sources():
        creds = site_credentials(name, store)
        rows.append(
            {
                "site": name,
                "label": site_label(name),
                "active": name == active,
                "ready": name == "mock" or creds.ready,
                "method": creds.method,
                "summary": "بدون نیاز به ورود" if name == "mock" else creds.summary,
            }
        )
    rows.sort(key=lambda row: (not row["active"], row["site"]))
    return rows


def mark_setup_complete(store: SettingsStore, *, complete: bool = True) -> None:
    store.set("setup.completed", bool(complete))
    if complete:
        from datetime import datetime, timezone

        store.set("setup.completed_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))


# ---------------------------------------------------------------------------
# Readiness / status helpers
# ---------------------------------------------------------------------------
#: One successful dry-run/preview is required before 🔴 live submission.
PROOF_KEY = "run.proof"


def preview_proof(store: SettingsStore) -> dict[str, Any]:
    """The stored proof that a proposal was already built without sending it."""
    raw = store.get(PROOF_KEY)
    return dict(raw) if isinstance(raw, Mapping) else {}


def preview_is_fresh(store: SettingsStore) -> bool:
    """True when a preview/dry-run succeeded *for the site that is active now*."""
    proof = preview_proof(store)
    if not proof.get("ok"):
        return False
    site = str(proof.get("site") or "")
    return not site or site == _current_marketplace(store)


def mark_preview_ok(store: SettingsStore, kind: str = "preview", *, note: str = "") -> None:
    """Remember that a real proposal was generated (``preview`` or ``dry_run``)."""
    store.set(
        PROOF_KEY,
        {
            "ok": True,
            "kind": kind,
            "site": _current_marketplace(store),
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "note": note[:300],
        },
    )


def preview_proof_text(store: SettingsStore) -> str:
    """Short Persian line for /status — empty when nothing proved yet."""
    if not preview_is_fresh(store):
        return ""
    proof = preview_proof(store)
    kind = "پیش‌نمایش" if proof.get("kind") != "dry_run" else "دور آزمایشی"
    return f"🎯 یک {kind} موفق ثبت شده — حالت واقعی باز است"


def readiness(store: SettingsStore) -> dict[str, Any]:
    provider = _current_provider(store)
    preset = PRESETS[provider]
    has_key = bool(store.get("llm.api_key") or store.get(f"llm.keys.{provider}"))
    marketplace = _current_marketplace(store)
    if marketplace == "mock":
        creds_ok = True
    else:
        from .credentials import site_credentials

        creds_ok = site_credentials(marketplace, store).ready
    return {
        "site": marketplace,
        "telegram": bool(store.get("telegram.bot_token") and store.get("telegram.chat_ids")),
        "llm": bool(has_key or not preset.requires_key),
        "llm_provider": provider,
        "llm_title": preset.title,
        "llm_model": store.get("llm.model") or preset.model,
        "marketplace": marketplace,
        "credentials": creds_ok,
        "tone": store.get("proposal.tone") or "",
        "style": bool(store.get("proposal.style_instructions")),
        "mode": store.get("runtime.submit_mode") or "dry_run",
        "profile_source": str(store.get("profile.source") or ""),
        "profile_imported_at": store.get("profile.imported_at"),
        "profile_imported": bool(store.get("profile.imported_at")),
        "profile_error": str(store.get("profile.import_error") or ""),
        "bid_percent": bid_percent(store),
        "preview": preview_is_fresh(store),
        "setup_completed": bool(store.get("setup.completed")),
    }


def missing_steps(store: SettingsStore, *, include_recommended: bool = True) -> list[str]:
    """What is still missing. LLM is recommended, not required (there is a template)."""
    state = readiness(store)
    missing: list[str] = []
    if not state["telegram"]:
        missing.append("اتصال تلگرام (توکن ربات + chat_id)")
    if not state["credentials"]:
        missing.append("حساب سایت فریلنسری (نام کاربری/رمز یا کوکی)")
    if not store.get("profile.full_name"):
        missing.append("نام و پروفایل")
    if include_recommended and not state["llm"]:
        missing.append("کلید هوش مصنوعی رایگان (اختیاری ولی توصیه‌شده)")
    return missing


def status_text(store: SettingsStore) -> str:
    """Human-readable status used by the bot's /status and by ``bidpilot setup --status``."""
    state = readiness(store)
    mark = lambda ok: "✅" if ok else "❌"  # noqa: E731
    lines = [
        "<b>وضعیت BidPilot</b>",
        f"{mark(state['telegram'])} تلگرام: "
        + (
            f"{len(store.get_list('telegram.chat_ids'))} چت متصل"
            if state["telegram"]
            else "توکن/chat_id ثبت نشده"
        ),
        f"{mark(state['llm'])} هوش مصنوعی: {state['llm_title']} — {state['llm_model']}",
        f"{mark(state['credentials'])} سایت فعال: {site_label(state['marketplace'])}",
        f"{mark(bool(state['tone']))} لحن: {state['tone'] or 'تعیین نشده'}",
        f"{mark(state['style'])} دستورهای سبک: " + ("ثبت شده" if state["style"] else "ثبت نشده (اختیاری)"),
        _profile_status_line(state, store),
        f"💰 قیمت: {to_persian_digits(str(state['bid_percent']))}٪ بودجه‌ی کارفرما",
        f"حالت ارسال: {'🔴 واقعی' if state['mode'] == 'live' else '🧪 آزمایشی'}",
        f"{mark(state['preview'])} آزمایش بی‌خطر: "
        + ("انجام شده" if state["preview"] else "هنوز انجام نشده — /preview"),
        f"تنظیمات اولیه: {'کامل ✅' if state['setup_completed'] else 'ناتمام ⚠️'}",
    ]
    return "\n".join(lines)


def _profile_status_line(state: dict[str, Any], store: SettingsStore) -> str:
    """Never claim «از رزومه» when the resume was never actually read."""
    name = str(store.get("profile.full_name") or "").strip()
    if not name:
        return "❌ پروفایل: وارد نشده"
    source = _profile_source_label(state.get("profile_source") or "")
    if state.get("profile_error"):
        return (
            f"⚠️ پروفایل: {name}"
            + (f" — {source} خوانده نشد" if source else " — منبع خوانده نشد")
            + " (/profile)"
        )
    if state.get("profile_source") and not state.get("profile_imported"):
        return f"⚠️ پروفایل: {name} — {source} هنوز خوانده نشده (/profile)"
    return f"✅ پروفایل: {name}" + (f" (از {source})" if source and state.get("profile_imported") else "")


def _profile_source_label(source: str) -> str:
    return {
        "resume": "رزومه",
        "github": "گیت‌هاب",
        "portfolio": "پورتفولیو",
        "manual": "ورود دستی",
        "skip": "—",
    }.get(str(source or ""), str(source or ""))


def tone_summary(store: SettingsStore) -> str:
    tone = store.get("proposal.tone") or "حرفه‌ای و صمیمی"
    style = (store.get("proposal.style_instructions") or "").strip()
    text = f"🎭 لحن فعلی: {tone}"
    if style:
        text += f"\n📝 دستورها: {style}"
    return text


# ---------------------------------------------------------------------------
# Terminal wizard
# ---------------------------------------------------------------------------
async def _send_test_message(token: str, chat_ids: Sequence[str]) -> str:
    import httpx

    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": str(chat_ids[0]),
                "text": "✅ BidPilot تنظیم شد. از این به بعد می‌توانی همه‌چیز را همین‌جا مدیریت کنی.",
            },
        )
    return "پیام تست تلگرام ارسال شد." if response.status_code < 400 else "ارسال پیام تست تلگرام ناموفق بود."


async def _verify_key(provider_id: str, key: str) -> tuple[bool, str]:
    preset = get_preset(provider_id)
    from .llm_providers import LLMProvider

    return await verify_provider(
        LLMProvider(id=preset.id, base_url=preset.base_url, model=preset.model, api_key=key)
    )


def _prompt_question(console, question: Question, store: SettingsStore, index: int, total: int) -> Any:
    from rich.markup import escape
    from rich.prompt import Prompt

    title = f"[{index}/{total}] {STAGE_TITLES.get(question.stage, question.stage)}"
    console.print(f"\n[bold cyan]{title}[/bold cyan]")
    console.print(escape(question.text(store)))
    help_text = question.help_text(store)
    if help_text:
        console.print(f"[dim]{escape(help_text)}[/dim]")

    choices = question.choice_list(store) if question.kind == "choice" else []
    default = question.current_default(store)
    if choices:
        for i, choice in enumerate(choices, 1):
            line = f"  {i}) {choice.label}"
            if choice.value == default:
                line += "  [green](فعلی)[/green]"
            console.print(line)
            if choice.description:
                console.print(f"     [dim]{escape(choice.description)}[/dim]")

    hint = "شماره/متن"
    if question.optional:
        hint = "خالی = رد کردن"
    default_label = None
    if choices and default:
        for i, choice in enumerate(choices, 1):
            if choice.value == default:
                default_label = str(i)
                break
    elif default not in (None, "", []):
        default_label = "، ".join(str(v) for v in default) if isinstance(default, list) else str(default)

    while True:
        raw = Prompt.ask(
            f"[bold]پاسخ[/bold] ({hint})",
            default=default_label if question.kind != "secret" else None,
            password=question.kind == "secret",
            show_default=question.kind != "secret" and default_label is not None,
        )
        try:
            return parse_answer(question, raw, store)
        except QuestionError as exc:
            console.print(f"[red]{escape(str(exc))}[/red]")


def _ensure_pdf_reader(console) -> bool:
    """Read a resume PDF even when ``pypdf`` was never installed in this venv."""
    from rich.markup import escape

    from .deps import ensure, has, install_command

    if has("pypdf"):
        return True
    console.print("[cyan]بسته‌ی خواندن PDF نصب نیست؛ همین حالا نصبش می‌کنم…[/cyan]")
    ok, detail = ensure("pypdf")
    if ok:
        console.print("[green]pypdf نصب شد.[/green]")
        return True
    console.print("[yellow]نصب خودکار انجام نشد. دستی اجرا کن:[/yellow]")
    console.print(f"  [bold]{escape(install_command('pypdf'))}[/bold]")
    if detail:
        console.print(f"[dim]{escape(detail[-300:])}[/dim]")
    return False


def _run_pending_profile_import(console, store: SettingsStore) -> None:
    from rich.markup import escape

    from .profile_import import (
        ProfileImportError,
        pending_import,
        run_pending_import,
        summary_text,
    )

    pending = pending_import(store)
    if pending is None:
        return
    if pending["type"] == "file" and not _ensure_pdf_reader(console):
        store.set("profile.import_error", "pypdf نصب نیست")
        return
    console.print("[cyan]در حال خواندن و تحلیل رزومه/پروفایل…[/cyan]")
    try:
        result = asyncio.run(run_pending_import(store))
    except ProfileImportError as exc:
        store.set("profile.import_error", str(exc)[:300])
        console.print(f"[yellow]{escape(str(exc))}[/yellow]")
        return
    except Exception as exc:  # network/quota — the profile keeps its old values
        store.set("profile.import_error", f"تحلیل انجام نشد: {exc}"[:300])
        console.print(f"[yellow]تحلیل پروفایل انجام نشد: {escape(str(exc))}[/yellow]")
        return
    if result is None:  # pragma: no cover - defensive
        return
    console.print(summary_text(result.fields, title="پروفایل استخراج‌شده"))
    if result.used_llm:
        console.print("[green]استخراج با هوش مصنوعی انجام شد.[/green]")
    for warning in result.warnings:
        console.print(f"[yellow]{escape(warning)}[/yellow]")


def run_terminal_wizard(
    store: SettingsStore,
    console,
    *,
    stages: Sequence[str] | None = None,
    questions: Sequence[Question] | None = None,
    complete_marker: bool = True,
) -> bool:
    """Ask every applicable question, saving each answer immediately."""
    from rich.panel import Panel
    from rich.table import Table

    pool = list(questions) if questions is not None else all_questions(store)
    pool = [q for q in pool if stages is None or q.stage in stages]
    # conditions are re-checked per question: answering "cookie" reveals the
    # cookie field, answering "credentials" reveals username/password.
    applicable = [q for q in pool if q.applicable(store)]
    if not applicable:
        console.print("[yellow]چیزی برای پرسیدن نیست — همه‌چیز تنظیم شده است.[/yellow]")
        return True

    console.print(
        Panel(
            "هیچ فایل .env لازم نیست. پاسخ‌ها امن در data/settings.json ذخیره می‌شوند و "
            "همین‌جا قابل تغییرند. با Ctrl+C هر زمان می‌توانی خارج شوی؛ پاسخ‌های قبلی ذخیره شده‌اند.",
            title="راه‌اندازی BidPilot",
            border_style="cyan",
        )
    )

    answered = 0
    try:
        for question in pool:
            if not question.applicable(store):
                continue
            visible = [q for q in pool if q.applicable(store)]
            position = visible.index(question) + 1
            value = _prompt_question(console, question, store, position, len(visible))
            apply_answer(store, question, value)
            answered += 1
    except (KeyboardInterrupt, EOFError):
        console.print("\n[yellow]متوقف شد؛ پاسخ‌های داده‌شده ذخیره شده‌اند.[/yellow]")
        return False

    # A resume/URL answer is analysed here (the questions themselves stay sync).
    _run_pending_profile_import(console, store)

    # post-setup checks: test the key and the Telegram connection when possible
    provider = _current_provider(store)
    key = store.get("llm.api_key") or store.get("llm.keys." + provider)
    only = set(stages) if stages else None
    if key and (only is None or only == {"llm"}):
        with console.status("در حال تست کلید هوش مصنوعی…"):
            ok, message = asyncio.run(_verify_key(provider, str(key)))
        console.print(f"[green]{message}[/green]" if ok else f"[yellow]{message}[/yellow]")

    token = store.get("telegram.bot_token")
    chat_ids = store.get_list("telegram.chat_ids")
    if token and chat_ids and (only is None or only == {"telegram"}):
        try:
            message = asyncio.run(_send_test_message(str(token), [str(c) for c in chat_ids]))
            console.print(f"[green]{message}[/green]")
        except Exception as exc:  # network errors must not lose the answers
            console.print(f"[yellow]پیام تست تلگرام ارسال نشد: {exc}[/yellow]")

    state = readiness(store)
    table = Table(title="خلاصه‌ی تنظیمات")
    table.add_column("بخش")
    table.add_column("وضعیت")
    table.add_row("تلگرام", "✅" if state["telegram"] else "❌")
    table.add_row("هوش مصنوعی", f"{'✅' if state['llm'] else '❌'} {state['llm_title']}")
    table.add_row(
        "سایت",
        f"{'✅' if state['credentials'] else '❌'} {site_label(state['marketplace'])}",
    )
    table.add_row("لحن", state["tone"] or "—")
    table.add_row("حالت ارسال", "واقعی" if state["mode"] == "live" else "آزمایشی")
    console.print(table)

    if complete_marker and stages is None:
        mark_setup_complete(store, complete=not missing_steps(store, include_recommended=False))
    console.print(
        "\n[bold green]آماده است.[/bold green]\n"
        "  bidpilot bot        → مدیریت کامل از داخل تلگرام (شامل تغییر لحن)\n"
        "  bidpilot preview    → دیدن یک پیشنهاد نمونه\n"
        "  bidpilot run        → اجرای دائمی رصد و ارسال\n"
    )
    return answered > 0


__all__ = [
    "KEEP",
    "STAGE_TITLES",
    "TONE_CHOICES",
    "Choice",
    "Question",
    "QuestionError",
    "all_questions",
    "apply_answer",
    "llm_questions",
    "marketplace_questions",
    "mark_preview_ok",
    "mark_setup_complete",
    "missing_steps",
    "mode_questions",
    "parse_answer",
    "preview_is_fresh",
    "preview_proof",
    "preview_proof_text",
    "profile_questions",
    "readiness",
    "run_terminal_wizard",
    "save_chat_id",
    "status_text",
    "style_questions",
    "telegram_questions",
    "tone_summary",
]
