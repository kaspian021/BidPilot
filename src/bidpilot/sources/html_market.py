"""Config-driven HTML marketplace adapter — the base every site shares.

A marketplace is described by ``<package>/config.yaml``:

* ``base_url`` and the listing path/pagination,
* ``card_selectors`` + ``fields`` (title, link, budget, skills, …),
* ``detail`` selectors for the full description,
* ``bid`` form selectors, success/failure markers,
* ``auth`` (cookie / credentials / none).

The code below never mentions a specific site: adding a marketplace is a YAML
file plus a three-line subclass, and switching between them happens in Telegram
(``/site``). Credentials come from :mod:`bidpilot.credentials`, so every site has
its own account stored under ``credentials.<site>.*``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
import yaml
from bs4 import BeautifulSoup

from ..config import AppConfig, Secrets
from ..credentials import SiteCredentials, site_credentials
from ..llm_providers import build_client, resolve_chain
from ..models import Project, Proposal, SubmissionResult, SubmissionStatus
from . import html_parser as parser
from .base import Source, SourceError
from .discovery import (
    MAX_DETAIL_PAGES,
    MAX_PROJECTS,
    MAX_STEPS,
    DiscoveryAgent,
    DiscoveryError,
    DiscoveryResult,
)

log = logging.getLogger(__name__)


def load_yaml_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def merge_settings(defaults: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = dict(defaults)
    for key, value in (overrides or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


class HtmlMarketSource(Source):
    """Fetch projects and post bids on any HTML marketplace."""

    #: file with the site's selectors; subclasses point at their package config
    config_file: Path = Path("config.yaml")
    #: settings fallback when nothing else is configured
    default_base_url: str = ""

    def __init__(
        self,
        settings: dict[str, Any],
        config: AppConfig,
        secrets: Secrets,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        merged = merge_settings(load_yaml_settings(self.config_file), settings or {})
        if not merged.get("base_url") and self.default_base_url:
            merged["base_url"] = self.default_base_url
        super().__init__(merged, config, secrets, client)
        #: what the AI discovery did on the last run (shown by /preview and the digest)
        self.last_discovery: DiscoveryResult | None = None
        self.last_discovery_error: str = ""
        self._llm_clients: list[Any] = []
        #: the last AI discovery, reused for a while so a 2-minute poll loop
        #: cannot burn a free tier with the same pages over and over
        self._last_projects: list[Project] = []
        self._last_fetch_at: float = 0.0
        legacy = secrets.model_dump() if hasattr(secrets, "model_dump") else {}
        self.credentials: SiteCredentials = site_credentials(
            self.name,
            legacy=legacy,
            method_default=str((self.settings.get("auth") or {}).get("mode") or "cookie"),
        )

    # ------------------------------------------------------------- settings
    def site_config(self) -> dict[str, Any]:
        return self.settings

    # ------------------------------------------------------------ auth/http
    def cookies(self) -> dict[str, str]:
        """Cookies from config + the stored session cookie (per site)."""
        cookies = super().cookies()
        cookies.update(self.credentials.cookies)
        return cookies

    def headers(self) -> dict[str, str]:
        headers = super().headers()
        headers.update(self.credentials.headers or {})
        return headers

    def credentials_present(self) -> bool:
        auth = self.settings.get("auth") or {}
        if auth.get("mode") == "none":
            return True
        if self.credentials.ready:
            return True
        if auth.get("mode") in (None, "cookie"):
            return bool(self.credentials.cookies) or bool(auth.get("allow_anonymous"))
        return False

    async def login(self) -> bool:
        """Cookie mode needs nothing; credentials mode posts the login form."""
        auth = self.settings.get("auth") or {}
        mode = auth.get("mode", "cookie")
        if mode != "credentials":
            return bool(self.credentials.cookies) or bool(auth.get("allow_anonymous"))
        if not (self.credentials.username and self.credentials.password):
            return False

        client = await self.client()
        data: dict[str, str] = {
            auth.get("username_field", "username"): self.credentials.username,
            auth.get("password_field", "password"): self.credentials.password,
        }
        login_path = auth.get("login_path", "/login")
        try:
            if auth.get("parse_hidden", True):
                page = await client.get(login_path)
                data.update(_hidden_inputs(page.text))
            response = await client.post(
                auth.get("submit_path") or login_path,
                data=data,
                headers={"Referer": urljoin(self.base_url + "/", login_path.lstrip("/"))},
            )
        except httpx.HTTPError as exc:
            log.warning("%s login failed: %s", self.name, exc)
            return False

        ok = response.status_code < 400 and not _looks_like_login_page(response.text, auth)
        log.info("%s login status=%s ok=%s", self.name, response.status_code, ok)
        if ok:
            # keep the refreshed session cookies for the rest of the run
            self.credentials = SiteCredentials(
                site=self.credentials.site,
                method=self.credentials.method,
                cookie=self.credentials.cookie,
                username=self.credentials.username,
                password=self.credentials.password,
                headers=self.credentials.headers,
                cookie_name=self.credentials.cookie_name,
            )
        return ok

    async def verify_session(self) -> tuple[bool, str]:
        """Check the stored account by opening a page only a logged-in user sees.

        Cookie mode cannot be *proved* without a request, so we fetch
        ``auth.check_path`` and look for the site's own markers. When the page is
        inconclusive we say so instead of blocking the user.
        """
        auth = self.settings.get("auth") or {}
        if auth.get("mode") == "none":
            return True, "این سایت ورود لازم ندارد."
        if not self.credentials.ready:
            return False, "کوکی/نام کاربری ثبت نشده است — «/market» را بزن."

        if auth.get("mode") == "credentials":
            ok, detail = await self.login()
            if not ok:
                return False, detail or "لاگین با نام کاربری/رمز ناموفق بود (کپچا یا رمز اشتباه)."

        html = ""
        errors: list[str] = []
        candidates = [auth.get("check_path") or "/"]
        if candidates[0] != "/":
            candidates.append("/")  # a guessed dashboard path must not be fatal
        for path in dict.fromkeys(candidates):
            try:
                html = await self.get_html(path)
                break
            except Exception as exc:
                errors.append(f"{path}: {exc}")
        if not html:
            return False, "صفحه‌ی بررسی باز نشد: " + "؛ ".join(errors[:2])

        lowered = (html or "").casefold()
        logged_in = [str(m).casefold() for m in (auth.get("logged_in_markers") or []) if m]
        login_only = [str(m).casefold() for m in (auth.get("login_markers") or []) if m]

        if logged_in and any(marker in lowered for marker in logged_in):
            return True, "وارد حساب کاربری شدیم ✅"
        if login_only and any(marker in lowered for marker in login_only):
            if not (logged_in and any(marker in lowered for marker in logged_in)):
                return False, "سایت صفحه‌ی ورود نشان داد؛ کوکی منقضی شده است — «/market» را بزن."
        if _looks_like_login_page(html, auth):
            return False, "سایت فرم ورود نشان داد؛ کوکی/رمز معتبر نیست — «/market» را بزن."
        return True, "صفحه باز شد و نشانه‌ی «ورود لازم است» نداشت (وضعیت قطعی نیست)."

    # -------------------------------------------------------------- health
    async def healthcheck(self) -> tuple[bool, str]:
        list_cfg = self.settings.get("list") or {}
        try:
            html = await self.get_html(list_cfg.get("path", "/"))
        except Exception as exc:
            return False, f"دسترسی به لیست پروژه‌ها ناموفق بود: {exc}"
        projects = parser.parse_list_page(html, list_cfg, self.name, self.base_url)
        if not projects:
            return (
                False,
                "صفحه لیست باز شد اما هیچ پروژه‌ای تشخیص داده نشد "
                "(سلکتورها را با «bidpilot inspect» بررسی کنید).",
            )
        return True, f"{len(projects)} پروژه در صفحه اول پیدا شد."

    # -------------------------------------------------------- AI discovery
    def discovery_settings(self) -> dict[str, Any]:
        cfg = self.settings.get("discovery")
        return cfg if isinstance(cfg, dict) else {}

    def discovery_mode(self) -> str:
        mode = str(self.discovery_settings().get("mode") or "auto").strip().lower()
        return mode if mode in {"ai", "selectors", "auto", "off"} else "auto"

    def _discovery_providers(self) -> list[Any]:
        """The same free-provider chain the proposals use (key required)."""
        llm = self.config.llm
        if not getattr(llm, "enabled", False):
            return []
        try:
            return resolve_chain(llm, self.secrets.llm_keys, self.secrets.llm_api_key)
        except Exception as exc:  # broken llm config must not break fetching
            log.debug("%s: no discovery provider (%s)", self.name, exc)
            return []

    def ai_discovery_enabled(self) -> bool:
        if self.discovery_mode() in {"selectors", "off"}:
            return False
        return bool(self._discovery_providers())

    async def _discover_with_ai(self) -> list[Project]:
        """Let the model read the site (inside this account) and bring back projects."""
        providers = self._discovery_providers()
        if not providers:
            raise DiscoveryError("هوش مصنوعی برای جست‌وجو در سایت تنظیم نشده است.")
        cfg = self.discovery_settings()
        failures: list[str] = []
        for provider in providers:
            client = build_client(provider, self.config.llm)
            self._llm_clients.append(client)
            agent = DiscoveryAgent(
                source_name=self.name,
                base_url=self.base_url,
                fetch=self.get_html,
                llm=client,
                profile=self.config.profile,
                brief=str(cfg.get("brief") or ""),
                seed_paths=[str(p) for p in (cfg.get("seed_paths") or ["/"])],
                max_steps=int(cfg.get("max_steps") or MAX_STEPS),
                max_detail_pages=int(cfg.get("max_detail_pages") or MAX_DETAIL_PAGES),
                max_projects=int(cfg.get("max_projects") or MAX_PROJECTS),
                page_delay=float(cfg.get("page_delay_seconds") or 0),
            )
            try:
                result = await agent.discover()
            except Exception as exc:
                failures.append(f"{provider.id}: {exc}")
                log.warning("%s: AI discovery via %s failed: %s", self.name, provider.id, exc)
                continue
            self.last_discovery = result
            if result.projects:
                self.last_discovery_error = ""
                return result.projects
            failures.append(f"{provider.id}: هیچ پروژه‌ای پیدا نشد")
        self.last_discovery_error = "؛ ".join(failures) or "جست‌وجوی هوشمند نتیجه نداد"
        raise DiscoveryError(self.last_discovery_error)

    # ------------------------------------------------------------- fetching
    async def fetch_projects(self) -> list[Project]:
        if self.ai_discovery_enabled():
            cfg = self.discovery_settings()
            reuse = float(cfg.get("reuse_seconds", 600) or 0)
            if self._last_projects and (time.monotonic() - self._last_fetch_at) < reuse:
                log.debug("%s: reusing the last AI discovery (%ss old)", self.name, reuse)
                return list(self._last_projects)
            try:
                found = await self._discover_with_ai()
                found.sort(key=lambda p: (p.published_at is not None, p.published_at), reverse=True)
                log.info("%s: %s projects discovered by AI", self.name, len(found))
                self._last_projects = list(found)
                self._last_fetch_at = time.monotonic()
                return found
            except Exception as exc:
                # the model is a helper, not a single point of failure
                log.warning("%s: AI discovery failed (%s); using selectors", self.name, exc)
                self.last_discovery_error = str(exc)

        list_cfg = self.settings.get("list") or {}
        path = list_cfg.get("path", "/")
        page_param = list_cfg.get("page_param", "page")
        max_pages = int(list_cfg.get("max_pages", 1))

        projects: list[Project] = []
        seen: set[str] = set()
        for page in range(1, max_pages + 1):
            params = {page_param: page} if page > 1 else None
            try:
                html = await self.get_html(path, params=params)
            except Exception as exc:
                if page == 1:
                    raise SourceError(f"خطا در دریافت لیست پروژه‌ها: {exc}") from exc
                log.warning("%s page %s failed: %s", self.name, page, exc)
                break
            page_projects = parser.parse_list_page(html, list_cfg, self.name, self.base_url)
            for project in page_projects:
                if project.uid not in seen:
                    seen.add(project.uid)
                    projects.append(project)
            if len(page_projects) < int(list_cfg.get("min_items_per_page", 1)):
                break

        if list_cfg.get("fetch_details", True) and projects:
            projects = await self._attach_details(projects, int(list_cfg.get("detail_concurrency", 3)))

        projects.sort(key=lambda p: (p.published_at is not None, p.published_at), reverse=True)
        log.info("%s: %s projects fetched", self.name, len(projects))
        return projects

    async def _attach_details(self, projects: list[Project], concurrency: int) -> list[Project]:
        detail_cfg = self.settings.get("detail") or {}
        template = detail_cfg.get("path", "{external_id}")
        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def enrich(project: Project) -> Project:
            path = template.format(external_id=project.external_id, url=project.url)
            async with semaphore:
                try:
                    html = await self.get_html(path)
                except Exception as exc:
                    log.debug("detail fetch failed for %s: %s", project.uid, exc)
                    return project
            try:
                return parser.enrich_from_detail(html, project, detail_cfg)
            except Exception as exc:
                log.debug("detail parse failed for %s: %s", project.uid, exc)
                return project

        return list(await asyncio.gather(*(enrich(p) for p in projects)))

    # -------------------------------------------------------------- bidding
    async def submit_bid(
        self,
        project: Project,
        proposal: Proposal,
        amount: int,
        delivery_days: int | None,
    ) -> SubmissionResult:
        if not self.credentials_present():
            await self.login()
        if not self.credentials_present():
            return self.fail(
                f"اعتبارنامه {self.name} تنظیم نشده است؛ در تلگرام «/market» را بزن "
                "و کوکی یا نام کاربری/رمز را وارد کن."
            )

        bid_cfg = self.settings.get("bid") or {}
        detail_path = (self.settings.get("detail") or {}).get("path", "{external_id}")
        page_path = detail_path.format(external_id=project.external_id, url=project.url)

        client = await self.client()
        try:
            if bid_cfg.get("pre_submit_get"):
                await client.get(bid_cfg["pre_submit_get"])
            page = await client.get(page_path)
        except httpx.HTTPError as exc:
            return self.fail(f"خطای شبکه هنگام باز کردن صفحه پروژه: {exc}")

        if page.status_code in (301, 302, 401, 403) or "login" in str(page.url).lower():
            return self.fail("نشست منقضی شده است؛ در تلگرام «/market» کوکی را به‌روز کن.", page.status_code)
        if page.status_code >= 400:
            return self.fail(f"صفحه پروژه قابل دریافت نبود (HTTP {page.status_code}).", page.status_code)

        form, payload = parser.find_bid_form(page.text, bid_cfg)
        if form is None:
            return self.fail(
                "فرم ارسال پیشنهاد پیدا نشد (احتمالاً قبلاً پیشنهاد داده‌اید یا سلکتورها به‌روز نیستند)."
            )

        mapping = parser.map_bid_fields(form, bid_cfg)
        if "description" not in mapping:
            return self.fail("فیلد متن پیشنهاد در فرم شناسایی نشد.")

        payload[mapping["description"]] = proposal.text
        if "amount" in mapping:
            payload[mapping["amount"]] = str(int(amount))
        if delivery_days and "days" in mapping:
            payload[mapping["days"]] = str(delivery_days)

        action = urljoin(str(page.url), form.get("action") or str(page.url))
        method = (form.get("method") or "post").upper()

        try:
            if method == "POST":
                response = await client.post(action, data=payload, headers={"Referer": str(page.url)})
            else:
                response = await client.get(action, params=payload, headers={"Referer": str(page.url)})
        except httpx.HTTPError as exc:
            return self.fail(f"خطای شبکه هنگام ارسال پیشنهاد: {exc}")

        ok, message = parser.detect_success(response.status_code, response.text, bid_cfg)
        if not ok and response.is_redirect:
            ok, message = True, "درخواست ارسال شد (ریدایرکت موفق)."
        return SubmissionResult(
            status=SubmissionStatus.SUBMITTED if ok else SubmissionStatus.FAILED,
            message=message,
            http_status=response.status_code,
            payload_debug={"action": action, "fields": sorted(payload)},
        )

    async def aclose(self) -> None:  # type: ignore[override]
        for client in self._llm_clients:
            try:
                await client.aclose()
            except Exception:  # pragma: no cover - closing must never raise
                pass
        self._llm_clients.clear()
        await super().aclose()


def _hidden_inputs(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "lxml")
    values: dict[str, str] = {}
    for node in soup.select("input[type=hidden][name]"):
        name = node.get("name")
        if name:
            values[str(name)] = str(node.get("value") or "")
    return values


def _looks_like_login_page(html: str, auth: dict[str, Any]) -> bool:
    markers = auth.get("failure_text_contains") or ["رمز عبور", "کپچا", "captcha", "ورود ناموفق"]
    lowered = (html or "").lower()
    return any(str(marker).lower() in lowered for marker in markers) and (
        auth.get("password_field", "password") in (html or "")
    )


__all__ = ["HtmlMarketSource", "load_yaml_settings", "merge_settings"]
