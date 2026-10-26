"""HTML -> Project parsing shared by every marketplace adapter.

The card markup of each site is described in that site's ``config.yaml``, so a
markup change is a config edit, not a code change. Every selector list is tried
in order; the first selector that matches something wins.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from ..models import Budget, Project
from ..pricing.budget import parse_budget
from ..text import clean_text, has_any, parse_days

log = logging.getLogger(__name__)


def _budget_debug(budget: Budget | None) -> str:
    if budget is None:
        return "none"
    return (
        f"known={budget.is_known} min={budget.min} max={budget.max} "
        f"negotiable={budget.negotiable} currency={budget.currency} "
        f"source_text={budget.source_text!r}"
    )


DEFAULT_ID_REGEX = r"/projects?/(\d+)"
#: tried in order when a site has no selectors yet — lets an unknown site be
#: read without any config, which is what ``bidpilot inspect`` prints.
GENERIC_ID_REGEXES = (r"/\d{3,}", r"/[\w-]+/\d{2,}")
_PERSIAN_MONTHS = {
    "فروردین": 1,
    "اردیبهشت": 2,
    "خرداد": 3,
    "تیر": 4,
    "مرداد": 5,
    "شهریور": 6,
    "مهر": 7,
    "آبان": 8,
    "آذر": 9,
    "دی": 10,
    "بهمن": 11,
    "اسفند": 12,
}


def _first(card: Tag | BeautifulSoup, selectors: list[str] | None) -> Tag | None:
    for selector in selectors or []:
        try:
            found = card.select_one(selector)
        except Exception:  # invalid selector should not kill the run
            continue
        if found is not None:
            return found
    return None


def _all(card: Tag | BeautifulSoup, selectors: list[str] | None) -> list[Tag]:
    for selector in selectors or []:
        try:
            found = card.select(selector)
        except Exception:
            continue
        if found:
            return found
    return []


def _text(card: Tag | BeautifulSoup, selectors: list[str] | None) -> str:
    # A selector can match an empty wrapper before a later selector matches the
    # actual text node. Keep trying until a non-empty value is found.
    for selector in selectors or []:
        try:
            nodes = card.select(selector)
        except Exception:
            continue
        for node in nodes:
            value = clean_text(node.get_text(" ", strip=True))
            if value:
                return value
    return ""


_BUDGET_NODE_SELECTORS = (
    "[data-budget]",
    "[data-price]",
    "[data-amount]",
    "[class*='budget']",
    "[class*='price']",
    "[class*='amount']",
    "[id*='budget']",
    "[id*='price']",
    "[aria-label*='بودجه']",
    "[title*='بودجه']",
)
_SCOPED_BUDGET_LABELS = (
    r"سقف\s+بودجه",
    r"بودجه(?:[\u200c]?[یٔ])?(?:\s+پروژه)?",
    r"مبلغ\s+(?:پروژه|کل(?:\s+پروژه)?)",
    r"حق[\u200c-]?الزحمه(?:\s+پروژه)?",
    r"هزینه\s+(?:پروژه|کل(?:\s+پروژه)?)",
    r"قیمت\s+پروژه",
    r"project\s+budget",
    r"budget(?:\s+amount)?",
)
_SCOPED_BUDGET_REGEXES = tuple(rf"{label}\s*[:：]?\s*([^\n]{{2,100}})" for label in _SCOPED_BUDGET_LABELS)

# Match an explicitly labeled range even when a marketplace splits it across
# visual lines, e.g. "بودجه 25,000 تومان\n تا\n 500,000 تومان".
_BUDGET_RANGE_VALUE_RE = re.compile(
    r"(?P<range>\d(?:[\d,.\u066c\u060c]*\d)?\s*"
    r"(?:(?:هزار|میلیون|ملیون|میلیارد)\s*)?"
    r"(?:تومان|تومن|ریال)?\s*"
    r"(?:تا|الی|[-–—])\s*"
    r"\d(?:[\d,.\u066c\u060c]*\d)?\s*"
    r"(?:(?:هزار|میلیون|ملیون|میلیارد)\s*)?"
    r"(?:تومان|تومن|ریال)?)",
    re.IGNORECASE,
)

_DESCRIPTION_END_MARKERS = (
    "مهارت‌های مورد نیاز",
    "مهارت های مورد نیاز",
    "مهارت‌های موردنیاز",
    "مهارت های موردنیاز",
    "مهارت‌ها:",
    "مهارت ها:",
    "سرویس‌های مرتبط",
    "سرویس های مرتبط",
    "خدمات مرتبط",
    "توضیحات بیشتر",
    "فریلنسرهایی که در این پروژه پیشنهاد ارسال کرده‌اند",
    "آخرین پروژه‌های",
    "آخرین پروژه های",
    "فرصت انتخاب پیشنهادها",
    "مشاهده‌ی پروژه",
    "مشاهده پروژه",
)


def _labeled_range_budget(text: str, value_start: int) -> Budget | None:
    """Parse a nearby explicit budget range before single-amount fallbacks truncate it."""
    value_tail = text[value_start : value_start + 220]
    match = _BUDGET_RANGE_VALUE_RE.search(value_tail)
    if not match:
        return None
    evidence = match.group("range")
    # Require monetary context so dates, durations and other numeric ranges
    # cannot be promoted to budgets just because they follow a label.
    if not re.search(r"تومان|تومن|ریال|هزار|میلیون|ملیون|میلیارد", evidence):
        return None
    budget = parse_budget_text(evidence)
    if (
        budget is None
        or not budget.is_known
        or budget.min is None
        or budget.max is None
        or budget.min == budget.max
    ):
        return None
    return budget


def _find_employer_deadline(root: Tag | BeautifulSoup) -> str:
    """Extract an explicitly labeled project duration, never use a profile default here."""
    labels = (
        r"مهلت\s+برای\s+انجام",
        r"مدت\s+زمان(?:\s+انجام)?(?:\s+پروژه)?",
        r"زمان\s+تحویل(?:\s+پروژه)?",
        r"delivery\s+time",
        r"project\s+duration",
        r"مهلت",
    )
    tags = ["p", "li", "span", "small", "label", "dt", "dd", "td", "strong", "b", "div", "section"]
    for node in root.find_all(tags):
        text = clean_text(node.get_text(" ", strip=True))
        if not text or len(text) > 220:
            continue
        for label in labels:
            match = re.search(label, text, re.IGNORECASE)
            if not match:
                continue
            tail = text[match.end() :].strip(" :：-")
            days = parse_days(tail)
            if days is not None:
                return f"{days} روز"
    return ""


def _node_text_candidates(node: Tag) -> list[str]:
    """Read visible text plus a small allowlist of non-sensitive budget attributes."""
    values: list[str] = []
    visible = clean_text(node.get_text(" ", strip=True))
    if visible:
        values.append(visible)
    for attr in ("data-budget", "data-price", "data-amount", "aria-label", "title", "content"):
        value = clean_text(str(node.get(attr) or ""))
        if value and value not in values:
            values.append(value)
    return values


def _find_labeled_budget(text: str, patterns: list[str] | tuple[str, ...] | None = None) -> Budget | None:
    """Parse a budget only when a budget-specific label and its value share a short block."""
    cleaned = clean_text(text or "")
    if not cleaned or len(cleaned) > 500 or _is_non_project_budget_evidence(cleaned):
        return None
    for pattern in patterns or _SCOPED_BUDGET_REGEXES:
        try:
            match = re.search(pattern, cleaned, re.I)
        except re.error:
            continue
        if not match:
            continue
        value_start = match.start(1) if match.lastindex else match.end()
        range_budget = _labeled_range_budget(cleaned, value_start)
        if range_budget is not None and range_budget.is_known:
            return range_budget
        candidate = match.group(1) if match.groups() else match.group(0)
        # Never scan beyond the first amount unit in the same block when only a
        # single amount is present.
        unit = re.search(r"تومان|تومن|ریال|هزار|میلیون|میلیارد", candidate)
        if unit:
            candidate = candidate[: unit.end()]
        elif any(token in candidate for token in ("توافق", "مذاکره", "negotiable")):
            return Budget(negotiable=True, source_text=candidate[:200])
        budget = parse_budget_text(candidate)
        if budget is not None and budget.is_known:
            return budget
    return None


def _find_budget_in_markup(
    root: Tag | BeautifulSoup,
    selectors: list[str] | None = None,
    patterns: list[str] | tuple[str, ...] | None = None,
) -> tuple[Budget | None, str, str]:
    """Find budget evidence inside a project/card scope, never across the whole page."""
    seen: set[int] = set()

    # Prefer the configured budget selectors. Their context is strong enough to
    # accept an unlabeled numeric value, including values stored in data-* attrs.
    for selector in selectors or []:
        try:
            nodes = root.select(selector)
        except Exception:
            continue
        for node in nodes:
            seen.add(id(node))
            for evidence in _node_text_candidates(node):
                if len(evidence) > 500 or _is_non_project_budget_evidence(evidence):
                    continue
                budget = parse_budget_text(evidence)
                if budget is not None and budget.is_known:
                    return budget, evidence, "configured_selector"
                budget = _find_labeled_budget(evidence, patterns)
                if budget is not None and budget.is_known:
                    return budget, evidence, "configured_selector_label"

    # Then inspect budget-shaped DOM nodes for a visible value or a data attribute.
    for selector in _BUDGET_NODE_SELECTORS:
        try:
            nodes = root.select(selector)
        except Exception:
            continue
        for node in nodes:
            if id(node) in seen:
                continue
            seen.add(id(node))
            for evidence in _node_text_candidates(node):
                if len(evidence) > 500:
                    continue
                budget = parse_budget_text(evidence)
                if budget is not None and budget.is_known:
                    return budget, evidence, "budget_node"
                budget = _find_labeled_budget(evidence, patterns)
                if budget is not None and budget.is_known:
                    return budget, evidence, "budget_node_label"

    # Last, only accept an explicit budget label and value in the same short
    # text block. Do not run the old amount regex over flattened body text: that
    # can combine an ordinary sentence with an unrelated amount below it.
    for node in root.find_all(
        ["dt", "dd", "th", "td", "li", "p", "span", "small", "label", "div", "section"]
    ):
        if id(node) in seen:
            continue
        evidence = clean_text(node.get_text(" ", strip=True))
        if not evidence or len(evidence) > 180:
            continue
        budget = _find_labeled_budget(evidence, patterns)
        if budget is not None and budget.is_known:
            return budget, evidence, "labeled_text_block"
    return None, "", "not_found"


def _budget_node_summaries(
    root: Tag | BeautifulSoup,
    selectors: list[str] | None = None,
    *,
    limit: int = 20,
) -> list[str]:
    """Compact markup evidence for debugging; deliberately omits arbitrary attributes."""
    summaries: list[str] = []
    seen: set[int] = set()
    for selector in [*(selectors or []), *_BUDGET_NODE_SELECTORS]:
        try:
            nodes = root.select(selector)
        except Exception:
            continue
        for node in nodes:
            if id(node) in seen:
                continue
            seen.add(id(node))
            classes = node.get("class") or []
            if not isinstance(classes, list):
                classes = [str(classes)]
            attrs = {
                key: str(node.get(key))[:120]
                for key in ("data-budget", "data-price", "data-amount", "aria-label", "title", "content")
                if node.get(key) is not None
            }
            text = clean_text(node.get_text(" ", strip=True))[:180]
            if not (text or attrs):
                continue
            summaries.append(
                f"<{node.name} id={str(node.get('id') or '')[:80]!r} "
                f"class={classes[:6]!r} text={text!r} attrs={attrs!r}>"
            )
            if len(summaries) >= limit:
                return summaries
    return summaries


def _project_scope(soup: BeautifulSoup, title: str) -> Tag | None:
    """Return the largest ancestor around the matching project heading before related cards begin."""
    normalized_title = clean_text(title).replace("\u200c", "").casefold()
    if not normalized_title:
        return None

    heading: Tag | None = None
    for candidate in soup.find_all(["h1", "h2", "h3", "h4"]):
        text = clean_text(candidate.get_text(" ", strip=True)).replace("\u200c", "").casefold()
        if text == normalized_title or (normalized_title in text and len(text) <= len(normalized_title) + 30):
            heading = candidate
            break
    if heading is None:
        return None

    best: Tag | None = heading.parent if isinstance(heading.parent, Tag) else heading
    current: Any = heading.parent
    project_link_pattern = re.compile(r"/projects?/(\d+)(?:/|$)", re.I)
    while isinstance(current, Tag):
        text_len = len(clean_text(current.get_text(" ", strip=True)))
        if text_len > 40_000:
            break
        ids = {
            match.group(1)
            for anchor in current.find_all("a", href=True)
            if (match := project_link_pattern.search(str(anchor.get("href") or "")))
        }
        if len(ids) > 1:
            break
        if text_len >= len(normalized_title) + 30:
            best = current
        current = current.parent
    return best


_MONEY_EXPRESSION_RE = re.compile(
    r"(?<!\w)"
    r"(?P<amount>\d(?:[\d,.\u066c\u060c]*\d)?\s*"
    r"(?:(?:هزار|میلیون|ملیون|میلیارد)\s*)?"
    r"(?:تومان|تومن|ریال)?"
    r"(?:\s*(?:تا|الی|[-–—])\s*\d(?:[\d,.\u066c\u060c]*\d)?\s*"
    r"(?:(?:هزار|میلیون|ملیون|میلیارد)\s*)?"
    r"(?:تومان|تومن|ریال)?)?"
    r")",
    re.IGNORECASE,
)
_BUDGET_PREFIX_RE = re.compile(
    r"(?:بودجه(?:[\u200c]?[یٔ])?|سقف\s*بودجه|مبلغ|حق[\u200c-]?الزحمه|"
    r"هزینه|قیمت|برآورد(?:ی)?|تخمینی|تقریبی|حداقل|حداکثر|تصریح|از|حدود)"
    r"\s*[:：\-]?\s*",
    re.IGNORECASE,
)
_BUDGET_SUFFIX_RE = re.compile(r"[\d\s:/\-–—آ-ی]*")


def _normalized_marker(value: str) -> str:
    return clean_text(value).replace("\u200c", " ").replace("ي", "ی").replace("ك", "ک").casefold()


_NON_PROJECT_BUDGET_MARKERS = (
    "عایدی شما",
    "مبلغ عایدی",
    "دریافت کنی",
    "ارتقاء حساب کاربری",
    "your earnings",
)


def _is_non_project_budget_evidence(text: str | None) -> bool:
    """Reject the logged-in freelancer's earnings widget as employer project budget."""
    normalized = _normalized_marker(text or "")
    return any(_normalized_marker(marker) in normalized for marker in _NON_PROJECT_BUDGET_MARKERS)


