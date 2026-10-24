"""AI-driven discovery: the model reads the marketplace, not a CSS selector.

A selector-based scraper only sees the page it was told about. Real marketplaces
give the freelancer *several* lists (projects matching his profile, all open
projects, categories, search), a profile page that defines his expertise, and a
detail page per project with the budget and the employer's own deadline. If the
markup changes — or the site was never calibrated — the scraper reads nothing
and the bot starts bidding on whatever it happened to see.

So this module gives the LLM the same job a human freelancer does:

1. it receives the site's *logic* (a short brief written by us) and the user's
   profile — never a hard-coded list of URLs;
2. it looks at the links of the page it is on and decides where to go next;
3. on a project page it extracts the facts **verbatim** (title, description,
   budget text, the employer's delivery time, skills);
4. only then the normal pipeline prices the bid (a percentage of the employer's
   budget), writes the proposal and submits it.

Everything the model returns is treated as data, not as truth: budgets go through
the deterministic Persian parser, links must exist on the page, and the whole
run has hard caps so a bad answer can never loop or burn the free tier.

The agent is deliberately transport-agnostic: ``fetch`` is the source's own
logged-in ``get_html``, so the model browses *inside the user's account*.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ..config import Profile
from ..jsonparse import parse_json_object
from ..models import Project
from ..text import clean_text, truncate
from . import html_parser as parser

log = logging.getLogger(__name__)

Fetch = Callable[[str], Awaitable[str]]

#: never spend more than this on one cycle (free tiers are ~30 requests/minute)
MAX_STEPS = 10
MAX_DETAIL_PAGES = 6
MAX_PROJECTS = 6
MAX_TEXT_CHARS = 3_500
MAX_DETAIL_CHARS = 6_000
MAX_LINKS = 70

_STATIC_SUFFIXES = (
    ".css",
    ".js",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".pdf",
    ".zip",
    ".rar",
    ".mp4",
)

#: navigation words that are never a project or a useful list
_SKIP_WORDS = (
    "خروج",
    "logout",
    "sign out",
    "ورود",
    "login",
    "sign in",
    "ثبت‌نام",
    "ثبت نام",
    "register",
    "فراموشی رمز",
    "قوانین",
    "حریم خصوصی",
    "تماس با ما",
    "درباره ما",
    "پشتیبانی",
    "telegram",
    "instagram",
    "twitter",
)

AGENT_SYSTEM = """تو «دستیار جست‌وجوی پروژه» هستی و مثل یک فریلنسر باتجربه داخل حساب کاربری \
یک کاربر در یک سایت فریلنسری ایرانی کار می‌کنی.

وظیفه‌ات: در سایت بگرد، فهرست‌های پروژه را پیدا کن، پروژه‌های مرتبط با تخصص همین کاربر را \
باز کن و اطلاعات واقعی هر پروژه را بردار — نه چیز دیگری.

قواعد سخت‌گیرانه:
- فقط از لینک‌های شماره‌دارِ همان صفحه استفاده کن؛ هیچ آدرسی از خودت نساز و حدس نزن.
- هرچه برمی‌داری باید عیناً در متن همان صفحه باشد. اگر چیزی نبود، خالی بگذار.
- ساختار هر سایت متفاوت است؛ «توضیح سایت» می‌گوید کجا را بگردی. اول بفهم صفحه‌ی فعلی \
فهرست است یا پروژه یا پروفایل.
- فهرست‌های مختلف را با هم اشتباه نگیر: فهرست مخصوص تخصص من، فهرست همه‌ی پروژه‌های باز، \
دسته‌بندی‌ها و جست‌وجو.
- پروژه‌های بی‌ربط به تخصص کاربر را جمع نکن (مثلاً پروژه‌ی طراحی لوگو برای یک برنامه‌نویس موبایل).
- فقط یک کار در هر پاسخ انجام بده و فقط JSON برگردان؛ هیچ توضیح اضافه‌ای ننویس."""

STEP_PROMPT = """# تخصص کاربر (منبع مجاز برای سنجش ارتباط)
{profile}

# توضیح این سایت
{brief}

# کارهایی که تا الان انجام داده‌ای
{history}

