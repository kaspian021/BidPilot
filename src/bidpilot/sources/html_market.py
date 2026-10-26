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
import random
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
import yaml
from bs4 import BeautifulSoup

from ..config import AppConfig, Secrets
from ..credentials import SiteCredentials, site_credentials
from ..llm_providers import build_client, resolve_chain
from ..models import Project, Proposal, SubmissionResult, SubmissionStatus
from . import html_parser as parser
from .base import AuthenticationRequiredError, JavaScriptRequiredError, Source, SourceError
from .browser_submit import submit_js_managed_form
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


def _javascript_required_message(site_notice: str = "") -> str:
    detail = (
        f"پیام سایت: {site_notice[:180]}" if site_notice else "سایت اجرای JavaScript در مرورگر را لازم دانست."
    )
    return (
        f"{detail} BidPilot در این اتصال HTTP مستقیم کار می‌کند و JavaScript "
        "اجرا نمی‌کند؛ این منبع موقتاً متوقف می‌شود و پس از مکث خودکار دوباره تلاش می‌کند. "
        "ثبت پیشنهاد تأیید نشده است."
    )


def _retry_delay(attempt: int, *, retry_after: str | None = None) -> float:
    """Exponential backoff with jitter for idempotent GET retries.

    The old fixed ``0.5 * attempt`` (<=1.5s) schedule retried a throttling
    server almost immediately, which is exactly how a polite client becomes
    a banned one (the ``RemoteProtocolError`` disconnects in production logs).
    A server-provided ``Retry-After`` always wins.
    """
    if retry_after:
        try:
            return min(60.0, max(1.0, float(retry_after)))
        except (TypeError, ValueError):
            pass  # HTTP-date form: rare here; fall through to backoff
    return min(16.0, (2.0 ** max(0, attempt - 1)) + random.uniform(0.0, 0.5))


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
        #: no AI attempts until then (a blocked provider must not be hammered
        #: every poll interval: Groq's 403 for Iranian IPs is not transient)
        self._discovery_blocked_until: float = 0.0
        #: Cache successful detail HTML across poll cycles to avoid hammering
        #: the same 25 detail pages every two minutes.
        self._detail_html_cache: dict[str, tuple[float, str]] = {}
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

    @staticmethod
    def _safe_diagnostic_url(url: str) -> str:
        """Strip query strings/fragments so diagnostic logs cannot expose tokens."""
        parsed = urlsplit(str(url))
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))

    async def get_html(self, path: str, **kwargs: Any) -> str:
        """Fetch HTML with bounded retries for transient, idempotent GET failures."""
        diagnostics = bool(
            (self.settings.get("discovery") or {}).get("log_extraction_details")
            or (self.settings.get("discovery") or {}).get("log_http_details")
        )
        requested = str(path)
        if not urlsplit(requested).scheme:
            requested = urljoin(self.base_url + "/", requested.lstrip("/"))
        safe_requested = self._safe_diagnostic_url(requested)
        params = kwargs.get("params")
        param_keys = sorted(str(key) for key in params.keys()) if hasattr(params, "keys") else []
        if diagnostics:
            log.info(
                "[HTTP][REQUEST] source=%s url=%s params_keys=%r",
                self.name,
                safe_requested,
                param_keys,
            )

        client = await self.client()
        http_cfg = self.settings.get("http") or {}
        attempts = max(1, min(4, int(http_cfg.get("get_attempts", 3))))
        response: httpx.Response | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = await client.get(path, **kwargs)
            except httpx.HTTPError as exc:
                if attempt < attempts:
                    delay = _retry_delay(attempt)
                    log.warning(
                        "[HTTP][RETRY] source=%s url=%s attempt=%s/%s error_type=%s retry_in=%.1fs",
                        self.name,
                        safe_requested,
                        attempt,
                        attempts,
                        type(exc).__name__,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                log.error(
                    "[HTTP][FAILED] source=%s url=%s attempts=%s error_type=%s",
                    self.name,
                    safe_requested,
                    attempts,
                    type(exc).__name__,
                )
                raise SourceError(
                    f"GET {safe_requested} failed after {attempts} attempts: {type(exc).__name__}"
                ) from exc

            if response.status_code == 429 or response.status_code >= 500:
                if attempt < attempts:
                    delay = _retry_delay(attempt, retry_after=response.headers.get("retry-after"))
                    log.warning(
                        "[HTTP][RETRY] source=%s url=%s attempt=%s/%s status=%s retry_in=%.1fs",
                        self.name,
                        safe_requested,
                        attempt,
                        attempts,
                        response.status_code,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
            break

        assert response is not None
        if diagnostics:
            redirects = [
                (item.status_code, self._safe_diagnostic_url(str(item.url))) for item in response.history
            ]
            log.info(
                "[HTTP][RESPONSE] source=%s status=%s requested=%s final_url=%s "
                "content_type=%r html_chars=%s redirects=%r",
                self.name,
                response.status_code,
                safe_requested,
                self._safe_diagnostic_url(str(response.url)),
                response.headers.get("content-type", ""),
                len(response.text or ""),
                redirects,
            )

        if response.status_code >= 400:
            log.warning(
                "[HTTP][FAILED] source=%s requested=%s status=%s",
                self.name,
                safe_requested,
                response.status_code,
            )
            raise SourceError(f"GET {safe_requested} -> HTTP {response.status_code}")
        return response.text

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
        client = await self.client()
        for path in dict.fromkeys(candidates):
            try:
                response = await client.get(path)
            except Exception as exc:
                errors.append(f"{path}: {type(exc).__name__}")
                continue

            final_path = urlsplit(str(response.url)).path.casefold()
            log.info(
                "[AUTH][VERIFY-RESPONSE] source=%s requested_path=%s status=%s final_path=%s html_chars=%s",
                self.name,
                urlsplit(str(path)).path,
                response.status_code,
                final_path,
                len(response.text or ""),
            )
            if response.status_code in (401, 403) or re.search(
                r"/(?:login|signin|sign-in)(?:/|$)", final_path
            ):
                return (
                    False,
                    "سایت به صفحه‌ی ورود هدایت کرد یا دسترسی را رد کرد؛ کوکی پارس‌کدرز را در «/market» به‌روز کن.",
                )
            if response.status_code >= 400:
                errors.append(f"{path}: HTTP {response.status_code}")
                continue
            html = response.text
            break
        if not html:
            return False, "صفحه‌ی بررسی باز نشد: " + "؛ ".join(errors[:2])

        lowered = (html or "").casefold()
        logged_in = [str(m).casefold() for m in (auth.get("logged_in_markers") or []) if m]
        login_only = [str(m).casefold() for m in (auth.get("login_markers") or []) if m]
        has_logged_in_marker = bool(logged_in and any(marker in lowered for marker in logged_in))
        has_login_marker = bool(login_only and any(marker in lowered for marker in login_only))

        if has_logged_in_marker:
            return True, "وارد حساب کاربری شدیم ✅"
        if has_login_marker:
            return False, "سایت صفحه‌ی ورود نشان داد؛ کوکی منقضی شده است — «/market» را بزن."
        if _looks_like_login_page(html, auth):
            return False, "سایت فرم ورود نشان داد؛ کوکی/رمز معتبر نیست — «/market» را بزن."
        if auth.get("require_auth_markers"):
            return (
                False,
                "صفحه باز شد، اما نشانه‌ی مشخص ورود به حساب پیدا نشد؛ برای جلوگیری از ارسال اشتباه، کوکی را در «/market» دوباره ثبت و سپس «/test» را اجرا کن.",
            )
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
        mode = self.discovery_mode()
        if mode in {"selectors", "off"}:
            log.debug("[DISCOVERY][DISABLED] source=%s reason=mode_%s", self.name, mode)
            return False

        remaining = self._discovery_blocked_until - time.monotonic()
        if remaining > 0:
            log.debug(
                "[DISCOVERY][DISABLED] source=%s reason=failure_cooldown remaining_seconds=%.1f",
                self.name,
                remaining,
            )
            return False

        providers = self._discovery_providers()
        if not providers:
            log.warning(
                "[DISCOVERY][DISABLED] source=%s reason=no_llm_provider llm_enabled=%s "
                "any_api_key_configured=%s",
                self.name,
                bool(getattr(self.config.llm, "enabled", False)),
                bool(self.secrets.llm_keys or self.secrets.llm_api_key),
            )
            return False
        return True

    async def _discover_with_ai(self) -> list[Project]:
        """Let the model read the site (inside this account) and bring back projects."""
        providers = self._discovery_providers()
        if not providers:
            raise DiscoveryError("هوش مصنوعی برای جست‌وجو در سایت تنظیم نشده است.")
        cfg = self.discovery_settings()
        failures: list[str] = []
        log.info(
            "[DISCOVERY][START] source=%s providers=%s seeds=%s max_steps=%s "
            "max_detail_pages=%s max_projects=%s log_extraction_details=%s",
            self.name,
            [provider.id for provider in providers],
            cfg.get("seed_paths") or ["/"],
            cfg.get("max_steps") or MAX_STEPS,
            cfg.get("max_detail_pages") or MAX_DETAIL_PAGES,
            cfg.get("max_projects") or MAX_PROJECTS,
            bool(cfg.get("log_extraction_details")),
        )
        for provider in providers:
            log.info(
                "[DISCOVERY][PROVIDER] source=%s provider=%s model=%s",
                self.name,
                provider.id,
                getattr(provider, "model", None) or self.config.llm.model,
            )
            client = build_client(provider, self.config.llm)
            budget = int(cfg.get("tokens_per_minute") or 0)
            if budget:
                client.tokens_per_minute = budget
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
            self._discovery_blocked_until = 0.0
            log.info(
                "[DISCOVERY][RESULT] source=%s provider=%s projects=%s pages_read=%s "
                "llm_calls=%s visited=%r notes=%r",
                self.name,
                provider.id,
                len(result.projects),
                result.pages_read,
                result.llm_calls,
                result.visited,
                result.notes,
            )
            for project in result.projects:
                log.debug(
                    "[DISCOVERY][PROJECT] source=%s id=%s title=%r url=%s "
                    "budget=%r raw_budget_text=%r skills=%r deadline=%r description_chars=%s",
                    project.source,
                    project.external_id,
                    project.title,
                    project.url,
                    project.budget.model_dump() if project.budget else None,
                    (project.raw or {}).get("budget_text", ""),
                    project.skills,
                    project.employer_deadline,
                    len(project.description or ""),
                )
            if result.projects:
                self.last_discovery_error = ""
                return result.projects
            log.warning(
                "[DISCOVERY][EMPTY] source=%s provider=%s visited=%r notes=%r "
                "The model finished without extracting any projects.",
                self.name,
                provider.id,
                result.visited,
                result.notes,
            )
            failures.append(f"{provider.id}: هیچ پروژه‌ای پیدا نشد")
        self.last_discovery_error = "؛ ".join(failures) or "جست‌وجوی هوشمند نتیجه نداد"
        cooldown = float(cfg.get("failure_cooldown_seconds", 900) or 0)
        if cooldown:
            self._discovery_blocked_until = time.monotonic() + cooldown
            log.info(
                "%s: AI discovery paused for %ss after a failure (selectors keep working)",
                self.name,
                int(cooldown),
            )
        raise DiscoveryError(self.last_discovery_error)

    # ------------------------------------------------------------- fetching
    async def fetch_projects(self) -> list[Project]:
        mode = self.discovery_mode()
        ai_enabled = self.ai_discovery_enabled()
        cfg = self.discovery_settings()
        log.info(
            "[FETCH][START] source=%s base_url=%s mode=%s ai_enabled=%s log_extraction_details=%s",
            self.name,
            self.base_url,
            mode,
            ai_enabled,
            bool(cfg.get("log_extraction_details")),
        )
        if ai_enabled:
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
                if self.last_discovery is not None:
                    log.warning(
                        "[DISCOVERY][FALLBACK] source=%s error=%r visited=%r notes=%r",
                        self.name,
                        str(exc),
                        self.last_discovery.visited,
                        self.last_discovery.notes,
                    )
                self.last_discovery_error = str(exc)
        else:
            log.info(
                "[FETCH][AI-BYPASSED] source=%s mode=%s reason=see_DISCOVERY_DISABLED",
                self.name,
                mode,
            )

        list_cfg = dict(self.settings.get("list") or {})
        list_cfg["_diagnostics"] = bool(cfg.get("log_extraction_details"))
        path = list_cfg.get("path", "/")
        page_param = list_cfg.get("page_param", "page")
        max_pages = int(list_cfg.get("max_pages", 1))

        projects: list[Project] = []
        seen: set[str] = set()
        log.debug(
            "[SELECTORS][START] source=%s base_url=%s list_path=%s max_pages=%s "
            "card_selectors=%r detail_mode=%s detail_path=%s",
            self.name,
            self.base_url,
            path,
            max_pages,
            list_cfg.get("card_selectors") or [],
            self.detail_mode(),
            (self.settings.get("detail") or {}).get("path", "{external_id}"),
        )
        for page in range(1, max_pages + 1):
            params = {page_param: page} if page > 1 else None
            try:
                log.debug(
                    "[SELECTORS][GET-LIST] source=%s requested=%s params=%r",
                    self.name,
                    urljoin(self.base_url + "/", str(path).lstrip("/")),
                    params,
                )
                html = await self.get_html(path, params=params)
            except Exception as exc:
                if page == 1:
                    detail = str(exc).strip() or type(exc).__name__
                    raise SourceError(f"خطا در دریافت لیست پروژه‌ها: {detail}") from exc
                log.warning("%s page %s failed: %s", self.name, page, exc)
                break
            log.debug(
                "[SELECTORS][LIST-RESPONSE] source=%s requested_path=%s html_chars=%s",
                self.name,
                path,
                len(html or ""),
            )
            page_projects = parser.parse_list_page(html, list_cfg, self.name, self.base_url)
            log.debug(
                "[SELECTORS][LIST-PARSED] source=%s page=%s projects=%s",
                self.name,
                page,
                len(page_projects),
            )
            for project in page_projects:
                if project.uid not in seen:
                    seen.add(project.uid)
                    projects.append(project)
            if len(page_projects) < int(list_cfg.get("min_items_per_page", 1)):
                break

        # "eager" keeps the historical behaviour (detail page for every card).
        # "on_demand" (preferred) defers detail pages to the pipeline, which
        # only enriches projects that already passed the filters — in the
        # production log that turned 25 detail requests per poll into ~2 and
        # stopped the marketplace from disconnecting us.
        if self.detail_mode() == "eager" and projects:
            projects = await self._attach_details(projects, int(list_cfg.get("detail_concurrency", 3)))

        projects.sort(key=lambda p: (p.published_at is not None, p.published_at), reverse=True)
        # Single concise summary for the cycle; per-card details stay at DEBUG
        log.info("📥 %s: %s پروژه دریافت شد", self.name, len(projects))
        for project in projects:
            log.debug(
                "[FETCH][FINAL-PROJECT] source=%s id=%s title=%r url=%s "
                "budget=%r raw_budget_text=%r skills=%r description_chars=%s",
                project.source,
                project.external_id,
                project.title,
                project.url,
                project.budget.model_dump() if project.budget else None,
                (project.raw or {}).get("budget_text", ""),
                project.skills,
                len(project.description or ""),
            )
        return projects

    requires_bid_status_check = True

    async def check_bid_status(self, project: Project) -> bool | None:
        """Inspect the project page before generating or posting another bid.

        Two deliberate orderings live here:

        * a *freshly cached* detail page (fetched seconds ago by
          :meth:`enrich_project` in this same cycle) is reused instead of
          downloading the identical 300 KB page again;
        * the page's actual bid state is evaluated **before** any
          "JavaScript required" banner: many sites render that banner
          server-side and hide it with JS, so a fully usable page (bid form
          present, state readable) must never be reported as JS-blocked.
          The banner only matters when the state is otherwise unreadable.
        """
        detail_cfg = self.settings.get("detail") or {}
        bid_cfg = self.settings.get("bid") or {}
        page_path = self._detail_path(project)
        html: str | None = None

        reuse_seconds = max(0.0, float(detail_cfg.get("status_reuse_seconds", 120) or 0))
        if reuse_seconds:
            cached = self._detail_html_cache.get(self._detail_cache_key(project, page_path))
            if cached and time.monotonic() - cached[0] <= reuse_seconds:
                html = cached[1]
                log.debug(
                    "[BID][STATUS-CHECK-CACHE-HIT] source=%s id=%s age_seconds=%.1f",
                    self.name,
                    project.external_id,
                    time.monotonic() - cached[0],
                )

        if html is None:
            client = await self.client()
            try:
                page = await client.get(page_path)
            except httpx.HTTPError as exc:
                log.warning(
                    "[BID][STATUS-CHECK] source=%s id=%s outcome=network_error error_type=%s",
                    self.name,
                    project.external_id,
                    type(exc).__name__,
                )
                raise SourceError(f"خطای شبکه هنگام بررسی وضعیت پیشنهاد پروژه: {type(exc).__name__}") from exc

            final_path = urlsplit(str(page.url)).path
            log.debug(
                "[BID][STATUS-CHECK-RESPONSE] source=%s id=%s status=%s final_path=%s html_chars=%s",
                self.name,
                project.external_id,
                page.status_code,
                final_path,
                len(page.text or ""),
            )
            if page.status_code in (401, 403) or re.search(
                r"/(?:login|signin|sign-in|auth/login)(?:/|$)", final_path, re.IGNORECASE
            ):
                raise AuthenticationRequiredError(
                    "نشست منقضی شده است؛ برای جلوگیری از ارسال تکراری وضعیت پیشنهاد بررسی نشد."
                )
            if page.status_code >= 400:
                raise SourceError(
                    f"صفحه پروژه برای بررسی وضعیت پیشنهاد قابل دریافت نبود (HTTP {page.status_code})."
                )
            html = page.text

        state = parser.detect_existing_bid(html, bid_cfg)
        if state is None:
            snippet = parser.javascript_requirement_snippet(html)
            if snippet:
                raise JavaScriptRequiredError(_javascript_required_message(snippet))
        outcome = "already_submitted" if state is True else "not_submitted" if state is False else "unknown"
        # Only log already_submitted at INFO (actionable); not_submitted is noise for 20+ cards
        if state is True:
            log.info("[BID] %s:%s — قبلاً پیشنهاد داده‌ایم ✓", self.name, project.external_id)
        else:
            log.debug("[BID][STATUS-CHECK] source=%s id=%s outcome=%s", self.name, project.external_id, outcome)
        return state

    # --------------------------------------------------------- detail pages
    def detail_mode(self) -> str:
        """``eager`` (detail for every card) | ``on_demand`` (only candidates) | ``off``."""
        list_cfg = self.settings.get("list") or {}
        # The legacy boolean remains a hard kill-switch for existing overrides.
        if list_cfg.get("fetch_details") is False:
            return "off"
        mode = str(list_cfg.get("detail_mode") or "").strip().lower()
        if mode in {"eager", "on_demand", "off"}:
            return mode
        # Backwards compatibility with configs that only know fetch_details.
        return "eager" if list_cfg.get("fetch_details", True) else "off"

    def _detail_path(self, project: Project) -> str:
        template = (self.settings.get("detail") or {}).get("path", "{external_id}")
        return template.format(external_id=project.external_id, url=project.url)

    @staticmethod
    def _detail_cache_key(project: Project, path: str) -> str:
        return f"{project.external_id}:{path}"

    def _detail_cache_seconds(self) -> float:
        return max(0.0, float((self.settings.get("detail") or {}).get("cache_seconds", 900) or 0))

    def _prune_detail_cache(self) -> None:
        cache_seconds = self._detail_cache_seconds()
        if not cache_seconds:
            self._detail_html_cache.clear()
            return
        now = time.monotonic()
        for key, (cached_at, _) in list(self._detail_html_cache.items()):
            if now - cached_at >= cache_seconds:
                self._detail_html_cache.pop(key, None)

    def _detail_settings(self) -> dict[str, Any]:
        detail_cfg = dict(self.settings.get("detail") or {})
        detail_cfg["_diagnostics"] = bool(self.discovery_settings().get("log_extraction_details"))
        return detail_cfg

    async def _fetch_detail_html(self, project: Project, path: str) -> str | None:
        """Detail HTML from cache or network (with a polite jittered delay)."""
        cache_seconds = self._detail_cache_seconds()
        cache_key = self._detail_cache_key(project, path)
        cached = self._detail_html_cache.get(cache_key) if cache_seconds else None
        if cached and time.monotonic() - cached[0] < cache_seconds:
            log.debug(
                "[SELECTORS][DETAIL-CACHE-HIT] source=%s id=%s age_seconds=%.1f",
                self.name,
                project.external_id,
                time.monotonic() - cached[0],
            )
            return cached[1]
        # A human does not open 25 project pages in the same second; the old
        # zero-delay burst is what earned us server disconnects.
        delay = float((self.settings.get("http") or {}).get("request_delay_seconds", 0) or 0)
        if delay > 0:
            await asyncio.sleep(delay * random.uniform(0.5, 1.5))
        try:
            html = await self.get_html(path)
        except Exception as exc:
            detail = str(exc).strip() or type(exc).__name__
            log.warning(
                "[SELECTORS][DETAIL-FAILED] source=%s id=%s path=%s error_type=%s detail=%r",
                self.name,
                project.external_id,
                self._safe_diagnostic_url(urljoin(self.base_url + "/", path.lstrip("/"))),
                type(exc).__name__,
                detail[:180],
            )
            return None
        if cache_seconds:
            self._detail_html_cache[cache_key] = (time.monotonic(), html)
        return html

    async def _enrich_one(self, project: Project, detail_cfg: dict[str, Any]) -> Project:
        path = self._detail_path(project)
        log.debug(
            "[SELECTORS][GET-DETAIL] source=%s id=%s title=%r project_url=%s "
            "configured_detail_path=%s resolved_request=%s",
            self.name,
            project.external_id,
            project.title,
            project.url,
            path,
            urljoin(self.base_url + "/", path.lstrip("/")),
        )
        html = await self._fetch_detail_html(project, path)
        if html is None:
            return project
        try:
            enriched = parser.enrich_from_detail(html, project, detail_cfg)
        except Exception:
            log.exception(
                "[SELECTORS][DETAIL-PARSE-FAILED] source=%s id=%s path=%s",
                self.name,
                project.external_id,
                path,
            )
            return project
        log.debug(
            "[SELECTORS][DETAIL-RESULT] source=%s id=%s title=%r budget_before=%r budget_after=%r",
            self.name,
            project.external_id,
            project.title,
            project.budget.model_dump() if project.budget else None,
            enriched.budget.model_dump() if enriched.budget else None,
        )
        return enriched

    async def enrich_project(self, project: Project) -> Project:
        """On-demand detail fetch for one project that passed the filters."""
        if self.detail_mode() == "off":
            return project
        self._prune_detail_cache()
        return await self._enrich_one(project, self._detail_settings())

    async def _attach_details(self, projects: list[Project], concurrency: int) -> list[Project]:
        """Eager mode: detail page for every card (legacy; prefer on_demand)."""
        detail_cfg = self._detail_settings()
        self._prune_detail_cache()
        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def enrich(project: Project) -> Project:
            async with semaphore:
                return await self._enrich_one(project, detail_cfg)

        return list(await asyncio.gather(*(enrich(p) for p in projects)))

    # -------------------------------------------------------------- bidding
    def _apply_js_cookies(self, client: httpx.AsyncClient, html: str, page_url: str) -> None:
        """Replay static ``document.cookie`` assignments found in the page."""
        cookies = parser.extract_js_cookie_assignments(html)
        if not cookies:
            return
        host = urlsplit(page_url).hostname or ""
        for name, value in cookies.items():
            try:
                client.cookies.set(name, value, domain=host)
            except Exception:  # a mock/minimal client must not break bidding
                log.debug("[BID][JS-COOKIES] could not set cookie %s", name)
        log.info(
            "[BID][JS-COOKIES] source=%s applied_names=%r host=%s",
            self.name,
            sorted(cookies),
            host,
        )

    async def _follow_proposal_cta(
        self,
        client: httpx.AsyncClient,
        html: str,
        page_url: str,
        bid_cfg: dict[str, Any],
        project: Project,
    ) -> tuple[Any, dict[str, Any], str] | None:
        """GET the «ارسال پیشنهاد» target once and look for the real form there."""
        cta_url = parser.find_proposal_cta_url(html, page_url)
        if not cta_url or urlsplit(cta_url).path == urlsplit(page_url).path:
            return None
        log.info(
            "[BID][CTA-FOLLOW] source=%s id=%s cta_path=%s",
            self.name,
            project.external_id,
            urlsplit(cta_url).path,
        )
        try:
            cta_page = await client.get(
                cta_url,
                headers={"Referer": page_url, "X-Requested-With": "XMLHttpRequest"},
            )
        except httpx.HTTPError as exc:
            log.warning(
                "[BID][CTA-FOLLOW] source=%s id=%s outcome=network_error error_type=%s",
                self.name,
                project.external_id,
                type(exc).__name__,
            )
            return None
        if cta_page.status_code >= 400:
            log.warning(
                "[BID][CTA-FOLLOW] source=%s id=%s outcome=http_%s",
                self.name,
                project.external_id,
                cta_page.status_code,
            )
            return None
        self._apply_js_cookies(client, cta_page.text, str(cta_page.url))
        form, payload = parser.find_bid_form(cta_page.text, bid_cfg)
        if form is None:
            return None
        return form, payload, str(cta_page.url)

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

        # Keep the target URL query-free in logs; the HTML size and final path
        # are enough to diagnose an empty/changed form or a login/challenge page.
        log.info(
            "[BID][PAGE-RESPONSE] source=%s id=%s status=%s final_url=%s html_chars=%s content_type=%r",
            self.name,
            project.external_id,
            page.status_code,
            str(page.url).split("?", 1)[0],
            len(page.text or ""),
            page.headers.get("content-type", ""),
        )

        if page.status_code in (301, 302, 401, 403) or "login" in str(page.url).lower():
            return self.fail("نشست منقضی شده است؛ در تلگرام «/market» کوکی را به‌روز کن.", page.status_code)
        if page.status_code >= 400:
            return self.fail(f"صفحه پروژه قابل دریافت نبود (HTTP {page.status_code}).", page.status_code)

        # Some sites gate POSTs on a trivial cookie their own JS sets
        # ("document.cookie = ...") and bounce everyone else to /login with
        # a «جاوا اسکریپت غیر فعال است» alert. Replaying those static
        # assignments is faithful browser behaviour, no JS engine needed.
        self._apply_js_cookies(client, page.text, str(page.url))

        # Functional evidence beats textual banners: many pages render a
        # "JavaScript را فعال کنید" notice server-side and hide it with JS.
        # If the real bid form is right there, the page is usable — only a
        # missing form + JS notice means the flow truly needs a browser.
        form_page_url = str(page.url)
        form, payload = parser.find_bid_form(page.text, bid_cfg)
        if form is None:
            # The form often lives behind the «ارسال پیشنهاد» button (its own
            # page or an AJAX modal). Follow that URL once before diagnosing.
            followed = await self._follow_proposal_cta(client, page.text, form_page_url, bid_cfg, project)
            if followed is not None:
                form, payload, form_page_url = followed
        if form is None:
            # A login-gated CTA is more specific/actionable than a JS banner
            # (a logged-out page can show both), so it wins the diagnosis.
            login_link = parser.find_proposal_login_link(page.text, str(page.url))
            if login_link:
                log.warning(
                    "[BID][LOGIN-GATED] source=%s id=%s proposal_action_path=%s",
                    self.name,
                    project.external_id,
                    login_link,
                )
                return self.fail(
                    "لینک «ارسال پیشنهاد» در صفحه به مسیر ورود هدایت می‌شود؛ "
                    "نشست فعلی به‌عنوان واردشده تأیید نشد. کوکی پارس‌کدرز را در «/market» "
                    "دوباره ثبت کن و با «/test» بررسی کن."
                )
            snippet = parser.javascript_requirement_snippet(page.text)
            if snippet:
                message = _javascript_required_message(snippet)
                log.warning(
                    "[BID][PAGE-RESPONSE] source=%s id=%s outcome=javascript_required notice=%r",
                    self.name,
                    project.external_id,
                    snippet,
                )
                return SubmissionResult(
                    status=SubmissionStatus.FAILED,
                    message=message,
                    http_status=page.status_code,
                    javascript_required=True,
                )
            return self.fail(
                "صفحهٔ پروژه دریافت شد، اما فرم HTML ارسال پیشنهاد شناسایی نشد؛ "
                "جزئیات فرم و لینک‌های ارسال در لاگ [BID][FORM-NOT-FOUND] ثبت شده‌اند."
            )

        mapping = parser.map_bid_fields(form, bid_cfg)
        if "description" not in mapping:
            return self.fail("فیلد متن پیشنهاد در فرم شناسایی نشد.")

        payload[mapping["description"]] = proposal.text
        if "amount" in mapping:
            payload[mapping["amount"]] = str(int(amount))
        if delivery_days and "days" in mapping:
            payload[mapping["days"]] = str(delivery_days)

        # Some sites render a JS/AJAX bid form whose real endpoint is attached
        # to the submit button (e.g. data-href), not to the <form>. Do not
        # silently POST proposal data to the project URL in that situation:
        # HTTP 200 from the project page is not evidence that a bid was saved.
        raw_action = str(form.get("action") or form.get("data-action") or form.get("data-url") or "")
        if not raw_action:
            submitter = form.select_one(
                "button[data-href], input[data-href], button[data-url], input[data-url], "
                "button[data-action], input[data-action]"
            )
            if submitter is not None:
                endpoint_attr = next(
                    (name for name in ("data-href", "data-url", "data-action") if submitter.get(name)),
                    "",
                )
                submitter_action = str(submitter.get(endpoint_attr) or "").strip() if endpoint_attr else ""
                if submitter_action:
                    endpoint = urljoin(form_page_url, submitter_action)
                    endpoint_path = urlsplit(endpoint).path
                    form_id = str(form.get("id") or "")
                    form_name = str(form.get("name") or "")
                    submitter_id = str(submitter.get("id") or "")
                    field_names = sorted(str(name) for name in payload)
                    if bool(bid_cfg.get("browser_submit")):
                        log.info(
                            "[BID][BROWSER-SUBMIT-START] source=%s id=%s endpoint_path=%s "
                            "form_id=%r submitter_id=%r fields=%r",
                            self.name,
                            project.external_id,
                            endpoint_path,
                            form_id,
                            submitter_id,
                            field_names,
                        )
                        return await submit_js_managed_form(
                            source_name=self.name,
                            base_url=self.base_url,
                            page_url=form_page_url,
                            project_id=str(project.external_id),
                            user_agent=self.config.runtime.user_agent,
                            cookies=client.cookies,
                            form_id=form_id,
                            form_name=form_name,
                            submitter_id=submitter_id,
                            endpoint_attribute=endpoint_attr,
                            endpoint_url=endpoint,
                            field_mapping=mapping,
                            proposal_text=proposal.text,
                            amount=amount,
                            delivery_days=delivery_days,
                            bid_cfg=bid_cfg,
                            auth_cfg=self.settings.get("auth") or {},
                        )
                    message = (
                        "فرم ارسال پیشنهاد action معمولی ندارد و مسیر ارسال روی دکمه‌ی submit "
                        "به‌صورت data-href/data-url/data-action تعریف شده است؛ این نشانه‌ی فرم "
                        "وابسته به JavaScript/AJAX است. برای جلوگیری از ارسال اشتباه، BidPilot "
                        "درخواست POST به URL صفحه پروژه نفرستاد. ثبت پیشنهاد انجام نشده است؛ "
                        "این فرم باید با گردش کار مرورگریِ مجاز و مطابق قوانین سایت بررسی شود."
                    )
                    log.warning(
                        "[BID][JS-AJAX-FORM-DETECTED] source=%s id=%s form_id=%r form_name=%r "
                        "form_action_present=False form_method_attr=%r submitter_id=%r "
                        "submitter_tag=%s endpoint_attribute=%s endpoint_path=%s fields=%r "
                        "has_csrf_field=%s has_key_field=%s decision=abort_without_request",
                        self.name,
                        project.external_id,
                        form_id,
                        form_name,
                        str(form.get("method") or ""),
                        submitter_id,
                        submitter.name,
                        endpoint_attr,
                        endpoint_path,
                        field_names,
                        any(name.casefold() in {"_token", "_csrf", "csrf_token"} for name in field_names),
                        any(name.casefold() == "key" for name in field_names),
                    )
                    log.error(
                        "[BID][SUBMIT-ABORT] source=%s id=%s reason=js_managed_form_no_native_action "
                        "page_path=%s submitter_endpoint_path=%s no_request_sent=true",
                        self.name,
                        project.external_id,
                        urlsplit(form_page_url).path,
                        endpoint_path,
                    )
                    return SubmissionResult(
                        status=SubmissionStatus.FAILED,
                        message=message,
                        http_status=page.status_code,
                        payload_debug={
                            "action_source": "submitter_data_attribute",
                            "endpoint_path": endpoint_path,
                            "form_id": form_id,
                            "submitter_id": submitter_id,
                            "fields": field_names,
                            "request_sent": False,
                        },
                        javascript_required=True,
                    )

        # For native HTML forms, an explicit form action is preferred. Keep the
        # legacy current-page fallback only when the page has no JS submitter
        # endpoint; the diagnostic above catches the known AJAX shape safely.
        action_source = "action" if form.get("action") else ("data-attr" if raw_action else "page-url")
        action = urljoin(form_page_url, raw_action or form_page_url)
        method = (form.get("method") or "post").upper()
        log.info(
            "[BID][SUBMIT-REQUEST] source=%s id=%s method=%s action_path=%s action_source=%s "
            "fields=%r referer_path=%s",
            self.name,
            project.external_id,
            method,
            urlsplit(action).path,
            action_source,
            sorted(payload),
            urlsplit(form_page_url).path,
        )

        submit_headers = {
            "Referer": form_page_url,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }
        if payload.get("_token"):
            submit_headers["X-CSRF-TOKEN"] = str(payload["_token"])
        if isinstance(bid_cfg.get("headers"), dict):
            submit_headers.update({str(k): str(v) for k, v in bid_cfg["headers"].items()})

        try:
            if method == "POST":
                response = await client.post(action, data=payload, headers=submit_headers)
            else:
                response = await client.get(action, params=payload, headers=submit_headers)
        except httpx.HTTPError as exc:
            return self.fail(f"خطای شبکه هنگام ارسال پیشنهاد: {exc}")

        final_path = urlsplit(str(response.url)).path
        location = str(response.headers.get("location") or "")
        location_path = urlsplit(urljoin(str(response.url), location)).path if location else ""
        redirect_history = [
            {"status": item.status_code, "path": urlsplit(str(item.url)).path}
            for item in response.history[:6]
        ]
        login_path_re = re.compile(r"/(?:login|signin|sign-in|auth/login)(?:/|$)", re.I)
        redirected_paths = [final_path, location_path, *(item["path"] for item in redirect_history)]
        notices = parser.extract_response_notices(response.text) + parser.extract_json_notices(response.text)
        # Diagnosis order matters (this exact response shape occurred in
        # production): POST -> 302 -> /login page whose visible alert says
        # «جاوا اسکریپت غیر فعال است». The old order called that "session
        # expired", hard-stopped the scheduler and sent the user on a futile
        # cookie-refresh loop while the cookie was perfectly valid. So:
        #   1) an explicit success confirmation outranks everything;
        #   2) an explicit visible JavaScript alert outranks a login redirect
        #      (the site is telling us the *reason* for the bounce);
        #   3) only then does a bare login redirect mean "session expired".
        # Subscription / daily-limit is terminal and outranks JS/login diagnosis
        if parser.requires_subscription(response.text):
            sub_msg = parser.subscription_limit_snippet(response.text)
            daily = parser.is_daily_limit_error(response.text)
            if daily:
                message = (
                    f"⛔ سقف ارسال روزانه پر شده است ({sub_msg[:120]}). "
                    "حساب رایگان پارس‌کدرز روزی ۱ پیشنهاد اجازه می‌دهد. "
                    "برای ادامه، اشتراک تهیه کنید. جست‌وجو متوقف شد."
                )
            else:
                message = (
                    f"⛔ اشتراک حساب پارس‌کدرز به پایان رسیده ({sub_msg[:120]}). "
                    "برای ارسال پیشنهاد بیشتر باید اشتراک را تمدید کنید. جست‌وجو متوقف شد."
                )
            log.warning(
                "[BID][SUBMIT-RESPONSE] source=%s id=%s outcome=subscription_required daily=%s snippet=%r",
                self.name,
                project.external_id,
                daily,
                sub_msg,
            )
            return SubmissionResult(
                status=SubmissionStatus.FAILED,
                message=message,
                http_status=response.status_code,
                subscription_required=True,
                payload_debug={"action": action, "fields": sorted(payload)},
            )

        _ok_preview, _ = parser.detect_success(response.status_code, response.text, bid_cfg)
        js_notice = next((item for item in notices if parser.requires_javascript(item)), "")
        if not _ok_preview and js_notice:
            message = _javascript_required_message(js_notice)
            log.warning(
                "[BID][SUBMIT-RESPONSE] source=%s id=%s status=%s action_path=%s final_path=%s "
                "location_path=%s redirects=%r response_chars=%s notices=%r "
                "outcome=javascript_required message=%r",
                self.name,
                project.external_id,
                response.status_code,
                urlsplit(action).path,
                final_path,
                location_path,
                redirect_history,
                len(response.text or ""),
                notices,
                message,
            )
            return SubmissionResult(
                status=SubmissionStatus.FAILED,
                message=message,
                http_status=response.status_code,
                payload_debug={"action": action, "fields": sorted(payload)},
                javascript_required=True,
            )

        if any(login_path_re.search(path) for path in redirected_paths if path):
            message = (
                "نشست منقضی شده است؛ ارسال متوقف شد. از /market ورود/کوکی را به‌روز کن و با /test بررسی کن."
            )
            log.warning(
                "[BID][SUBMIT-RESPONSE] source=%s id=%s status=%s action_path=%s final_path=%s "
                "location_path=%s redirects=%r response_chars=%s notices=%r outcome=auth_required",
                self.name,
                project.external_id,
                response.status_code,
                urlsplit(action).path,
                final_path,
                location_path,
                redirect_history,
                len(response.text or ""),
                notices,
            )
            return self.fail(message, response.status_code)

        # (Success was already ruled in/out above via _ok_preview; a
        # whole-page JS banner with no alert notice is the last JS signal.)
        if not _ok_preview and parser.requires_javascript(response.text):
            notice = next((item for item in notices if parser.requires_javascript(item)), "")
            message = _javascript_required_message(notice)
            log.warning(
                "[BID][SUBMIT-RESPONSE] source=%s id=%s status=%s method=%s action_path=%s "
                "final_path=%s location_path=%s redirects=%r content_type=%r response_chars=%s "
                "notices=%r outcome=javascript_required message=%r",
                self.name,
                project.external_id,
                response.status_code,
                method,
                urlsplit(action).path,
                final_path,
                location_path,
                redirect_history,
                response.headers.get("content-type", ""),
                len(response.text or ""),
                notices,
                message,
            )
            return SubmissionResult(
                status=SubmissionStatus.FAILED,
                message=message,
                http_status=response.status_code,
                payload_debug={"action": action, "fields": sorted(payload)},
                javascript_required=True,
            )

        ok, message = parser.detect_success(response.status_code, response.text, bid_cfg)
        log_method = log.info if ok else log.warning
        log_method(
            "[BID][SUBMIT-RESPONSE] source=%s id=%s status=%s method=%s action_path=%s "
            "final_path=%s location_path=%s redirects=%r content_type=%r response_chars=%s "
            "notices=%r outcome=%s message=%r",
            self.name,
            project.external_id,
            response.status_code,
            method,
            urlsplit(action).path,
            final_path,
            location_path,
            redirect_history,
            response.headers.get("content-type", ""),
            len(response.text or ""),
            notices,
            "confirmed_success" if ok else "not_confirmed",
            message,
        )
        # An arbitrary redirect is not proof that the proposal was registered.
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