def _unique_clean_values(values) -> list[str]:
    """Clean and deduplicate site tags while preserving their original order."""
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = clean_text(str(raw or ""))
        key = re.sub(r"\s+", " ", _normalized_marker(value)).strip()
        if value and key and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _find_project_header_budget(
    soup: BeautifulSoup,
    title: str,
    *,
    end_markers: list[str] | tuple[str, ...] | None = None,
    diagnostics: bool = False,
) -> tuple[Budget | None, str, str, str]:
    """Find a budget amount in short metadata nodes between the project title and skills section.

    Ponisha renders the budget as an unclassed text node adjacent to the description.
    Keep the search bounded before the skills heading so recommendation-card prices
    lower on the page can never become this project's budget.
    """
    normalized_title = _normalized_marker(title)
    headings = soup.find_all(["h1", "h2", "h3", "h4"])
    heading: Tag | None = None
    for candidate in headings:
        normalized = _normalized_marker(candidate.get_text(" ", strip=True))
        if normalized == normalized_title or (
            normalized_title
            and normalized_title in normalized
            and len(normalized) <= len(normalized_title) + 30
        ):
            heading = candidate
            break

    if heading is None:
        if diagnostics:
            log.info("[PARSER][HEADER-BUDGET] title=%r result=no_project_heading", title)
        return None, "", "no_project_heading", ""

    node_tags = [
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "p",
        "div",
        "span",
        "li",
        "dt",
        "dd",
        "td",
        "label",
        "small",
        "strong",
        "b",
        "time",
    ]
    ordered = list(soup.find_all(node_tags))
    try:
        title_index = ordered.index(heading)
    except ValueError:
        if diagnostics:
            log.info("[PARSER][HEADER-BUDGET] title=%r result=title_not_in_dom_order", title)
        return None, "", "no_project_heading", ""

    markers = [
        _normalized_marker(marker)
        for marker in (end_markers or _DESCRIPTION_END_MARKERS)
        if _normalized_marker(marker)
    ]
    end_index = len(ordered)
    end_found = False
    for index in range(title_index + 1, len(ordered)):
        node = ordered[index]
        if node.name not in {"h2", "h3", "h4", "h5"}:
            continue
        heading_text = _normalized_marker(node.get_text(" ", strip=True))
        if any(marker in heading_text for marker in markers):
            end_index = index
            end_found = True
            break

    # Without a recognizable section boundary, this fallback cannot distinguish
    # the current project's metadata from recommended-project cards below it.
    if not end_found:
        if diagnostics:
            log.info(
                "[PARSER][HEADER-BUDGET] title=%r result=no_section_boundary",
                title,
            )
        return None, "", "not_found", ""

    money_nodes: list[str] = []
    checked = 0
    # Only inspect the DOM between this title and the first section heading below it.
    for node in ordered[title_index + 1 : end_index]:
        text = clean_text(node.get_text(" ", strip=True))
        if len(text) > 180:
            # A parent container can include the full description and other sections.
            # Data attributes are still eligible, but its combined visible text is not.
            visible_candidates: list[str] = []
        else:
            has_nested_text = node.find(node_tags) is not None
            visible_candidates = [] if has_nested_text else ([text] if text else [])

        attrs = [
            clean_text(str(node.get(attr) or ""))
            for attr in ("data-budget", "data-price", "data-amount", "aria-label", "title", "content")
            if node.get(attr) is not None
        ]
        candidates = [*visible_candidates, *attrs]
        for evidence in candidates:
            if not evidence or len(evidence) > 180 or _is_non_project_budget_evidence(evidence):
                continue
            if not re.search(r"تومان|تومن|ریال", evidence):
                continue

            checked += 1
            money_nodes.append(
                f"<{node.name} class={(node.get('class') or [])[:4]!r} text={evidence[:140]!r}>"
            )
            matches = list(_MONEY_EXPRESSION_RE.finditer(evidence))
            for match in reversed(matches):
                prefix = evidence[: match.start()].strip()
                suffix = evidence[match.end() :].strip()
                # Accept an amount-only metadata node, optionally preceded by a
                # short label such as the "تصریح" marker seen in the supplied log.
                # Reject full descriptive sentences and unrelated numeric prose.
                if len(prefix) > 20 or len(suffix) > 36:
                    continue
                if re.search(r"[.!؟؛،\n]", prefix) or re.search(r"[.!؟؛،\n]", suffix):
                    continue
                if prefix and not _BUDGET_PREFIX_RE.fullmatch(prefix):
                    continue
                if suffix and not _BUDGET_SUFFIX_RE.fullmatch(suffix):
                    continue

                amount_text = clean_text(match.group("amount"))
                budget = parse_budget_text(amount_text)
                if budget is None or not budget.is_known:
                    continue
                if diagnostics:
                    log.info(
                        "[PARSER][HEADER-BUDGET] title=%r result=found source=project_header_money_node "
                        "amount=%r evidence=%r candidates_checked=%s",
                        title,
                        amount_text,
                        evidence,
                        checked,
                    )
                return budget, amount_text, "project_header_money_node", evidence

    if diagnostics:
        log.info(
            "[PARSER][HEADER-BUDGET] title=%r result=not_found candidates_checked=%s money_nodes=%r",
            title,
            checked,
            money_nodes[:8],
        )
    return None, "", "not_found", ""