# شمارش
صفحه‌های خوانده‌شده: {pages} | پروژه‌های جمع‌آوری‌شده: {collected} | قدم: {step}/{max_steps}

# صفحه‌ی فعلی
آدرس: {url}
عنوان: {title}
متن صفحه:
{text}

# لینک‌های این صفحه
{links}

# تصمیم
یکی از این JSONها را برگردان:
- رفتن به یک لینک: {{"action": "open", "link": 3, "why": "..."}}
- این صفحه فهرست پروژه‌ی مرتبط است و باید باز شوند: {{"action": "collect", "links": [2, 5], "why": "..."}}
- کار تمام است: {{"action": "done", "why": "..."}}

اگر این صفحه فهرست پروژه است، حتماً پروژه‌های مرتبط را با «collect» بده (نه one-by-one). \
اگر صفحه پروفایل کاربر است، فقط بخوان و به فهرست پروژه‌ها برو."""

EXTRACT_SYSTEM = """تو یک استخراج‌کننده‌ی دقیق اطلاعات از صفحه‌ی پروژه در یک سایت فریلنسری هستی.
فقط چیزهایی را برگردان که در متن صفحه آمده است؛ هیچ چیزی از خودت نساز و حدس نزن.
خروجی فقط یک JSON معتبر است."""

EXTRACT_PROMPT = """# متن صفحه‌ی پروژه ({url})
{text}

# خروجی (JSON)
{{
  "is_project": true,
  "title": "عنوان دقیق پروژه",
  "description": "توضیحات کارفرما، خلاصه‌ی وفادار به متن",
  "budget_text": "متن بودجه/حق‌الزحمه عیناً (مثلاً «بودجه: ۵۰۰,۰۰۰ تا ۱,۵۰۰,۰۰۰ تومان») یا \\"\\"",
  "deadline_text": "زمان تحویل/مهلت اعلامی کارفرما عیناً (مثلاً «۱۰ روز») یا \\"\\"",
  "skills": ["مهارت‌های ذکرشده"],
  "category": "دسته‌بندی یا \\"\\"",
  "employer": "نام کارفرما یا \\"\\""
}}

