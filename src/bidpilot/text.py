"""Persian/Arabic text normalization helpers.

Parsing Iranian marketplace pages means dealing with:
  * Persian digits (۰-۹) and Arabic-Indic digits (٠-٩)
  * Arabic yeh/kaf (ي/ك) instead of Persian (ی/ک)
  * Arabic/Persian thousands separators (٬ , .) and stray ZWNJ characters
  * HTML entities and non-breaking spaces

Everything that touches user-facing text goes through this module first.
"""

from __future__ import annotations

import html
import re
import unicodedata

PERSIAN_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
ARABIC_DIGITS = "٠١٢٣٤٥٦٧٨٩"
_ASCII_DIGITS = "0123456789"

_DIGIT_MAP = {ord(p): a for p, a in zip(PERSIAN_DIGITS, _ASCII_DIGITS, strict=False)}
_DIGIT_MAP.update({ord(a): b for a, b in zip(ARABIC_DIGITS, _ASCII_DIGITS, strict=False)})

_CHAR_MAP = {
    "\u064a": "\u06cc",  # ي -> ی
    "\u0649": "\u06cc",  # ى -> ی
    "\u0643": "\u06a9",  # ك -> ک
    "\u0629": "\u0647",  # ة -> ه
    "\u200f": "",  # RLM
    "\u200e": "",  # LRM
    "\u200b": "",  # ZWSP
    "\u00a0": " ",
    "\u2029": "\n",
    "\u066b": ".",  # Arabic decimal separator
    "\u00f7": "/",
}

_MULTISPACE_RE = re.compile(r"[ \t]+")
_NEWLINES_RE = re.compile(r"\n{3,}")
_PERSIAN_LETTER_RE = re.compile(r"[\u0600-\u06FF]")


def to_ascii_digits(text: str) -> str:
    """Convert Persian/Arabic digits to ASCII digits."""
    return text.translate(_DIGIT_MAP)


def clean_text(text: str | None) -> str:
    """Normalize a scraped HTML/text blob into comparable plain text."""
    if not text:
        return ""
    text = html.unescape(text)
    for src, dst in _CHAR_MAP.items():
        text = text.replace(src, dst)
    text = to_ascii_digits(text)
    text = unicodedata.normalize("NFC", text)
    text = _MULTISPACE_RE.sub(" ", text)
    text = _NEWLINES_RE.sub("\n\n", text)
    return text.strip()


def persian_ratio(text: str) -> float:
    """Share of characters that are Persian/Arabic letters (ignores spaces & punctuation)."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if _PERSIAN_LETTER_RE.match(c)) / len(letters)


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    """Truncate without breaking the middle of a word when possible."""
    if len(text) <= limit:
        return text
    cut = text[: limit - len(suffix)]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut + suffix


def has_any(text: str, needles: list[str] | tuple[str, ...]) -> bool:
    haystack = clean_text(text).lower()
    return any(clean_text(n).lower() in haystack for n in needles if n)


def to_persian_digits(text: str) -> str:
    """Convert ASCII digits to Persian digits (for user-facing proposal copy)."""
    return text.translate({ord(a): p for a, p in zip(_ASCII_DIGITS, PERSIAN_DIGITS, strict=False)})


#: how many days each duration unit means
_DURATION_UNITS = {
    "روز": 1,
    "روزه": 1,
    "روزه‌ای": 1,
    "day": 1,
    "days": 1,
    "هفته": 7,
    "هفته‌ای": 7,
    "week": 7,
    "weeks": 7,
    "ماه": 30,
    "ماهه": 30,
    "ماهه‌ای": 30,
    "month": 30,
    "months": 30,
}


def parse_days(text: str | None) -> int | None:
    """Read a delivery time out of free text ("۱۰ روز"، "۲ هفته"، "1 ماه").

    Returns ``None`` when the text has no usable duration — the caller then keeps
    its own default instead of guessing for the employer.
    """
    if not text:
        return None
    cleaned = to_ascii_digits(str(text)).lower()
    match = re.search(
        r"(\d{1,3})\s*(?:تا\s*\d{1,3}\s*)?"
        r"(روز|روزه|روزه‌ای|هفته|هفته‌ای|ماه|ماهه|ماهه‌ای|days?|weeks?|months?)",
        cleaned,
    )
    if match:
        number, unit = int(match.group(1)), match.group(2)
        days = number * _DURATION_UNITS.get(unit, 1)
        return days if 0 < days <= 365 else None
    return None


def count_emoji(text: str) -> int:
    """Rough emoji counter (works for the ranges clients actually use)."""
    import unicodedata

    return sum(1 for c in text if unicodedata.category(c) in ("So", "Sk") and ord(c) > 0x2100)


def words_fa(text: str) -> list[str]:
    return [w for w in re.split(r"[\s،؛:.,!؟()\[\]{}«»\"'\-–—]+", text) if w]