def _extract_between_markers(
    text: str,
    start_markers: list[str] | tuple[str, ...] | None = None,
    end_markers: list[str] | tuple[str, ...] | None = None,
    *,
    max_chars: int = 12_000,
) -> str:
    """Extract a bounded text section using site-specific heading markers."""
    cleaned = clean_text(text or "")
    if not cleaned:
        return ""
    start = 0
    starts = [clean_text(marker) for marker in (start_markers or []) if clean_text(marker)]
    if starts:
        matches = [(cleaned.find(marker), marker) for marker in starts]
        matches = [(pos, marker) for pos, marker in matches if pos >= 0]
        if not matches:
            return ""
        pos, marker = min(matches)
        start = pos + len(marker)
    end = len(cleaned)
    markers = [
        clean_text(marker) for marker in (end_markers or _DESCRIPTION_END_MARKERS) if clean_text(marker)
    ]
    for marker in markers:
        pos = cleaned.find(marker, start)
        if pos >= 0:
            end = min(end, pos)
    result = cleaned[start:end].strip(" \t:-–—|")
    if len(result) < 20:
        return ""
    return result[:max_chars]


def _attr(card: Tag | BeautifulSoup, selectors: list[str] | None, attr: str, default: str = "") -> str:
    node = _first(card, selectors)
    if node is None:
        return default
    return str(node.get(attr) or default)


def extract_external_id(url: str, id_regex: str = DEFAULT_ID_REGEX) -> str | None:
    match = re.search(id_regex, url)
    return match.group(1) if match else None


def fallback_id(*parts: str) -> str:
    return "h" + hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


#: Labels an employer's budget carries on Iranian marketplaces. The capture
#: group is the short text *after* the label, which is then parsed normally.
BUDGET_LABELS = (
    r"بودجه",
    r"بودجهٔ",
    r"بودجه‌ی",
    r"مبلغ\s+(?:پروژه|کل(?:\s+پروژه)?)",
    r"حق‌?الزحمه",
    r"حقوق",
    r"هزینه",
    r"قیمت",
    r"پرداختی",
    r"سقف بودجه",
)
#: generic "labeled amount" patterns, used when a site has no budget selector
DEFAULT_BUDGET_REGEXES: tuple[str, ...] = tuple(
    rf"{label}\s*[:：]?\s*([^\n]{{2,60}})" for label in BUDGET_LABELS
)

#: amounts in this range are plausible bids (1,000 toman … 1 billion toman)
_MIN_PLAUSIBLE = 1_000
_MAX_PLAUSIBLE = 1_000_000_000


def _parse_amount(text: str) -> Budget | None:
    return parse_budget(text)


def parse_budget_text(text: str | None) -> Budget | None:
    """Parse an amount and keep only plausible values (no page counters)."""
    budget = parse_budget(text)
    if budget is None or not budget.is_known:
        return budget
    low, high = budget.lower or 0, budget.upper or 0
    if low < _MIN_PLAUSIBLE or high > _MAX_PLAUSIBLE:
        return Budget(negotiable=True, source_text=(text or "")[:200])
    return budget


def find_budget(text: str | None, patterns: list[str] | tuple[str, ...] | None = None) -> Budget | None:
    """Look for a *labeled* budget inside free text ("بودجه: …", "حق‌الزحمه …").

    Falls back to a bare amount only when it carries a currency unit, so page
    counters ("۱٬۲۳۴ پروژه") are not mistaken for money.
    """
    cleaned = clean_text(text or "")
    if not cleaned or _is_non_project_budget_evidence(cleaned):
        return None

    for pattern in patterns or DEFAULT_BUDGET_REGEXES:
        try:
            match = re.search(pattern, cleaned, re.I)
        except re.error:
            continue
        if not match:
            continue
        candidate_start = match.start(1) if match.lastindex else match.start()
        candidate_end = match.end(1) if match.lastindex else match.end()
        candidate = match.group(1) if match.lastindex else match.group(0)
        surrounding = cleaned[max(0, match.start() - 36) : candidate_end]
        if _is_non_project_budget_evidence(surrounding) or _is_non_project_budget_evidence(candidate):
            continue
        value_start = candidate_start
        range_budget = _labeled_range_budget(cleaned, value_start)
        if range_budget is not None and range_budget.is_known:
            return range_budget
        candidate = match.group(1) if match.groups() else match.group(0)
        # A label such as «بودجه: توافقی» is followed by other card text; only a
        # candidate that names a currency may be read as an amount, and nothing
        # after that unit belongs to the budget.
        unit = re.search(r"تومان|تومن|ریال|هزار|میلیون|میلیارد", candidate)
        if unit is None:
            if any(token in candidate for token in ("توافق", "مذاکره", "negotiable")):
                return Budget(negotiable=True, source_text=candidate[:200])
            continue
        candidate = candidate[: unit.end()]
        budget = parse_budget_text(candidate)
        if budget is not None and budget.is_known:
            return budget

    for match in re.finditer(r"([^\n]{0,30}?)(\d[\d,.٬،]*)\s*(تومان|تومن|ریال|هزار تومان|میلیون)", cleaned):
        if _is_non_project_budget_evidence(match.group(1)):
            continue
        budget = parse_budget_text(match.group(0))
        if budget is not None and budget.is_known:
            return budget
    return None


#: Persian marketplaces spell small numbers out («چهار ساعت پیش», «دو روز پیش»),
#: which the old digit-only regex never matched -> published_at was always None
#: and the newest-first sort silently degraded to page order.
_PERSIAN_WORD_NUMBERS: dict[str, int] = {
    "یک": 1,
    "دو": 2,
    "سه": 3,
    "چهار": 4,
    "پنج": 5,
    "شش": 6,
    "هفت": 7,
    "هشت": 8,
    "نه": 9,
    "ده": 10,
    "یازده": 11,
    "دوازده": 12,
    "سیزده": 13,
    "چهارده": 14,
    "پانزده": 15,
    "شانزده": 16,
    "هفده": 17,
    "هجده": 18,
    "نوزده": 19,
    "بیست": 20,
    "سی": 30,
}