اگر این صفحه پروژه نیست (فهرست، پروفایل، ورود)، فقط {{"is_project": false}} برگردان."""


class DiscoveryError(RuntimeError):
    """The agent could not do its job — the caller falls back to the selectors."""


@dataclass
class PageLink:
    index: int
    url: str
    label: str


@dataclass
class PageSnapshot:
    url: str
    title: str
    text: str
    links: list[PageLink] = field(default_factory=list)
    is_login: bool = False


@dataclass
class DiscoveryResult:
    projects: list[Project] = field(default_factory=list)
    pages_read: int = 0
    llm_calls: int = 0
    visited: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    used_llm: bool = True


# ---------------------------------------------------------------------------
# Page digest
# ---------------------------------------------------------------------------
def _host(url: str) -> str:
    host = urlparse(url).netloc.lower().split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _same_host(url: str, base_url: str) -> bool:
    host, base = _host(url), _host(base_url)
    return bool(host) and (host == base or host.endswith("." + base))


def _looks_like_item(url: str) -> bool:
    path = urlparse(url).path
    return bool(re.search(r"/(project|projects|job|jobs|task|tasks|gig)/[^/]+", path, re.I))


def digest_html(
    html: str,
    url: str,
    *,
    base_url: str,
    max_text: int = MAX_TEXT_CHARS,
    max_links: int = MAX_LINKS,
) -> PageSnapshot:
    """Turn a page into what the model needs: title, readable text, real links."""
    soup = BeautifulSoup(html or "", "lxml")
    title = clean_text(soup.title.get_text(" ", strip=True) if soup.title else "")[:200]
    for tag in soup(["script", "style", "noscript", "svg", "head", "iframe"]):
        tag.decompose()
    text = clean_text(soup.get_text(" ", strip=True))[:max_text]
    # A password field alone means nothing: most sites ship a login modal on
    # every page. A real login wall is a *sparse* page asking for credentials.
    is_login = bool(soup.select_one("input[type=password]")) and len(text) < 700

    seen: set[str] = set()
    links: list[PageLink] = []
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absolute = urljoin(url, href).split("#")[0]
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"} or not _same_host(absolute, base_url):
            continue
        if parsed.path.lower().endswith(_STATIC_SUFFIXES):
            continue
        label = clean_text(anchor.get_text(" ", strip=True))[:90]
        lowered = label.casefold()
        if any(word in lowered for word in _SKIP_WORDS):
            continue
        if not label and not _looks_like_item(absolute):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        links.append(PageLink(index=len(links), url=absolute, label=label or "بدون عنوان"))
        if len(links) >= max_links:
            break

    return PageSnapshot(url=url, title=title, text=text, links=links, is_login=is_login)


def render_links(links: list[PageLink], *, limit: int = 50) -> str:
    lines = [f"[{link.index}] {link.label} — {urlparse(link.url).path}" for link in links[:limit]]
    if len(links) > limit:
        lines.append(f"… و {len(links) - limit} لینک دیگر (بدون شماره)")
    return "\n".join(lines) or "(لینکی در این صفحه نیست)"


def profile_brief(profile: Profile) -> str:
    parts = [
        f"نام: {profile.full_name or '—'}",
        f"نقش: {profile.role or '—'}",
        f"سابقه: {profile.years_experience} سال",
        f"مهارت‌ها: {'، '.join(profile.stack) or '—'}",
    ]
    if profile.highlights:
        parts.append("نمونه‌کارها: " + "؛ ".join(profile.highlights[:4]))
    return "\n".join(parts)


DEFAULT_SITE_BRIEF = """این یک سایت فریلنسری است و تو با حساب کاربری همین کاربر وارد شده‌ای.
معمولاً دو نوع فهرست وجود دارد: فهرستی که پروژه‌ها را بر اساس تخصص پروفایل من نشان می‌دهد و
فهرستی از همه‌ی پروژه‌های باز. صفحه‌ی «پروفایل من» مهارت‌های ثبت‌شده را نشان می‌دهد.
هر کارت پروژه معمولاً عنوان، بودجه/حق‌الزحمه و زمان تحویل دارد؛ صفحه‌ی هر پروژه توضیحات کامل،
بودجه‌ی کارفرما، مدت زمان و مهارت‌های مورد نیاز را دارد. اول فهرست‌ها را از ناوبری سایت پیدا کن،
بعد پروژه‌های مرتبط با تخصص من را باز کن."""


def _external_id(url: str, title: str) -> str:
    match = re.search(r"/(?:project|projects|job|jobs|task|tasks)/([A-Za-z0-9_-]+)", url)
    if match:
        return match.group(1)
    return hashlib.sha1(f"{url}|{title}".encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------
class DiscoveryAgent:
    """Browse a marketplace with the user's own session and a language model."""

    def __init__(
        self,
        *,
        source_name: str,
        base_url: str,
        fetch: Fetch,
        llm: Any,
        profile: Profile,
        brief: str = "",
        seed_paths: list[str] | None = None,
        max_steps: int = MAX_STEPS,
        max_detail_pages: int = MAX_DETAIL_PAGES,
        max_projects: int = MAX_PROJECTS,
        page_delay: float = 0.0,
    ) -> None:
        self.source_name = source_name
        self.base_url = base_url.rstrip("/")
        self.fetch = fetch
        self.llm = llm
        self.profile = profile
        self.brief = (brief or DEFAULT_SITE_BRIEF).strip()
        self.seed_paths = seed_paths or ["/"]
        self.max_steps = max(1, max_steps)
        self.max_detail_pages = max(1, max_detail_pages)
        self.max_projects = max(1, max_projects)
        self.page_delay = max(0.0, page_delay)
        self._detail_pages = 0
        self._login_seen = False

    # ------------------------------------------------------------------ public
    async def discover(self) -> DiscoveryResult:
        result = DiscoveryResult()
        self._detail_pages = 0
        self._login_seen = False
        queue: deque[str] = deque(self._seeds())
        visited: set[str] = set()
        history: list[str] = []
        collected: dict[str, Project] = {}
        details_read = 0
        step = 0

        while queue and step < self.max_steps and len(collected) < self.max_projects:
            url = queue.popleft()
            if url in visited:
                continue
            visited.add(url)
            step += 1
            try:
                html = await self.fetch(url)
            except Exception as exc:  # network/404: keep browsing, don't die
                result.notes.append(f"{url}: {exc}")
                log.debug("discovery: %s failed (%s)", url, exc)
                continue

            page = digest_html(html, url, base_url=self.base_url)
            result.pages_read += 1
            result.visited.append(url)
            if page.is_login:
                raise DiscoveryError("سایت فرم ورود نشان داد؛ کوکی منقضی شده است — «/market» را بزن.")

            if (
                self._project_page_like(url, page)
                and len(collected) < self.max_projects
                and details_read < self.max_detail_pages
            ):
                details_read += 1
                project = await self._read_project(url, html=html)
                result.llm_calls += 1
                if project is not None and project.uid not in collected:
                    collected[project.uid] = project
                    history.append(f"پروژه خوانده شد: {truncate(project.title, 60)}")
                continue

            try:
                decision = await self._decide(page, history, step, len(collected))
            except Exception as exc:
                raise DiscoveryError(f"تصمیم‌گیری مدل ناموفق بود: {exc}") from exc
            result.llm_calls += 1

            action = str(decision.get("action") or "").strip().lower()
            why = truncate(str(decision.get("why") or ""), 120)
            if why:
                history.append(f"{_short(url)}: {action} — {why}")

            if action == "done":
                break
            if action == "open":
                link = _pick(page.links, decision.get("link"))
                if link is None:
                    result.notes.append(f"لینک نامعتبر از مدل: {decision.get('link')!r}")
                    continue
                if link.url not in visited:
                    queue.appendleft(link.url)
                history.append(f"رفتم به «{link.label}» ({_short(link.url)})")
                continue
            if action in {"collect", "collect_projects", "projects"}:
                indexes = decision.get("links")
                if indexes is None and decision.get("link") is not None:
                    indexes = [decision.get("link")]
                chosen = _pick_many(page.links, indexes)
                if not chosen:
                    result.notes.append("مدل پروژه‌ای برای باز کردن انتخاب نکرد.")
                    continue
                for link in chosen:
                    if len(collected) >= self.max_projects or details_read >= self.max_detail_pages:
                        break
                    if link.url in visited:
                        continue
                    visited.add(link.url)
                    details_read += 1
                    project = await self._read_project(link.url)
                    result.llm_calls += 1
                    if project is not None and project.uid not in collected:
                        collected[project.uid] = project
                        history.append(f"پروژه خوانده شد: {truncate(project.title, 60)}")
                if self.page_delay:
                    await asyncio.sleep(self.page_delay)
                continue

            result.notes.append(f"پاسخ نامفهوم مدل: {action or '(خالی)'}")

        result.pages_read += self._detail_pages
        result.projects = list(collected.values())
        if not result.projects and self._login_seen:
            raise DiscoveryError("سایت فرم ورود نشان داد؛ کوکی منقضی شده است — «/market» را بزن.")
        log.info(
            "%s: AI discovery read %s pages / %s projects (%s LLM calls)",
            self.source_name,
            result.pages_read,
            len(result.projects),
            result.llm_calls,
        )
        return result

    # ------------------------------------------------------------------ steps
    @staticmethod
    def _project_page_like(url: str, page: PageSnapshot) -> bool:
        """True for a detail page: a project-ish URL with no list of projects on it."""
        if not _looks_like_item(url):
            return False
        item_links = sum(1 for link in page.links if _looks_like_item(link.url))
        return item_links < 3

    def _seeds(self) -> list[str]:
        seeds: list[str] = []
        for path in self.seed_paths:
            url = path if path.startswith("http") else urljoin(self.base_url + "/", path.lstrip("/"))
            if url not in seeds:
                seeds.append(url)
        return seeds or [self.base_url + "/"]

    async def _complete_json(self, system: str, prompt: str, *, max_tokens: int) -> Any:
        """One JSON-mode call; older/odd providers silently get a plain call."""
        try:
            return await self.llm.complete(
                system,
                prompt,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
        except TypeError:  # a client without the keyword (tests, other backends)
            return await self.llm.complete(system, prompt, max_tokens=max_tokens)
        except Exception as exc:
            if "400" not in str(exc):
                raise
            return await self.llm.complete(system, prompt, max_tokens=max_tokens)

    async def _decide(
        self,
        page: PageSnapshot,
        history: list[str],
        step: int,
        collected: int,
    ) -> dict[str, Any]:
        prompt = STEP_PROMPT.format(
            profile=profile_brief(self.profile),
            brief=self.brief,
            history="\n".join(history[-8:]) or "تازه شروع کرده‌ای؛ این اولین صفحه است.",
            pages=step,
            collected=collected,
            step=step,
            max_steps=self.max_steps,
            url=page.url,
            title=page.title or "—",
            text=truncate(page.text, MAX_TEXT_CHARS),
            links=render_links(page.links),
        )
        answer = await self._complete_json(AGENT_SYSTEM, prompt, max_tokens=400)
        data = parse_json_object(getattr(answer, "text", answer))
        if not data:
            raise DiscoveryError("مدل JSON معتبر برنگرداند.")
        return data

    async def _read_project(self, url: str, *, html: str | None = None) -> Project | None:
        if html is None:
            try:
                html = await self.fetch(url)
            except Exception as exc:
                log.debug("discovery: project page %s failed (%s)", url, exc)
                return None
        page = digest_html(html, url, base_url=self.base_url, max_text=MAX_DETAIL_CHARS)
        self._detail_pages += 1
        if page.is_login:
            self._login_seen = True
            return None
        prompt = EXTRACT_PROMPT.format(url=url, text=page.text)
        try:
            answer = await self._complete_json(EXTRACT_SYSTEM, prompt, max_tokens=900)
        except Exception as exc:
            log.debug("discovery: extraction failed for %s (%s)", url, exc)
            return None
        data = parse_json_object(getattr(answer, "text", answer))
        if not data or data.get("is_project") is False:
            return None
        return self._to_project(url, data, page)

    def _to_project(self, url: str, data: dict[str, Any], page: PageSnapshot) -> Project | None:
        title = clean_text(str(data.get("title") or page.title or ""))[:300]
        if not title:
            return None
        budget_text = clean_text(str(data.get("budget_text") or ""))[:120]
        deadline = clean_text(str(data.get("deadline_text") or ""))[:80]
        description = clean_text(str(data.get("description") or ""))[:6_000]
        skills = data.get("skills") or []
        if isinstance(skills, str):
            skills = re.split(r"[,،/|]", skills)
        skills = [clean_text(str(item))[:40] for item in skills if str(item).strip()][:12]

        budget = parser.parse_budget_text(budget_text) if budget_text else None
        if budget is not None and not budget.is_known:
            budget = None

        return Project(
            source=self.source_name,
            external_id=_external_id(url, title),
            title=title,
            url=url,
            description=description,
            budget=budget,
            skills=skills,
            category=clean_text(str(data.get("category") or ""))[:120] or None,
            employer=clean_text(str(data.get("employer") or ""))[:120] or None,
            employer_deadline=deadline,
            raw={
                "budget_text": budget_text,
                "deadline_text": deadline,
                "discovered_by": "ai",
            },
        )


def _short(url: str) -> str:
    parsed = urlparse(url)
    return (parsed.path or "/")[:60]


def _pick(links: list[PageLink], raw_index: Any) -> PageLink | None:
    chosen = _pick_many(links, [raw_index])
    return chosen[0] if chosen else None


def _pick_many(links: list[PageLink], raw_indexes: Any) -> list[PageLink]:
    if raw_indexes is None:
        return []
    if isinstance(raw_indexes, (int, str)):
        raw_indexes = [raw_indexes]
    if not isinstance(raw_indexes, list):
        return []
    by_index = {link.index: link for link in links}
    picked: list[PageLink] = []
    for raw in raw_indexes[:20]:
        try:
            index = int(raw)
        except (TypeError, ValueError):
            continue
        link = by_index.get(index)
        if link is not None and link not in picked:
            picked.append(link)
    return picked


__all__ = [
    "AGENT_SYSTEM",
    "DEFAULT_SITE_BRIEF",
    "DiscoveryAgent",
    "DiscoveryError",
    "DiscoveryResult",
    "PageLink",
    "PageSnapshot",
    "digest_html",
    "profile_brief",
    "render_links",
]
