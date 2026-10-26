"""Submit JavaScript-managed marketplace forms through their real browser UI.

This module deliberately clicks the site's own controls; it does not reproduce
or guess private AJAX payloads, forge JavaScript state, or bypass challenges.
Playwright is an optional extra so non-browser sources do not need Chromium.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any
from urllib.parse import urljoin, urlsplit

from ..models import SubmissionResult, SubmissionStatus
from . import html_parser

log = logging.getLogger(__name__)


def _origin(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    return parsed.scheme.casefold(), parsed.netloc.casefold()


def _attr_selector(name: str, value: str) -> str:
    """Build a quoted CSS attribute selector without interpolating raw markup."""
    return f"[{name}={json.dumps(value)}]"


def _browser_cookies(cookies: Any, base_url: str) -> list[dict[str, Any]]:
    """Copy only cookies belonging to this site's domain into Playwright."""
    parsed = urlsplit(base_url)
    host = (parsed.hostname or "").casefold()
    jar = getattr(cookies, "jar", cookies)
    result: list[dict[str, Any]] = []

    try:
        items = list(jar)
    except TypeError:
        return result

    for cookie in items:
        name = str(getattr(cookie, "name", "") or "")
        value = str(getattr(cookie, "value", "") or "")
        raw_domain = str(getattr(cookie, "domain", "") or host).casefold()
        domain = raw_domain.lstrip(".")
        if not name or not domain or not host:
            continue
        # Never carry unrelated-domain cookies into the browser context.
        if domain.count(".") < 1 or not (host == domain or host.endswith("." + domain)):
            continue
        expires = getattr(cookie, "expires", None)
        if expires is not None:
            try:
                if float(expires) <= time.time():
                    continue
            except (TypeError, ValueError):
                expires = None

        spec: dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": raw_domain or domain,
            "path": str(getattr(cookie, "path", "") or "/"),
            "secure": bool(getattr(cookie, "secure", False)),
        }
        if expires is not None:
            spec["expires"] = float(expires)

        rest = getattr(cookie, "_rest", {}) or {}
        if any(str(key).casefold() == "httponly" for key in rest):
            spec["httpOnly"] = True
        same_site = next(
            (
                str(val).capitalize()
                for key, val in rest.items()
                if str(key).casefold() == "samesite" and str(val).casefold() in {"strict", "lax", "none"}
            ),
            None,
        )
        if same_site:
            spec["sameSite"] = same_site
        result.append(spec)
    return result


def _failure(
    message: str,
    *,
    status: int | None = None,
    pause_source: bool = False,
    debug: dict[str, Any] | None = None,
) -> SubmissionResult:
    return SubmissionResult(
        status=SubmissionStatus.FAILED,
        message=message,
        http_status=status,
        javascript_required=pause_source,
        payload_debug=debug or {},
    )


