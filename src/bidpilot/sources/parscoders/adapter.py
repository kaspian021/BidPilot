"""Parscoders source adapter."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
import yaml

from ...config import AppConfig, Secrets
from ...models import Project, Proposal, SubmissionResult, SubmissionStatus
from ..base import Source, SourceError
from ..registry import register_source
from . import parser

log = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).with_name("config.yaml")
_AMOUNT_UNIT_MULTIPLIER = 1  # values are already in Toman; keep as int


def load_default_settings() -> dict[str, Any]:
    with _CONFIG_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def parse_cookie_header(raw: str | None) -> dict[str, str]:
    """Parse a browser cookie header ("a=1; b=2") or a bare session id."""
    if not raw:
        return {}
    raw = raw.strip()
    if "=" not in raw:
        return {"PHPSESSID": raw}
    cookies: dict[str, str] = {}
    for chunk in raw.split(";"):
        if "=" in chunk:
            key, _, value = chunk.partition("=")
            cookies[key.strip()] = value.strip()
    return cookies


@register_source("parscoders")
class ParscodersSource(Source):
    name = "parscoders"

    def __init__(
        self,
        settings: dict[str, Any],
        config: AppConfig,
        secrets: Secrets,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        merged = load_default_settings()
        for key, value in (settings or {}).items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
        super().__init__(merged, config, secrets, client)

        cookie = self.secrets.parscoders_cookie or self.settings.get("cookie", "")
        self._session_cookies = parse_cookie_header(cookie)

    # ------------------------------------------------------------------ http
    def cookies(self) -> dict[str, str]:
        """Config cookies + the session cookie from PARSOCODERS_COOKIE."""
        cookies = super().cookies()
        cookies.update(getattr(self, "_session_cookies", {}) or {})
        return cookies

    # ------------------------------------------------------------------ auth
    def credentials_present(self) -> bool:
        mode = (self.settings.get("auth") or {}).get("mode", "cookie")
        if mode == "credentials":
            return bool(self.secrets.parscoders_username and self.secrets.parscoders_password)
        return bool(self._session_cookies)

    async def login(self) -> bool:
        """Cookie mode uses the stored session; credentials mode performs the login POST."""
        auth = self.settings.get("auth") or {}
        if auth.get("mode") != "credentials":
            return bool(self._session_cookies)
        if not (self.secrets.parscoders_username and self.secrets.parscoders_password):
            return False
        client = await self.client()
        data = {
            auth.get("username_field", "username"): self.secrets.parscoders_username,
            auth.get("password_field", "password"): self.secrets.parscoders_password,
        }
        response = await client.post(auth.get("login_path", "/login"), data=data)
        ok = response.status_code < 400
        log.info("Parscoders login status=%s ok=%s", response.status_code, ok)
        return ok

    async def healthcheck(self) -> tuple[bool, str]:
        try:
            html = await self.get_html((self.settings.get("list") or {}).get("path", "/project"))
        except Exception as exc:  # network / block / 5xx
            return False, f"دسترسی به لیست پروژه‌ها ناموفق بود: {exc}"
        projects = parser.parse_list_page(html, self.settings.get("list") or {}, self.name, self.base_url)
        if not projects:
            return False, "صفحه لیست باز شد اما هیچ پروژه‌ای تشخیص داده نشد (سلکتورها را بررسی کنید)."
        return True, f"{len(projects)} پروژه در صفحه اول پیدا شد."

    # -------------------------------------------------------------- fetching
    async def fetch_projects(self) -> list[Project]:
        list_cfg = self.settings.get("list") or {}
        path = list_cfg.get("path", "/project")
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
                log.warning("Parscoders page %s failed: %s", page, exc)
                break
            page_projects = parser.parse_list_page(html, list_cfg, self.name, self.base_url)
            for project in page_projects:
                if project.uid not in seen:
                    seen.add(project.uid)
                    projects.append(project)
            if len(page_projects) < 10:  # last page
                break

        if list_cfg.get("fetch_details", True) and projects:
            projects = await self._attach_details(projects, int(list_cfg.get("detail_concurrency", 3)))

        projects.sort(key=lambda p: (p.published_at is not None, p.published_at), reverse=True)
        log.info("Parscoders: %s projects fetched", len(projects))
        return projects

    async def _attach_details(self, projects: list[Project], concurrency: int) -> list[Project]:
        detail_cfg = self.settings.get("detail") or {}
        template = detail_cfg.get("path", "/project/{external_id}")
        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def enrich(project: Project) -> Project:
            path = template.format(external_id=project.external_id)
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

    # --------------------------------------------------------------- bidding
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
            return self.fail("اعتبارنامه پارسکدرز تنظیم نشده است. PARSOCODERS_COOKIE را در .env قرار دهید.")

        bid_cfg = self.settings.get("bid") or {}
        detail_path = (self.settings.get("detail") or {}).get("path", "/project/{external_id}")
        page_path = detail_path.format(external_id=project.external_id)

        client = await self.client()
        try:
            if bid_cfg.get("pre_submit_get"):
                await client.get(bid_cfg["pre_submit_get"])
            page = await client.get(page_path)
        except httpx.HTTPError as exc:
            return self.fail(f"خطای شبکه هنگام باز کردن صفحه پروژه: {exc}")

        if page.status_code in (301, 302, 401, 403) or "login" in str(page.url).lower():
            return self.fail("نشست پارسکدرز منقضی شده است؛ کوکی را به‌روزرسانی کنید.", page.status_code)
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
