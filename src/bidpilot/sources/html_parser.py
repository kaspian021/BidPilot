"""HTML -> Project parsing shared by every marketplace adapter.

The card markup of each site is described in that site's ``config.yaml``, so a
markup change is a config edit, not a code change. Every selector list is tried
in order; the first selector that matches something wins.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from ..models import Budget, Project
from ..pricing.budget import parse_budget
from ..text import clean_text, has_any

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
    node = _first(card, selectors)
    return clean_text(node.get_text(" ", strip=True)) if node else ""


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
    r"مبلغ",
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
    if not cleaned:
        return None

    for pattern in patterns or DEFAULT_BUDGET_REGEXES:
        try:
            match = re.search(pattern, cleaned, re.I)
        except re.error:
            continue
        if not match:
            continue
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
        budget = parse_budget_text(match.group(0))
        if budget is not None and budget.is_known:
            return budget
    return None


def parse_published(text: str) -> datetime | None:
    """Best-effort parser for Persian relative/absolute timestamps ("۳ ساعت پیش")."""
    if not text:
        return None
    t = clean_text(text)
    now = datetime.now()
    rel = re.search(r"(\d+)\s*(دقیقه|ساعت|روز|هفته|ماه)\s*پیش", t)
    if rel:
        value, unit = int(rel.group(1)), rel.group(2)
        minutes = {"دقیقه": 1, "ساعت": 60, "روز": 1440, "هفته": 10080, "ماه": 43200}[unit]
        from datetime import timedelta

        return now - timedelta(minutes=value * minutes)
    try:
        import jdatetime

        m = re.search(r"(\d{1,4})[/-](\d{1,2})[/-](\d{1,2})", t)
        if m and len(m.group(1)) == 4 and int(m.group(1)) > 1300:
            jalali = jdatetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return jalali.togregorian()
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

    budget_text = _text(card, fields.get("budget"))
    budget = _parse_amount(budget_text)
    skills = [clean_text(s.get_text(" ", strip=True)) for s in _all(card, fields.get("skills"))]
    description = _text(card, fields.get("description"))
    card_text = clean_text(card.get_text(" ", strip=True)) if isinstance(card, Tag) else ""
    if budget is None or not budget.is_known:
        # The budget is often just a line of the card without its own class.
        found = find_budget(card_text, settings.get("budget_regex"))
        if found is not None and found.is_known:
            budget = found
            budget_text = budget.source_text or budget.as_text()

    log.info(
        "[PARSER][CARD] source=%s id=%s title=%r url=%s budget_selector_text=%r "
        "budget_final={%s} skills=%r category=%r employer=%r",
        source_name,
        external_id,
        title,
        link,
        budget_text,
        _budget_debug(budget),
        [s for s in skills if s][:12],
        _text(card, fields.get("category")) or None,
        _text(card, fields.get("employer")) or None,
    )
    if settings.get("_diagnostics"):
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
        published_at=parse_published(_text(card, fields.get("published_at"))),
        raw={"budget_text": budget_text},
    )


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
            parent = anchor.find_parent(["article", "li", "div"]) or anchor
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
    for selector in settings.get("card_selectors", []) or []:
        try:
            cards = [c for c in soup.select(selector) if isinstance(c, Tag)]
        except Exception:
            cards = []
        if cards:
            break

    if not cards:
        cards = fallback_cards(soup, settings)

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

    description = _text(soup, detail.get("description"))
    budget_text = _text(soup, detail.get("budget"))
    skills = [clean_text(s.get_text(" ", strip=True)) for s in _all(soup, detail.get("skills"))]
    category = _text(soup, detail.get("category"))
    employer = _text(soup, detail.get("employer"))
    page_text = clean_text(soup.get_text(" ", strip=True))[:20_000]

    budget = _parse_amount(budget_text)
    budget_origin = "detail_selector" if budget_text else "detail_selector_miss"
    if budget is None or not budget.is_known:
        # no budget selector matched: look for a labeled amount or currency on the page
        found = find_budget(page_text, settings.get("budget_regex"))
        if found is not None and found.is_known:
            budget = found
            budget_text = found.source_text or budget_text
            budget_origin = "detail_page_text"
        else:
            budget_origin = "not_found"
    log.info(
        "[PARSER][DETAIL] source=%s id=%s title=%r url=%s "
        "budget_selector_text=%r budget_origin=%s budget_final={%s} "
        "description_chars=%s skills=%r category=%r employer=%r",
        project.source,
        project.external_id,
        project.title,
        project.url,
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
    if not update:
        return project
    return project.model_copy(update=update)


def find_bid_form(html: str, settings: dict[str, Any]) -> tuple[Tag | None, dict[str, Any]]:
    """Locate the bid form and build the payload skeleton from its inputs."""
    soup = BeautifulSoup(html, "lxml")
    form_selectors = settings.get("form_selectors") or ["form"]
    form: Tag | None = None
    for selector in form_selectors:
        try:
            form = soup.select_one(selector)
        except Exception:
            form = None
        if form is not None:
            break

    if form is None:
        # Heuristic: the form that contains a textarea and an amount-ish input.
        for candidate in soup.find_all("form"):
            if candidate.find("textarea") or candidate.find("input", {"name": re.compile("amount|price")}):
                form = candidate
                break
    if form is None:
        return None, {}

    payload: dict[str, Any] = {}
    if settings.get("include_hidden", True):
        for inp in form.select("input[type=hidden]"):
            name = inp.get("name")
            if name:
                payload[name] = inp.get("value", "")
    for inp in form.select("input"):
        if inp.get("type") in ("submit", "button", "file", "hidden"):
            continue
        name = inp.get("name")
        if name and name not in payload and inp.get("value"):
            payload[name] = inp.get("value", "")
    return form, payload


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
    return mapping


def detect_success(status_code: int, html: str, settings: dict[str, Any]) -> tuple[bool, str]:
    """Decide whether the bid actually landed, using configurable text/URL markers."""
    success_texts = settings.get("success_text_contains") or ["با موفقیت", "ثبت شد", "درخواست شما"]
    failure_texts = settings.get("failure_text_contains") or ["خطا", "قبلاً", "اعتبار کافی", "مجاز نیست"]
    body = clean_text(BeautifulSoup(html, "lxml").get_text(" ", strip=True))

    if has_any(body, failure_texts):
        snippet = next((t for t in failure_texts if clean_text(t) in body), "پیام خطای نامشخص")
        return False, f"سایت درخواست را رد کرد: {snippet}"
    if status_code >= 400:
        return False, f"HTTP {status_code}"
    if has_any(body, success_texts):
        return True, "درخواست با موفقیت ثبت شد."
    return False, "نتیجه نامشخص؛ متن موفقیت در پاسخ پیدا نشد."


__all__ = [
    "DEFAULT_ID_REGEX",
    "detect_success",
    "enrich_from_detail",
    "extract_external_id",
    "fallback_id",
    "find_bid_form",
    "map_bid_fields",
    "parse_list_page",
    "parse_project_card",
    "parse_published",
]