async def submit_js_managed_form(
    *,
    source_name: str,
    base_url: str,
    page_url: str,
    project_id: str,
    cookies: Any,
    form_id: str,
    form_name: str,
    submitter_id: str,
    endpoint_attribute: str,
    endpoint_url: str,
    field_mapping: dict[str, str],
    proposal_text: str,
    amount: int,
    delivery_days: int | None,
    bid_cfg: dict[str, Any],
    auth_cfg: dict[str, Any],
) -> SubmissionResult:
    """Open the project in Chromium and click the form's actual submit button."""
    page_parts = urlsplit(page_url)
    endpoint_parts = urlsplit(endpoint_url)
    safe_debug = {
        "browser": "playwright",
        "form_id": form_id,
        "submitter_id": submitter_id,
        "endpoint_path": endpoint_parts.path,
        "request_sent": False,
    }

    if not page_parts.scheme or _origin(page_url) != _origin(base_url):
        return _failure(
            "آدرس صفحه پروژه با دامنه اصلی منبع یکسان نیست؛ برای جلوگیری از ارسال ناخواسته متوقف شد.",
            debug=safe_debug,
        )
    if endpoint_parts.scheme and _origin(endpoint_url) != _origin(page_url):
        return _failure(
            "مسیر ارسال فرم به دامنه دیگری اشاره می‌کند؛ مرورگر هیچ درخواستی نفرستاد.",
            debug=safe_debug,
        )
    if not endpoint_parts.path.strip("/"):
        return _failure("مسیر ارسال فرم خالی است؛ هیچ درخواستی فرستاده نشد.", debug=safe_debug)
    if source_name.casefold() == "parscoders":
        expected_path = f"/conversation/ajax/bid-send/{project_id}"
        if endpoint_parts.path.rstrip("/") != expected_path:
            return _failure(
                "مسیر دکمه با مسیر ارسال مورد انتظار این پروژه یکی نیست؛ هیچ درخواستی فرستاده نشد.",
                debug=safe_debug,
            )
    if endpoint_attribute not in {"data-href", "data-url", "data-action"}:
        return _failure("ویژگی مسیر دکمه شناخته‌شده نیست؛ هیچ درخواستی فرستاده نشد.", debug=safe_debug)
    if not field_mapping.get("description") or not field_mapping.get("amount"):
        return _failure("فیلد مبلغ یا متن پیشنهاد مشخص نیست؛ هیچ درخواستی فرستاده نشد.", debug=safe_debug)

    try:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        from playwright.async_api import async_playwright
    except ImportError:
        return _failure(
            "برای فرم JavaScript پارس‌کدرز، افزونه مرورگر نصب نیست. در محیط مجازی اجرا کن: "
            'python -m pip install -e ".[browser]" و سپس python -m playwright install chromium. '
            "تا نصب مرورگر، پیشنهاد ثبت نشده است.",
            pause_source=True,
            debug=safe_debug,
        )

    browser = None
    context = None
    action_clicked = False
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context(locale="fa-IR")
            cookie_specs = _browser_cookies(cookies, base_url)
            if cookie_specs:
                await context.add_cookies(cookie_specs)
            page = await context.new_page()
            page.set_default_timeout(12_000)
            navigation = await page.goto(page_url, wait_until="domcontentloaded", timeout=25_000)
            if navigation is not None and navigation.status >= 400:
                return _failure(
                    f"مرورگر نتوانست صفحه پروژه را باز کند (HTTP {navigation.status}).",
                    status=navigation.status,
                    debug=safe_debug,
                )

            current_url = str(page.url)
            current_path = urlsplit(current_url).path.casefold()
            if re.search(r"/(?:login|signin|sign-in|auth/login)(?:/|$)", current_path):
                return _failure(
                    "نشست مرورگر به صفحه ورود هدایت شد؛ کوکی را از طریق /market تازه کن و /test را اجرا کن.",
                    debug=safe_debug,
                )

            body_text = await page.locator("body").inner_text(timeout=12_000)
            lowered = body_text.casefold()
            logged_markers = [
                str(item).casefold() for item in (auth_cfg.get("logged_in_markers") or []) if item
            ]
            login_markers = [str(item).casefold() for item in (auth_cfg.get("login_markers") or []) if item]
            if (
                logged_markers
                and not any(marker in lowered for marker in logged_markers)
                and any(marker in lowered for marker in login_markers)
            ):
                return _failure(
                    "ورود در نشست مرورگر تأیید نشد؛ هیچ پیشنهاد ارسالی انجام نشد. کوکی را در /market تازه کن.",
                    debug=safe_debug,
                )
            challenge_words = ("captcha", "cloudflare", "access denied", "تأیید کنید ربات نیستید")
            if any(word in lowered for word in challenge_words):
                return _failure(
                    "سایت کپچا یا صفحه محدودیت نمایش داده است؛ گردش خودکار متوقف شد و این کنترل دور زده نمی‌شود.",
                    pause_source=True,
                    debug=safe_debug,
                )

            if form_id:
                form_selector = f"form{_attr_selector('id', form_id)}"
            elif form_name:
                form_selector = f"form{_attr_selector('name', form_name)}"
            else:
                amount_selector = _attr_selector("name", field_mapping["amount"])
                text_selector = _attr_selector("name", field_mapping["description"])
                form_selector = f"form:has({amount_selector}):has({text_selector})"
            form = page.locator(form_selector).first
            if await form.count() != 1:
                return _failure(
                    "فرم مورد انتظار در صفحه مرورگر پیدا نشد؛ هیچ پیشنهادی ارسال نشد.",
                    debug=safe_debug,
                )

            # Parscoders keeps the proposal form inside a hidden off-canvas.
            # Open it via the visible site's own CTA before filling it.
            if not await form.is_visible():
                cta_selector = (
                    "button.bid-send--button"
                    + _attr_selector("data-project-id", str(project_id))
                    + ":visible"
                )
                cta = page.locator(cta_selector).first
                if await cta.count() == 0:
                    return _failure(
                        "فرم پنهان است اما دکمه اصلی «ارسال پیشنهاد» برای بازکردنش پیدا نشد؛ "
                        "هیچ پیشنهادی ارسال نشد.",
                        debug=safe_debug,
                    )
                await cta.click()
                await form.wait_for(state="visible", timeout=12_000)

            async def fill_field(logical_name: str, value: str) -> None:
                field_name = field_mapping.get(logical_name)
                if not field_name:
                    return
                control = form.locator(_attr_selector("name", field_name))
                if await control.count() != 1:
                    raise ValueError(f"mapped field missing or ambiguous: {logical_name}")
                properties = await control.evaluate(
                    "(el) => ({tag: el.tagName.toLowerCase(), type: (el.type || '').toLowerCase(), "
                    "disabled: Boolean(el.disabled), readOnly: Boolean(el.readOnly)})"
                )
                if properties["disabled"] or properties["readOnly"] or properties["type"] == "hidden":
                    raise ValueError(f"mapped field is not editable: {logical_name}")
                if properties["tag"] == "select":
                    await control.select_option(value=value)
                else:
                    await control.fill(value)
                    await control.dispatch_event("change")

            try:
                await fill_field("amount", str(int(amount)))
                if delivery_days is not None:
                    await fill_field("days", str(int(delivery_days)))
                await fill_field("description", proposal_text)
            except Exception as exc:
                log.warning(
                    "[BID][BROWSER-FORM] source=%s id=%s outcome=field_fill_failed error_type=%s",
                    source_name,
                    project_id,
                    type(exc).__name__,
                )
                return _failure(
                    "مرورگر نتوانست یکی از فیلدهای پیشنهاد را با اطمینان تکمیل کند؛ "
                    "هیچ کلیکی برای ثبت پیشنهاد انجام نشد.",
                    debug=safe_debug,
                )

            form_valid = await form.evaluate("(el) => el.checkValidity()")
            if not form_valid:
                return _failure(
                    "فرم مرورگر پس از تکمیل فیلدها معتبر نیست؛ برای جلوگیری از ارسال ناقص متوقف شد.",
                    debug=safe_debug,
                )

            if submitter_id:
                submitter = form.locator(_attr_selector("id", submitter_id)).first
            else:
                submitter = form.locator(
                    _attr_selector(endpoint_attribute, urlsplit(endpoint_url).path)
                ).first
            if await submitter.count() == 0 or not await submitter.is_visible():
                return _failure(
                    "دکمه اصلی ارسال در فرم مرورگر پیدا یا قابل‌مشاهده نیست؛ هیچ پیشنهادی ارسال نشد.",
                    debug=safe_debug,
                )
            if not await submitter.is_enabled():
                return _failure(
                    "دکمه ارسال سایت غیرفعال است؛ هیچ پیشنهادی ارسال نشد.",
                    debug=safe_debug,
                )

            live_action = str(await submitter.get_attribute(endpoint_attribute) or "").strip()
            live_endpoint = urljoin(current_url, live_action)
            if (
                not live_action
                or _origin(live_endpoint) != _origin(current_url)
                or urlsplit(live_endpoint).path.rstrip("/") != endpoint_parts.path.rstrip("/")
            ):
                return _failure(
                    "مسیر دکمه در مرورگر با مسیر تأییدشده صفحه یکی نیست؛ هیچ درخواستی نفرستادیم.",
                    debug=safe_debug,
                )

            initial_status_locator = page.locator("#bid_status_custom").first
            initial_bid_status = (
                (await initial_status_locator.inner_text()).strip()
                if await initial_status_locator.count()
                else ""
            )
            expected_path = endpoint_parts.path.rstrip("/")
            try:
                # Listen first, then perform one real click. Never replay the
                # endpoint directly if the site does not issue the request.
                async with page.expect_response(
                    lambda response: (
                        response.request.method.upper() == "POST"
                        and urlsplit(response.url).path.rstrip("/") == expected_path
                    ),
                    timeout=18_000,
                ) as response_info:
                    # From this point a timeout may be ambiguous: even if the
                    # click call raises, avoid claiming that no submit happened.
                    action_clicked = True
                    await submitter.click()
                response = await response_info.value
            except PlaywrightTimeoutError:
                safe_debug["request_sent"] = "unknown"
                return _failure(
                    "پس از کلیک، پاسخ مسیر ثبت پیشنهاد دریافت نشد؛ ممکن است سایت درخواست را گرفته باشد. "
                    "برای جلوگیری از پیشنهاد تکراری، وضعیت پروژه را دستی بررسی کن و فعلاً دوباره تلاش نکن.",
                    pause_source=True,
                    debug=safe_debug,
                )

            safe_debug["request_sent"] = True
            response_text = await response.text()
            ok, message = html_parser.detect_success(response.status, response_text, bid_cfg)
            if not ok and response.status < 400:
                # The captured ParsCoders page uses jQuery's .done() callback to
                # show a success modal and mutate #bid_status_custom only after
                # the server response. Wait for that UI evidence instead of
                # treating an arbitrary HTTP 200 as a submitted proposal.
                try:
                    await page.wait_for_function(
                        "(initial) => { const text = (document.querySelector('#bid_status_custom')?.textContent || '').trim(); "
                        "return ['پیشنهاد ثبت شد', 'پیشنهاد ویرایش شد'].includes(text) && text !== initial; }",
                        arg=initial_bid_status,
                        timeout=3_000,
                    )
                    status_text = (await page.locator("#bid_status_custom").first.inner_text()).strip()
                    ok = True
                    message = f"ثبت پیشنهاد با تغییر وضعیت در رابط سایت تأیید شد: {status_text}"
                except PlaywrightTimeoutError:
                    pass

            if not ok:
                pause = html_parser.requires_javascript(response_text)
                log.warning(
                    "[BID][BROWSER-SUBMIT] source=%s id=%s status=%s endpoint_path=%s "
                    "outcome=not_confirmed clicked=%s response_chars=%s",
                    source_name,
                    project_id,
                    response.status,
                    expected_path,
                    action_clicked,
                    len(response_text or ""),
                )
                return SubmissionResult(
                    status=SubmissionStatus.FAILED,
                    message=(
                        f"مرورگر دکمه ارسال را کلیک کرد و پاسخ HTTP {response.status} گرفت، "
                        f"اما سایت ثبت پیشنهاد را صریحاً تأیید نکرد: {message} "
                        "وضعیت پروژه را در سایت بررسی کن."
                    ),
                    http_status=response.status,
                    javascript_required=pause,
                    payload_debug=safe_debug,
                )

            log.info(
                "[BID][BROWSER-SUBMIT] source=%s id=%s status=%s endpoint_path=%s outcome=confirmed_success",
                source_name,
                project_id,
                response.status,
                expected_path,
            )
            return SubmissionResult(
                status=SubmissionStatus.SUBMITTED,
                message=message,
                http_status=response.status,
                payload_debug=safe_debug,
            )
    except PlaywrightTimeoutError:
        safe_debug["request_sent"] = "unknown" if action_clicked else False
        return _failure(
            "مرورگر برای بازکردن فرم یا تکمیل گردش کار زمان کافی نداشت؛ "
            "هیچ تلاش خودکار دیگری انجام نمی‌شود تا وضعیت روشن شود.",
            pause_source=action_clicked,
            debug=safe_debug,
        )
    except Exception as exc:
        safe_debug["request_sent"] = "unknown" if action_clicked else False
        log.warning(
            "[BID][BROWSER-SUBMIT] source=%s id=%s outcome=browser_error error_type=%s clicked=%s",
            source_name,
            project_id,
            type(exc).__name__,
            action_clicked,
        )
        return _failure(
            "اجرای گردش مرورگری فرم موفق نشد. "
            + (
                "کلیک ارسال انجام شده و نتیجه قطعی نیست؛ وضعیت پروژه را دستی بررسی کن."
                if action_clicked
                else "هیچ کلیکی برای ثبت پیشنهاد انجام نشد."
            ),
            pause_source=action_clicked,
            debug=safe_debug,
        )
