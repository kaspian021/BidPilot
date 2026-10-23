"""HTML -> Project parsing for Parscoders.

The card markup is described in ``src/bidpilot/sources/parscoders/config.yaml`` so a
markup change is a config edit, not a code change. Every selector list is tried in
order; the first selector that matches something wins.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from ...models import Budget, Project
from ...pricing.budget import parse_budget
from ...text import clean_text, has_any

DEFAULT_ID_REGEX = r"/projects?/(\d+)"
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


def _parse_amount(text: str) -> Budget | None:
    return parse_budget(text)


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
    base_url: str = "https://parscoders.com",
) -> Project | None:
    fields: dict[str, list[str]] = settings.get("fields", {}) or {}
    link = _attr(card, fields.get("link") or fields.get("title"), "href")
    title = _text(card, fields.get("title")) or _attr(card, fields.get("link"), "title")
    link = urljoin(base_url + "/", link)
    if not title:
        m = re.search(r"<title>(.*?)</title>", str(card), re.S)
        title = clean_text(m.group(1)) if m else ""

    external_id = extract_external_id(link, settings.get("id_regex", DEFAULT_ID_REGEX)) or fallback_id(
        link, title
    )
    if not title and not link:
        return None

    budget_text = _text(card, fields.get("budget"))
    skills = [clean_text(s.get_text(" ", strip=True)) for s in _all(card, fields.get("skills"))]
    description = _text(card, fields.get("description"))

    return Project(
        source=source_name,
        external_id=external_id,
        title=title or "(بدون عنوان)",
        url=link,
        description=description,
        budget=_parse_amount(budget_text),
        skills=[s for s in skills if s][:12],
        category=_text(card, fields.get("category")) or None,
        employer=_text(card, fields.get("employer")) or None,
        published_at=parse_published(_text(card, fields.get("published_at"))),
        raw={"budget_text": budget_text},
    )


def parse_list_page(
    html: str,
    settings: dict[str, Any],
    source_name: str = "parscoders",
    base_url: str = "https://parscoders.com",
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
        # Fallback: any anchor that looks like a project link, wrapped in its parent card.
        cards = []
        for anchor in soup.find_all("a", href=True):
            if re.search(settings.get("id_regex", DEFAULT_ID_REGEX), anchor["href"]):
                parent = anchor.find_parent(["div", "article", "li"]) or anchor
                if parent not in cards:
                    cards.append(parent)

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

    update: dict[str, Any] = {}
    if description and len(description) > len(project.description):
        update["description"] = description
    budget = _parse_amount(budget_text)
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
