"""Budget extraction from Persian job postings.

Ideal input looks like "بودجه: ۵۰۰,۰۰۰ تا ۱,۵۰۰,۰۰۰ تومان", but real pages also
contain "۵۰۰ تا ۱۵۰۰ تومن" (colloquial: each unit is *thousand* toman), "۱.۵ میلیون",
"حداکثر ۲ میلیون تومان", "توافقی", or a rial amount ("۵,۰۰۰,۰۰۰ ریال").
"""

from __future__ import annotations

import re

from ..models import Budget
from ..text import clean_text

_UNIT_MULTIPLIER: dict[str, int] = {
    "هزار": 1_000,
    "هزارتومان": 1_000,
    "هزار تومان": 1_000,
    "میلیون": 1_000_000,
    "ملیون": 1_000_000,
    "میلیارد": 1_000_000_000,
    "تومن": 1,
    "تومان": 1,
    "ریال": 1,  # rial handled separately (divided by 10)
}

_NEGOTIABLE_TOKENS = ("توافقی", "توافق", "قابل مذاکره", "negotiable", "مذاکره")

# captures a whole numeric token: ۵۰۰,۰۰۰ / 1.500.000 / 1500000 / 1.5
_NUMBER_RE = re.compile(r"(\d(?:[\d,.\u066c\u060c]*\d)?)\s*(هزار|میلیون|ملیون|میلیارد|تومن|تومان|ریال)?")

_THOUSANDS_SEP_RE = re.compile(r"(?<=\d)[,\u060c\u066c](?=\d{3}\b)")


def _to_number(raw: str) -> float | None:
    """Parse a number that may use '.' or ',' as a thousands or decimal separator."""
    raw = raw.strip()
    if not raw:
        return None
    # Persian/Arabic thousands separators (٬ U+066C, ، U+060C) are noise here
    raw = raw.replace("\u066c", ",").replace("\u060c", ",")
    try:
        # 1.500.000 or 1,500,000 -> thousands
        if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", raw):
            return float(raw.replace(".", "").replace(",", ""))
        # 1.5 -> decimal
        if re.fullmatch(r"\d+[.,]\d{1,2}", raw):
            return float(raw.replace(",", "."))
        return float(raw.replace(",", "").replace(".", ""))
    except ValueError:  # page counters, odd markup — never crash a scrape
        return None


def parse_budget(
    text: str | None,
    *,
    colloquial_thousands: bool = True,
    currency: str = "IRT",
) -> Budget | None:
    """Extract a normalized Budget from free-form Persian text.

    Args:
        text: raw text scraped from the posting.
        colloquial_thousands: when True, a bare "تومن/تومان" amount below 1,000 is
            read the Iranian way ("۵۰۰ تومن" == 500,000 toman). Set False for strict
            literal parsing.
    """
    if not text:
        return None
    cleaned = clean_text(text)
    lowered = cleaned.lower()

    raw_numbers: list[tuple[float, str | None, bool]] = []
    for match in _NUMBER_RE.finditer(cleaned):
        token = match.group(1)
        value = _to_number(token)
        if value is None:
            continue
        had_separator = any(ch in token for ch in (",", ".", "\u066c", "\u060c"))
        raw_numbers.append((value, match.group(2), had_separator))

    if not raw_numbers:
        negotiable = any(tok in lowered for tok in _NEGOTIABLE_TOKENS)
        return Budget(negotiable=negotiable, currency=currency, source_text=cleaned[:200])

    is_rial = "ریال" in lowered and "تومان" not in lowered and "تومن" not in lowered

    # Inherit the unit of the last number that carries one ("۵۰۰ تا ۱۵۰۰ تومن").
    inherited_unit = next((unit for _, unit, _ in reversed(raw_numbers) if unit), None)

    # Iranian colloquial shorthand: "۵۰۰ تا ۱۵۰۰ تومن" means 500k–1.5M toman.
    base_unit = inherited_unit or ""
    colloquial_scale = (
        colloquial_thousands
        and base_unit in ("", "تومن", "تومان")
        and all(not had_sep and value < 100_000 for value, _, had_sep in raw_numbers)
    )

    amounts: list[int] = []
    for value, unit, _had_sep in raw_numbers:
        multiplier = _UNIT_MULTIPLIER.get(unit or "", 1)
        if unit is None and inherited_unit:
            multiplier = _UNIT_MULTIPLIER.get(inherited_unit, 1)
        if colloquial_scale and multiplier == 1:
            multiplier = 1_000
        elif colloquial_thousands and multiplier == 1 and value < 1_000:
            multiplier = 1_000
        amount = int(round(value * multiplier))
        if is_rial:
            amount = amount // 10
        amounts.append(amount)

    amounts = [a for a in amounts if a > 0]
    if not amounts:
        return Budget(negotiable=True, currency=currency, source_text=cleaned[:200])

    low, high = min(amounts), max(amounts)
    # Guard against scraping noise such as "۵۰۰۰ پروژه ثبت شده".
    if high > 100 * 10**9:
        return Budget(negotiable=True, currency=currency, source_text=cleaned[:200])

    return Budget(min=low, max=high, currency=currency, source_text=cleaned[:200])


def format_toman(amount: int | None) -> str:
    if amount is None:
        return "—"
    if amount >= 1_000_000_000:
        return f"{amount / 1_000_000_000:g} میلیارد تومان"
    if amount >= 1_000_000:
        return f"{amount / 1_000_000:g} میلیون تومان"
    return f"{amount:,} تومان"