def parse_published(text: str) -> datetime | None:
    """Best-effort parser for Persian relative/absolute timestamps ("۳ ساعت پیش")."""
    if not text:
        return None
    t = clean_text(text).replace("‌", " ")
    # timezone-aware UTC like the rest of the codebase; a naive value here
    # raises TypeError the day an aware one joins the same sort.
    from datetime import timedelta, timezone

    now = datetime.now(timezone.utc)
    if any(marker in t for marker in ("لحظاتی پیش", "دقایقی پیش", "هم‌اکنون", "هم اکنون", "الان", "تازه")):
        return now
    if "دیروز" in t:
        return now - timedelta(days=1)
    if "پریروز" in t:
        return now - timedelta(days=2)
    if "ساعتی پیش" in t:
        return now - timedelta(hours=1)
    word_numbers = "|".join(sorted(_PERSIAN_WORD_NUMBERS, key=len, reverse=True))
    rel = re.search(rf"(\d+|{word_numbers})\s*(دقیقه|ساعت|روز|هفته|ماه)\s*پیش", t)
    if rel:
        raw_value, unit = rel.group(1), rel.group(2)
        value = int(raw_value) if raw_value.isdigit() else _PERSIAN_WORD_NUMBERS[raw_value]
        minutes = {"دقیقه": 1, "ساعت": 60, "روز": 1440, "هفته": 10080, "ماه": 43200}[unit]
        return now - timedelta(minutes=value * minutes)
    try:
        import jdatetime

        m = re.search(r"(\d{1,4})[/-](\d{1,2})[/-](\d{1,2})", t)
        if m and len(m.group(1)) == 4 and int(m.group(1)) > 1300:
            jalali = jdatetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            gregorian = jalali.togregorian()  # a plain date, not a datetime
            return datetime(gregorian.year, gregorian.month, gregorian.day, tzinfo=timezone.utc)
    except Exception:
        pass
    return None


def parse_project_card(
    card: Tag,
    settings: dict[str, Any],
    source_name: str = "parscoders",
    base_url: str = "",
) -> Project | None:
    fields: dict[str, list[str]] = settings.get("fields", {}) or {}
    anchor = _first(card, fields.get("link")) or _first(card, fields.get("title"))
    link = _attr(card, fields.get("link") or fields.get("title"), "href")
    title = _text(card, fields.get("title")) or _attr(card, fields.get("link"), "title")
    if not title and isinstance(anchor, Tag):
        # no selector matched: the anchor/heading text is the card's title
        title = clean_text(anchor.get_text(" ", strip=True))
    if isinstance(card, Tag):
        generic_anchor = card.find("a", href=True)
        if generic_anchor is not None:
            if not link:
                link = str(generic_anchor.get("href") or "")
            if not title:
                title = clean_text(generic_anchor.get_text(" ", strip=True))
        if not title:
            heading = card.find(["h1", "h2", "h3", "h4"])
            title = clean_text(heading.get_text(" ", strip=True)) if heading else ""
    link = urljoin(base_url + "/", link)

    external_id = extract_external_id(link, settings.get("id_regex", DEFAULT_ID_REGEX)) or fallback_id(
        link, title
    )
    if not title and not link:
        return None

    budget_selector_text = _text(card, fields.get("budget"))
    budget_text = budget_selector_text
    budget = _parse_amount(budget_text)
    budget_origin = "configured_selector" if budget is not None and budget.is_known else "not_found"
    if budget is None or not budget.is_known:
        found, evidence, origin = _find_budget_in_markup(
            card, fields.get("budget"), settings.get("budget_regex")
        )
        if found is not None and found.is_known:
            budget = found
            budget_text = evidence or found.source_text or found.as_text()
            budget_origin = origin

    skills = _unique_clean_values(s.get_text(" ", strip=True) for s in _all(card, fields.get("skills")))
    description = _text(card, fields.get("description"))
    card_text = clean_text(card.get_text(" ", strip=True)) if isinstance(card, Tag) else ""
    if not description:
        description = _extract_between_markers(
            card_text[card_text.find(title) + len(title) :] if title and title in card_text else "",
            end_markers=settings.get("description_end_markers"),
        )

    # Per-card diagnostics are DEBUG by default: 25 cards x several verbose
    # INFO lines per cycle made real logs megabytes of percent-encoded URLs
    # and hid the handful of lines that actually matter. The
    # ``log_extraction_details`` diagnostics flag promotes them back to INFO.
    log.log(
        logging.INFO if settings.get("_diagnostics") else logging.DEBUG,
        "[PARSER][CARD] source=%s id=%s title=%r url=%s budget_selector_text=%r "
        "budget_text_used=%r budget_origin=%s budget_final={%s} skills=%r category=%r employer=%r",
        source_name,
        external_id,
        title,
        link,
        budget_selector_text,
        budget_text,
        budget_origin,
        _budget_debug(budget),
        [s for s in skills if s][:12],
        _text(card, fields.get("category")) or None,
        _text(card, fields.get("employer")) or None,
    )
    if settings.get("_diagnostics"):
        log.info(
            "[PARSER][CARD-STRUCTURE] source=%s id=%s tag=%s id_attr=%r class=%r "
            "project_links=%r text_chars=%s",
            source_name,
            external_id,
            card.name,
            card.get("id"),
            (card.get("class") or [])[:8],
            sorted(_project_link_ids(card)),
            len(card_text),
        )
        log.info(
            "[PARSER][BUDGET-NODES] source=%s id=%s scope=card nodes=%r",
            source_name,
            external_id,
            _budget_node_summaries(card, fields.get("budget")),
        )
        log.info(
            "[PARSER][CARD-TEXT] source=%s id=%s chars=%s text=%s",
            source_name,
            external_id,
            len(card_text),
            card_text[:8_000],
        )

    return Project(
        source=source_name,
        external_id=external_id,
        title=title or "(بدون عنوان)",
        url=link,
        description=description,
        budget=budget,
        skills=[s for s in skills if s][:12],
        category=_text(card, fields.get("category")) or None,
        employer=_text(card, fields.get("employer")) or None,
        # Sites like ParsCoders print «چهار ساعت پیش» as bare card text with no
        # date class, so fall back to scanning the card text itself.
        published_at=parse_published(_text(card, fields.get("published_at")) or card_text),
        raw={"budget_text": budget_text},
    )


def _project_link_ids(root: Tag, pattern: str = DEFAULT_ID_REGEX) -> set[str]:
    """Distinct project IDs under a node, excluding duplicate CTA/title links."""
    try:
        matcher = re.compile(pattern)
    except re.error:
        matcher = re.compile(DEFAULT_ID_REGEX)
    ids: set[str] = set()
    for anchor in root.find_all("a", href=True):
        href = str(anchor.get("href") or "")
        match = matcher.search(href)
        if match:
            ids.add(match.group(1) if match.groups() else match.group(0))
    return ids


def fallback_cards(soup: BeautifulSoup, settings: dict[str, Any]) -> list[Tag]:
    """Content-based fallback: find repeated project links without any selector.

    Tried in order: the site's ``id_regex``, then generic numeric URLs. A page
    only counts as a listing when at least three distinct anchors match, so a
    stray "see project" link in a menu never invents a project.
    """
    patterns = [settings.get("id_regex") or DEFAULT_ID_REGEX, *GENERIC_ID_REGEXES]
    for pattern in patterns:
        try:
            matcher = re.compile(str(pattern))
        except re.error:
            continue
        cards: list[Tag] = []
        seen: set[str] = set()
        for anchor in soup.find_all("a", href=True):
            href = str(anchor["href"])
            if not matcher.search(href) or href in seen:
                continue
            seen.add(href)

            # The nearest div often contains only the title while budget,
            # skills and description live in sibling nodes. Prefer the best
            # ancestor that contains this project link and no other project IDs.
            ancestors = [
                parent
                for parent in anchor.find_parents(["article", "li", "section", "div"])
                if isinstance(parent, Tag)
            ]
            candidates: list[tuple[int, int, Tag]] = []
            for distance, parent in enumerate(ancestors):
                ids = _project_link_ids(parent, pattern)
                if len(ids) != 1:
                    continue
                text = clean_text(parent.get_text(" ", strip=True))
                if len(text) < 20 or len(text) > 12_000:
                    continue
                attrs = " ".join(
                    [str(parent.get("id") or ""), *[str(c) for c in (parent.get("class") or [])]]
                ).casefold()
                score = 0
                if re.search(r"project|card|item|job|task|result|listing", attrs):
                    score += 8
                if parent.select_one(
                    "[data-budget], [data-price], [data-amount], "
                    "[class*='budget'], [class*='price'], [class*='amount']"
                ):
                    score += 10
                if re.search(r"بودجه|حق.?الزحمه|تومان|تومن|میلیون|میلیارد", text):
                    score += 6
                if re.search(r"مهارت|skills?|category|دسته.?بندی", text, re.I):
                    score += 2
                # More complete cards win, while distance breaks ties in favor
                # of the closest ancestor. All candidates remain project-local.
                score += min(len(text) // 120, 8)
                candidates.append((score, -distance, parent))

            if candidates:
                parent = max(candidates, key=lambda item: (item[0], item[1]))[2]
            else:
                parent = anchor
            if parent not in cards:
                cards.append(parent)
        if len(cards) >= 3:
            return cards
    return []


def parse_list_page(
    html: str,
    settings: dict[str, Any],
    source_name: str = "parscoders",
    base_url: str = "",
) -> list[Project]:
    soup = BeautifulSoup(html, "lxml")
    cards: list[Tag] = []
    matched_selector = ""
    for selector in settings.get("card_selectors", []) or []:
        try:
            cards = [c for c in soup.select(selector) if isinstance(c, Tag)]
        except Exception as exc:
            log.warning("[PARSER][LIST] invalid card selector=%r error=%s", selector, exc)
            cards = []
        if cards:
            matched_selector = selector
            break

    used_fallback = False
    if not cards:
        cards = fallback_cards(soup, settings)
        used_fallback = bool(cards)

    log.info(
        "[PARSER][LIST] source=%s page_chars=%s card_count=%s selector=%r fallback_cards=%s",
        source_name,
        len(html or ""),
        len(cards),
        matched_selector or None,
        used_fallback,
    )
    if settings.get("_diagnostics"):
        page_text = clean_text(soup.get_text(" ", strip=True))
        log.info(
            "[PARSER][LIST-TEXT] source=%s chars=%s text=%s",
            source_name,
            len(page_text),
            page_text[:20_000],
        )

    projects: list[Project] = []
    seen: set[str] = set()
    for card in cards:
        project = parse_project_card(card, settings, source_name, base_url)
        if project is None or project.uid in seen:
            continue
        seen.add(project.uid)
        projects.append(project)
    return projects


def enrich_from_detail(html: str, project: Project, settings: dict[str, Any]) -> Project:
    """Merge detail-page data (full description, exact budget, skills) into a project."""
    soup = BeautifulSoup(html, "lxml")
    detail: dict[str, list[str]] = settings.get("detail_fields", {}) or {}
    scope = _project_scope(soup, project.title)
    scoped_root: Tag | BeautifulSoup = scope if scope is not None else soup
    require_project_scope = bool(settings.get("require_project_scope_for_budget"))

    description = _text(scoped_root, detail.get("description"))
    # On Ponisha, do not trust broad price/budget selectors across the entire page
    # if we could not isolate this project's own DOM section.
    budget_selector_text = (
        _text(scoped_root, detail.get("budget")) if scope is not None or not require_project_scope else ""
    )
    budget_text = budget_selector_text
    skills = _unique_clean_values(
        s.get_text(" ", strip=True) for s in _all(scoped_root, detail.get("skills"))
    )
    category = _text(scoped_root, detail.get("category"))
    employer = _text(scoped_root, detail.get("employer"))
    page_text = clean_text(soup.get_text(" ", strip=True))[:20_000]

    budget = None if _is_non_project_budget_evidence(budget_text) else _parse_amount(budget_text)
    budget_origin = "detail_selector" if budget is not None and budget.is_known else "detail_selector_miss"
    budget_source_evidence = ""
    if budget is None or not budget.is_known:
        # Searching flattened body text can combine "cost" wording with a number
        # in a different recommended project. First search the scoped project DOM.
        if scope is not None:
            found, evidence, origin = _find_budget_in_markup(
                scoped_root, detail.get("budget"), settings.get("budget_regex")
            )
        elif require_project_scope:
            found, evidence, origin = None, "", "no_project_scope"
        else:
            # Compatibility path for older sites whose detail HTML has no heading.
            found, evidence, origin = _find_budget_in_markup(
                soup, detail.get("budget"), settings.get("budget_regex")
            )
        if found is not None and found.is_known:
            budget = found
            budget_text = evidence or found.source_text or budget_text
            budget_origin = "detail_page_text" if origin == "labeled_text_block" else origin
        else:
            header_cfg = settings.get("description_fallback") or {}
            found, amount_text, origin, source_evidence = _find_project_header_budget(
                soup,
                project.title,
                end_markers=header_cfg.get("end_markers"),
                diagnostics=bool(settings.get("_diagnostics")),
            )
            if found is not None and found.is_known:
                budget = found
                budget_text = amount_text
                budget_origin = origin
                budget_source_evidence = source_evidence
            else:
                budget_origin = "not_found" if scope is not None or not require_project_scope else origin

    if not description:
        fallback_cfg = settings.get("description_fallback") or {}
        description = _extract_between_markers(
            page_text,
            fallback_cfg.get("start_markers"),
            fallback_cfg.get("end_markers"),
        )
    # The bounded page-text description can include the neighboring budget/date
    # widget. Remove that exact DOM text node so proposal text remains only the brief.
    if description and budget_source_evidence and budget_source_evidence in description:
        description = description.rsplit(budget_source_evidence, 1)[0].rstrip()
    log.log(
        logging.INFO if settings.get("_diagnostics") else logging.DEBUG,
        "[PARSER][DETAIL] source=%s id=%s title=%r url=%s scope_found=%s scope_tag=%s "
        "scope_id=%r scope_class=%r budget_selector_text=%r budget_text_used=%r "
        "budget_origin=%s budget_final={%s} description_chars=%s skills=%r category=%r employer=%r",
        project.source,
        project.external_id,
        project.title,
        project.url,
        scope is not None,
        scope.name if scope is not None else None,
        scope.get("id") if scope is not None else None,
        (scope.get("class") or [])[:8] if scope is not None else [],
        budget_selector_text,
        budget_text,
        budget_origin,
        _budget_debug(budget),
        len(description),
        skills[:12],
        category or None,
        employer or None,
    )
    if settings.get("_diagnostics"):
        log.info(
            "[PARSER][BUDGET-NODES] source=%s id=%s scope=detail scope_found=%s nodes=%r",
            project.source,
            project.external_id,
            scope is not None,
            _budget_node_summaries(scoped_root, detail.get("budget")),
        )
        log.info(
            "[PARSER][DETAIL-TEXT] source=%s id=%s chars=%s text=%s",
            project.source,
            project.external_id,
            len(page_text),
            page_text[:20_000],
        )
        if description:
            log.info(
                "[PARSER][DESCRIPTION] source=%s id=%s chars=%s text=%s",
                project.source,
                project.external_id,
                len(description),
                description[:12_000],
            )

    update: dict[str, Any] = {}
    if description and len(description) > len(project.description):
        update["description"] = description
    if budget and budget.is_known:
        update["budget"] = budget
    if skills:
        update["skills"] = skills[:12]
    if category:
        update["category"] = category
    if employer:
        update["employer"] = employer
    # Preserve evidence about what was (or was not) found, so the final project
    # record explains why a bid was skipped even after a detail-page attempt.
    raw = dict(project.raw or {})
    raw.update(
        {
            "detail_budget_selector_text": budget_selector_text,
            "detail_budget_text_used": budget_text,
            "detail_budget_source": budget_origin,
            "detail_budget_parsed": _budget_debug(budget),
            "detail_page_text_chars": len(page_text),
        }
    )
    update["raw"] = raw
    deadline = project.employer_deadline or _find_employer_deadline(scoped_root)
    if deadline:
        update["employer_deadline"] = deadline

    if not update:
        return project
    return project.model_copy(update=update)


def detect_existing_bid(html: str, settings: dict[str, Any]) -> bool | None:
    """Return True/False only when the project page provides clear bid-state evidence.

    None means that the page does not expose a reliable state. Callers must not
    submit when state is unknown on a marketplace that requires this check.
    """
    if not html:
        return None

    soup = BeautifulSoup(html, "lxml")
    body_text = _normalized_marker(clean_text(soup.get_text(" ", strip=True)))
    already_markers = settings.get("already_bid_text_contains") or [
        "ویرایش پیشنهاد",
        "تغییر پیشنهاد",
        "اصلاح پیشنهاد",
        "پیشنهاد خود را ویرایش",
        "پیشنهاد خود را تغییر",
        "شما قبلاً پیشنهاد داده‌اید",
        "شما قبلا پیشنهاد داده اید",
        "قبلاً پیشنهاد داده‌اید",
        "قبلا پیشنهاد داده اید",
        "قبلاً پیشنهاد ثبت کرده‌اید",
        "قبلا پیشنهاد ثبت کرده اید",
        "پیشنهاد شما قبلاً ثبت شده",
        "پیشنهاد شما قبلا ثبت شده",
        "پیشنهاد شما با موفقیت ثبت شد",
        "پیشنهاد با موفقیت ثبت شد",
        "edit proposal",
        "edit bid",
        "update proposal",
        "modify proposal",
        "you have already bid",
        "already submitted a proposal",
        "proposal already submitted",
        "already applied",
    ]
    normalized_already = [
        _normalized_marker(str(marker)) for marker in already_markers if str(marker).strip()
    ]
    if any(marker and marker in body_text for marker in normalized_already):
        return True

    actionable_nodes = soup.select("a, button, input[type='submit'], input[type='button'], [role='button']")
    action_labels: list[str] = []
    for node in actionable_nodes:
        values = [
            clean_text(node.get_text(" ", strip=True)),
            clean_text(str(node.get("aria-label") or "")),
            clean_text(str(node.get("title") or "")),
            clean_text(str(node.get("value") or "")),
        ]
        for value in values:
            normalized = _normalized_marker(value)
            if not normalized:
                continue
            action_labels.append(normalized)
            if any(marker and marker in normalized for marker in normalized_already):
                return True
            if re.search(r"(?:edit|update|modify).{0,30}(?:proposal|bid|offer)", normalized):
                return True

    # A real editable proposal form is strong evidence that no proposal has
    # been submitted yet. Positive "edit proposal" markers above take priority.
    form, _ = find_bid_form(html, settings)
    if form is not None and "description" in map_bid_fields(form, settings):
        return False

    submit_markers = (
        "ارسال پیشنهاد",
        "ثبت پیشنهاد",
        "ارسال درخواست",
        "پیشنهاد بدهید",
        "پیشنهاد خود را ثبت",
        "submit proposal",
        "send proposal",
        "place bid",
        "make an offer",
    )
    normalized_submit_markers = tuple(_normalized_marker(marker) for marker in submit_markers)
    if any(marker and marker in label for label in action_labels for marker in normalized_submit_markers):
        return False

    return None


def find_proposal_login_link(html: str, base_url: str = "") -> str | None:
    """Return a safe path when a proposal CTA sends the user to a login page."""
    soup = BeautifulSoup(html, "lxml")
    proposal_labels = ("ارسال پیشنهاد", "ثبت پیشنهاد", "submit proposal", "place bid")
    login_path = re.compile(r"/(?:login|signin|sign-in|auth/login)(?:/|$)", re.IGNORECASE)
    for anchor in soup.find_all("a", href=True):
        label = clean_text(anchor.get_text(" ", strip=True)).casefold()
        if not label or not any(term in label for term in proposal_labels):
            continue
        target = urlsplit(urljoin(base_url or "https://invalid.local/", str(anchor.get("href") or "")))
        if login_path.search(target.path):
            return target.path
    return None


#: Forms whose identity (action/id/class) smells like these are never bid
#: forms, no matter how many textareas they have. The 2026-10-25 incident:
#: none of the configured selectors matched ParsCoders' real bid form, the
#: old "first form with a textarea" heuristic picked the *project report*
#: form (``/project-report/<id>/new``), and the proposal was POSTed there.
_BID_FORM_DISQUALIFIERS = (
    "report",
    "گزارش",
    "تخلف",
    "شکایت",
    "contact",
    "تماس با",
    "search",
    "جستجو",
    "جست‌وجو",
    "login",
    "signin",
    "sign-in",
    "register",
    "ثبت نام",
    "ثبت‌نام",
    "comment",
    "دیدگاه",
    "newsletter",
    "خبرنامه",
    "forgot",
    "بازیابی رمز",
    "subscribe",
)

#: Words a genuine bid form's action/labels tend to carry.
_BID_FORM_HINTS = ("bid", "proposal", "offer", "پیشنهاد")
_PROPOSAL_CTA_LABELS = (
    "ارسال پیشنهاد",
    "ثبت پیشنهاد",
    "ارسال درخواست",
    "پیشنهاد بدهید",
    "پیشنهاد خود را ثبت",
    "submit proposal",
    "send proposal",
    "place bid",
    "make an offer",
)


def _form_identity(form: Tag) -> str:
    classes = form.get("class") or []
    if not isinstance(classes, list):
        classes = [str(classes)]
    return " ".join(
        [
            str(form.get("action") or ""),
            str(form.get("id") or ""),
            str(form.get("name") or ""),
            *[str(c) for c in classes],
        ]
    ).casefold()


def _is_disqualified_form(form: Tag) -> bool:
    identity = _form_identity(form)
    if any(marker in identity for marker in _BID_FORM_DISQUALIFIERS):
        return True
    # A password field means login/registration, never a bid.
    return form.find("input", attrs={"type": "password"}) is not None


def score_bid_form(form: Tag, settings: dict[str, Any]) -> int:
    """How likely is this ``<form>`` the *bid* form? Higher is likelier.

    A bid form essentially needs a price and a text; a report/contact/search
    form never has a price. Scoring every candidate (instead of taking the
    first textarea) is what keeps proposals out of the wrong endpoint.
    """
    if _is_disqualified_form(form):
        return -100
    mapping = map_bid_fields(form, settings)
    score = 0
    if "description" in mapping:
        score += 30
    if "amount" in mapping:
        score += 40
    if "days" in mapping:
        score += 15
    identity = _form_identity(form)
    if any(hint in identity for hint in _BID_FORM_HINTS):
        score += 25
    elif "project" in identity:
        score += 5
    if str(form.get("method") or "post").lower() == "get":
        score -= 20  # bids are POSTed; GET forms are search/filter widgets
    button_labels = " ".join(
        clean_text(node.get_text(" ", strip=True) or str(node.get("value") or ""))
        for node in form.select("button, input[type='submit'], input[type='button']")
    ).casefold()
    if any(label in button_labels for label in ("ارسال پیشنهاد", "ثبت پیشنهاد", "submit proposal")):
        score += 25
    elif "پیشنهاد" in button_labels:
        score += 10
    visible = clean_text(form.get_text(" ", strip=True))
    if any(term in visible for term in ("مبلغ پیشنهادی", "قیمت پیشنهادی", "مبلغ پیشنهاد", "قیمت پیشنهاد")):
        score += 15
    if any(term in visible for term in ("مدت انجام", "زمان تحویل", "مهلت انجام", "مدت زمان")):
        score += 5
    return score


def find_proposal_cta_url(html: str, base_url: str = "") -> str | None:
    """URL behind an «ارسال پیشنهاد» call-to-action, when the form lives elsewhere.

    Many marketplaces keep only a button on the project page; the real form
    is behind that link (a dedicated page or an AJAX-loaded modal). Returns
    None for login links (see :func:`find_proposal_login_link`), bare ``#``
    anchors and pure-JS toggles with no URL to follow.
    """
    soup = BeautifulSoup(html or "", "lxml")
    login_path = re.compile(r"/(?:login|signin|sign-in|auth/login)(?:/|$)", re.IGNORECASE)
    candidates: list[tuple[int, str]] = []
    nodes = soup.select("a[href], a[data-url], button[data-url], [data-proposal-url], [data-bid-url]")
    for node in nodes:
        label = clean_text(node.get_text(" ", strip=True)).casefold()
        raw_url = str(
            node.get("href")
            or node.get("data-url")
            or node.get("data-proposal-url")
            or node.get("data-bid-url")
            or ""
        ).strip()
        if not raw_url or raw_url.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        url = urljoin(base_url or "https://invalid.local/", raw_url)
        path = urlsplit(url).path
        if login_path.search(path):
            continue
        score = 0
        if any(term in label for term in _PROPOSAL_CTA_LABELS):
            score += 30
        if any(hint in path.casefold() for hint in ("proposal", "bid", "offer")):
            score += 20
        identity = " ".join(
            [str(node.get("id") or ""), *[str(c) for c in (node.get("class") or [])]]
        ).casefold()
        if any(hint in identity for hint in ("proposal", "bid", "offer")):
            score += 10
        if score:
            candidates.append((score, url if base_url else path))
    if not candidates:
        return None
    candidates.sort(key=lambda item: -item[0])
    return candidates[0][1]


# The value must be non-empty: the ``+`` is what stops
# ``document.cookie = "token=" + location.hash`` from matching with an empty
# value (which would *clear* a real cookie instead of replaying one).
_JS_COOKIE_SET_RE = re.compile(r"""document\.cookie\s*=\s*['"]([A-Za-z0-9_\-\.]+)=([A-Za-z0-9_\-\.%]+)""")
_JS_COOKIE_LIB_RE = re.compile(
    r"""Cookies\.set\(\s*['"]([A-Za-z0-9_\-\.]+)['"]\s*,\s*['"]([A-Za-z0-9_\-\.%]+)['"]"""
)


def extract_js_cookie_assignments(html: str) -> dict[str, str]:
    """Static cookies the page's own JS would set (``document.cookie = ...``).

    Some sites gate POST endpoints on a trivial JS-set cookie ("prove a
    browser ran") and bounce everyone else to /login with a "JavaScript
    disabled" alert — exactly the ParsCoders behaviour in the incident log.
    Plain static assignments can be replayed faithfully over HTTP; computed
    values (Date/Math/concatenation) intentionally do not match the regexes.
    Cookie *values* from markup are not secrets, but callers log names only.
    """
    cookies: dict[str, str] = {}
    for pattern in (_JS_COOKIE_SET_RE, _JS_COOKIE_LIB_RE):
        for match in pattern.finditer(html or ""):
            name, value = match.group(1), match.group(2)
            if name not in cookies:
                cookies[name] = value
    return cookies


def find_bid_form(html: str, settings: dict[str, Any]) -> tuple[Tag | None, dict[str, Any]]:
    """Locate the bid form and build the payload skeleton from its inputs.

    Selection order: configured selectors first (but never a disqualified
    form such as report/search/login), then the highest-scoring remaining
    form — which must map *both* a description and an amount to be trusted.
    """
    soup = BeautifulSoup(html, "lxml")
    form_selectors = settings.get("form_selectors") or ["form"]
    form: Tag | None = None
    selected_score: int | None = None
    for selector in form_selectors:
        try:
            candidate = soup.select_one(selector)
        except Exception:
            candidate = None
        if candidate is None:
            continue
        candidate_score = score_bid_form(candidate, settings)
        mapping = map_bid_fields(candidate, settings)
        # A bid form carries *both* a text and a price — even via a configured
        # selector. The incident form (project report) had a textarea but no
        # amount; description-only forms (report/contact/questions) must
        # never receive a proposal.
        if candidate_score <= -50 or "description" not in mapping or "amount" not in mapping:
            log.warning(
                "[BID][FORM-REJECTED] selector=%r action=%r score=%s mapped=%r "
                "reason=disqualified_or_missing_bid_fields",
                selector,
                str(candidate.get("action") or "")[:120],
                candidate_score,
                sorted(mapping),
            )
            continue
        form, selected_score = candidate, candidate_score
        break

    if form is None:
        scored = sorted(
            ((score_bid_form(candidate, settings), candidate) for candidate in soup.find_all("form")),
            key=lambda item: -item[0],
        )
        for candidate_score, candidate in scored:
            mapping = map_bid_fields(candidate, settings)
            if candidate_score >= 55 and "description" in mapping and "amount" in mapping:
                form, selected_score = candidate, candidate_score
                break
    if form is not None:
        log.info(
            "[BID][FORM-SELECTED] action=%r method=%s score=%s fields=%r",
            str(form.get("action") or "")[:160],
            str(form.get("method") or "post").lower(),
            selected_score,
            sorted(map_bid_fields(form, settings)),
        )
    if form is None:
        body_text = clean_text(soup.get_text(" ", strip=True))
        title = clean_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
        lowered = body_text.casefold()
        marker_candidates = (
            "ورود به حساب",
            "لطفاً وارد شوید",
            "captcha",
            "cloudflare",
            "access denied",
            "just a moment",
        )
        marker_hits = [marker for marker in marker_candidates if marker.casefold() in lowered]

        form_summaries: list[dict[str, Any]] = []
        for candidate in soup.find_all("form")[:12]:
            controls: list[dict[str, Any]] = []
            for control in candidate.select("input, textarea, select, button")[:16]:
                controls.append(
                    {
                        "tag": control.name,
                        "name": str(control.get("name") or "")[:80],
                        "type": str(control.get("type") or "")[:30],
                        "required": control.has_attr("required"),
                        "text": clean_text(control.get_text(" ", strip=True))[:60]
                        if control.name in {"button", "select"}
                        else "",
                    }
                )
            action = str(candidate.get("action") or "")
            action_path = urlsplit(action).path if action else ""
            form_summaries.append(
                {
                    "id": str(candidate.get("id") or "")[:80],
                    "class": (candidate.get("class") or [])[:6],
                    "action_path": action_path[:160],
                    "method": str(candidate.get("method") or "get").lower(),
                    "bid_score": score_bid_form(candidate, settings),
                    "mapped_fields": sorted(map_bid_fields(candidate, settings)),
                    "controls": controls,
                }
            )

        proposal_links = []
        proposal_labels = ("ارسال پیشنهاد", "ثبت پیشنهاد", "submit proposal", "place bid")
        for anchor in soup.find_all("a", href=True):
            label = clean_text(anchor.get_text(" ", strip=True))
            if label and any(term in label.casefold() for term in proposal_labels):
                proposal_links.append(
                    (
                        label[:80],
                        urlsplit(urljoin("https://invalid.local/", str(anchor.get("href") or ""))).path[:160],
                    )
                )
                if len(proposal_links) >= 12:
                    break

        log.warning(
            "[BID][FORM-NOT-FOUND] title=%r selectors=%r forms=%s textareas=%s "
            "inputs=%s buttons=%s scripts=%s page_markers=%r body_chars=%s "
            "form_summaries=%r proposal_links=%r",
            title,
            form_selectors,
            len(soup.find_all("form")),
            len(soup.find_all("textarea")),
            len(soup.find_all("input")),
            len(soup.find_all(["button", "input"], attrs={"type": re.compile("submit|button", re.I)})),
            len(soup.find_all("script")),
            marker_hits,
            len(body_text),
            form_summaries,
            proposal_links,
        )
        return None, {}

    payload: dict[str, Any] = serialize_form(form, include_hidden=bool(settings.get("include_hidden", True)))
    return form, payload


def serialize_form(form: Tag, *, include_hidden: bool = True) -> dict[str, str]:
    """Serialize a form the way a browser would submit it ("successful controls").

    The old payload builder only copied hidden inputs and text inputs that
    happened to carry a ``value`` — silently dropping the defaults of
    ``<select>`` elements and textareas. On the real ParsCoders bid form
    (verified via ``bidpilot inspect``, project 621237) fields such as
    ``initEscrowPercent``/``expertGuaranteePercent``/``validDays`` live next
    to ``bidAmount``/``deadline``/``message``; omitting their browser
    defaults is exactly the kind of thing a Laravel validator rejects.

    Rules (HTML form submission, subset): named + non-disabled controls;
    submit/button/file/image/reset inputs are skipped; checkbox/radio only
    when checked; a select contributes its selected option (else the first),
    using the option's text when it has no ``value``; a textarea contributes
    its default content. The caller overwrites the mapped bid fields.
    """
    payload: dict[str, str] = {}
    for node in form.select("input[name], select[name], textarea[name]"):
        if node.has_attr("disabled"):
            continue
        name = str(node.get("name") or "")
        if not name:
            continue
        if node.name == "input":
            input_type = str(node.get("type") or "text").lower()
            if input_type in ("submit", "button", "file", "image", "reset"):
                continue
            if input_type == "hidden" and not include_hidden:
                continue
            if input_type in ("checkbox", "radio"):
                if node.has_attr("checked"):
                    payload[name] = str(node.get("value") or "on")
                continue
            payload[name] = str(node.get("value") or "")
        elif node.name == "textarea":
            payload[name] = node.get_text()
        elif node.name == "select":
            options = node.find_all("option")
            if not options:
                continue
            chosen = next((o for o in options if o.has_attr("selected")), options[0])
            value = chosen.get("value")
            payload[name] = str(value) if value is not None else chosen.get_text(strip=True)
    return payload


def map_bid_fields(form: Tag, settings: dict[str, Any]) -> dict[str, str]:
    """Return {'amount': <input name>, 'description': <textarea name>, 'days': <input name>}."""
    mapping: dict[str, str] = {}
    desired = settings.get("fields", {}) or {}
    for key in ("amount", "description", "days"):
        for selector in desired.get(key, []) or []:
            try:
                node = form.select_one(selector)
            except Exception:
                node = None
            if node is not None and node.get("name"):
                mapping[key] = str(node["name"])
                break
    if "description" not in mapping:
        textarea = form.find("textarea")
        if textarea is not None and textarea.get("name"):
            mapping["description"] = str(textarea["name"])
    # Generic fallbacks for sites whose config has no field selectors yet:
    # match on input name/id vocabulary instead of guessing positions.
    generic = {
        "amount": re.compile(r"amount|price|cost|fee|budget|offer|bid", re.I),
        "days": re.compile(r"day|duration|delivery|deadline|period", re.I),
    }
    for key, pattern in generic.items():
        if key in mapping:
            continue
        for node in form.select("input[name], select[name]"):
            if node.get("type") in ("hidden", "submit", "button", "checkbox", "radio", "file"):
                continue
            identity = f"{node.get('name') or ''} {node.get('id') or ''}"
            if pattern.search(identity):
                mapping[key] = str(node["name"])
                break
    return mapping


def extract_response_notices(html: str) -> list[str]:
    """Collect concise visible form/alert notices without dumping the whole response."""
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript", "template"]):
        node.decompose()
    selectors = (
        ".alert-danger",
        ".alert-error",
        ".toast-error",
        ".toast-message",
        ".validation-summary-errors",
        ".field-validation-error",
        "[role='alert']",
    )
    notices: list[str] = []
    seen: set[str] = set()
    for selector in selectors:
        try:
            nodes = soup.select(selector)
        except Exception:
            continue
        for node in nodes[:5]:
            text = clean_text(node.get_text(" ", strip=True))
            key = _normalized_marker(text)
            if not text or len(text) > 600 or not key or key in seen:
                continue
            seen.add(key)
            notices.append(text)
            if len(notices) >= 5:
                return notices
    return notices


def extract_json_notices(text: str) -> list[str]:
    """Notices from a JSON response body (Laravel validation errors & friends).

    JS-driven bid forms (like the real ParsCoders one: no ``action``, a
    ``_token`` field) are answered with JSON — ``{"message": ...,
    "errors": {"bidAmount": ["..."]}}`` with HTTP 422. Surfacing those field
    errors turns "نتیجه نامشخص" into the server naming exactly what it wants.
    """
    body = (text or "").strip()
    if not body.startswith("{"):
        return []
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    notices: list[str] = []
    for key in ("message", "response_message"):
        message = data.get(key)
        if isinstance(message, str) and message.strip():
            notices.append(clean_text(message)[:300])
    errors = data.get("errors")
    if isinstance(errors, dict):
        for field, messages in errors.items():
            if isinstance(messages, (list, tuple)):
                items = [m for m in messages if isinstance(m, str) and m.strip()][:2]
            elif isinstance(messages, str) and messages.strip():
                items = [messages]
            else:
                items = []
            for item in items:
                notices.append(f"{field}: {clean_text(item)[:200]}")
                if len(notices) >= 5:
                    return notices
    return notices[:5]


#: A JavaScript-requirement notice must say "javascript" AND "disabled/enable…"
#: within one short window. The previous implementation searched the two parts
#: independently anywhere in the flattened page text, so a *skill tag* named
#: «جاوا اسکریپت» plus an unrelated «فعال نیست» 300 KB later (user status,
#: settings copy, …) reported "JavaScript required" on perfectly usable pages
#: and shut the scheduler down. Proximity is what makes the signal real.
_JS_REQUIRED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?:javascript|جاوا اسکریپت).{0,80}?"
        r"(?:disabled|not enabled|must be enabled|is required|"
        r"غیر\s*فعال|فعال نیست|فعال کنید|فعال باشد|فعال نمایید)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:enable|activate|turn on).{0,40}?javascript",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:فعال کنید|فعال باشد|فعال نمایید|غیر\s*فعال).{0,80}?(?:javascript|جاوا اسکریپت)",
        re.IGNORECASE,
    ),
)


def _visible_page_text(html: str) -> str:
    """Flatten *visible* text only.

    ``<noscript>`` banners ("JavaScript را فعال کنید") ship on almost every
    page and are by definition only rendered when JS is off *in a browser*;
    ``<script>``/``<style>``/``<template>`` content is not visible copy either.
    Counting them made the JS detector fire on every healthy page.
    """
    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript", "template"]):
        node.decompose()
    return _normalized_marker(soup.get_text(" ", strip=True))


def javascript_requirement_snippet(html: str) -> str:
    """Return the short matched notice (for logs/messages), or ``""``."""
    text = _visible_page_text(html)
    if not text:
        return ""
    for pattern in _JS_REQUIRED_PATTERNS:
        match = pattern.search(text)
        if match:
            start = max(0, match.start() - 30)
            return text[start : match.end() + 30].strip()[:200]
    return ""


def requires_javascript(html: str) -> bool:
    """Detect an explicit, *visible* site message requiring browser JavaScript."""
    return bool(javascript_requirement_snippet(html))


def detect_success(status_code: int, html: str, settings: dict[str, Any]) -> tuple[bool, str]:
    """Require explicit proposal success; generic words on the project page are insufficient."""
    success_texts = settings.get("success_text_contains") or [
        "پیشنهاد شما با موفقیت ثبت شد",
        "پیشنهاد با موفقیت ثبت شد",
        "پیشنهاد شما ثبت شد",
    ]
    failure_texts = settings.get("failure_text_contains") or [
        "قبلاً پیشنهاد",
        "قبلا پیشنهاد",
        "اعتبار کافی",
        "امکان ارسال",
        "مجاز نیست",
        "وارد شوید",
    ]
    json_notices = extract_json_notices(html)
    notices = extract_response_notices(html) + json_notices
    error_words = ("خطا", "ناموفق", "خطای", "failed", "error")

    # Check for structured JSON success response first
    body_raw = (html or "").strip()
    if body_raw.startswith("{"):
        try:
            data = json.loads(body_raw)
            if isinstance(data, dict):
                if (
                    data.get("status") in ("success", True, 1)
                    or data.get("success") is True
                    or data.get("ok") is True
                ):
                    msg = str(data.get("message") or "پیشنهاد با پاسخ موفقیت‌آمیز (JSON) سایت تأیید شد.")
                    return True, msg
        except (ValueError, TypeError):
            pass

    # A specific response alert is much stronger evidence than the surrounding
    # project page, which can mention the word "error" in unrelated content.
    for notice in notices:
        if has_any(notice, failure_texts) or any(word in notice.casefold() for word in error_words):
            return False, f"سایت پیام داد: {notice[:180]}"
        if has_any(notice, success_texts):
            return True, "پیشنهاد با پیام موفقیت سایت تأیید شد."
    if status_code == 419:
        # Laravel's "Page Expired": the form's CSRF token was stale/rejected.
        return False, "سایت توکن امنیتی فرم (CSRF) را رد کرد (HTTP 419)."
    if status_code >= 400:
        # Prefer a per-field validation error ("bidAmount: ...") over the
        # generic top-level message — the field name is the actionable part.
        detail = next(
            (n for n in json_notices if ": " in n),
            json_notices[0] if json_notices else "",
        )
        suffix = f": {detail[:180]}" if detail else ""
        return False, f"سایت درخواست را رد کرد (HTTP {status_code}){suffix}"

    soup = BeautifulSoup(html or "", "lxml")
    for node in soup(["script", "style", "noscript", "template"]):
        node.decompose()
    body = clean_text(soup.get_text(" ", strip=True))
    if has_any(body, failure_texts):
        snippet = next((t for t in failure_texts if clean_text(t) in body), "پیام رد مشخص")
        return False, f"سایت درخواست را رد کرد: {snippet}"
    if has_any(body, success_texts):
        return True, "پیشنهاد با پیام موفقیت سایت تأیید شد."
    return False, "نتیجه نامشخص؛ پیام روشن موفقیت/خطا در پاسخ سایت پیدا نشد."


__all__ = [
    "DEFAULT_ID_REGEX",
    "detect_success",
    "detect_existing_bid",
    "extract_response_notices",
    "extract_json_notices",
    "serialize_form",
    "javascript_requirement_snippet",
    "requires_javascript",
    "enrich_from_detail",
    "extract_external_id",
    "extract_js_cookie_assignments",
    "fallback_id",
    "find_bid_form",
    "find_proposal_cta_url",
    "find_proposal_login_link",
    "map_bid_fields",
    "score_bid_form",
    "parse_list_page",
    "parse_project_card",
    "parse_published",
]
