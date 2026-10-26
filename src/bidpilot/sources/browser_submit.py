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
    user_agent: str,
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
            try:
                browser = await playwright.chromium.launch(headless=True)
            except Exception as chromium_error:
                # The Playwright CDN may be unavailable in some regions. If the
                # managed Chromium build is missing, try a browser already installed
                # on Windows/macOS/Linux before asking the user to download anything.
                log.warning(
                    "[BID][BROWSER-LAUNCH] source=%s id=%s channel=chromium error_type=%s",
                    source_name,
                    project_id,
                    type(chromium_error).__name__,
                )
                browser = None
                for channel in ("msedge", "chrome"):
                    try:
                        browser = await playwright.chromium.launch(channel=channel, headless=True)
                        log.info(
                            "[BID][BROWSER-LAUNCH] source=%s id=%s channel=%s outcome=launched",
                            source_name,
                            project_id,
                            channel,
                        )
                        break
                    except Exception as channel_error:
                        log.info(
                            "[BID][BROWSER-LAUNCH] source=%s id=%s channel=%s error_type=%s",
                            source_name,
                            project_id,
                            channel,
                            type(channel_error).__name__,
                        )
                if browser is None:
                    return _failure(
                        "مرورگر داخلی Playwright در دسترس نیست و اجرای Microsoft Edge یا Google Chrome "
                        "نصب‌شده هم موفق نشد. یکی از این مرورگرها را نصب کن یا از شبکه‌ای استفاده کن "
                        "که دانلود مرورگر Playwright در آن مجاز است. هیچ پیشنهادی ارسال نشده است.",
                        pause_source=True,
                        debug=safe_debug,
                    )
            context_options: dict[str, Any] = {"locale": "fa-IR"}
            if user_agent.strip():
                context_options["user_agent"] = user_agent.strip()
            context = await browser.new_context(**context_options)
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
            if _origin(current_url) != _origin(base_url):
                return _failure(
                    "صفحه پروژه پس از بازشدن به دامنه دیگری هدایت شد؛ برای محافظت از کوکی و پیشنهاد متوقف شد.",
                    debug=safe_debug,
                )
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
            # The exact trigger markup has changed a few times (button vs a,
            # class bid-send--button vs generic), so try several selectors.
            if not await form.is_visible():
                cta_candidates = [
                    "button.bid-send--button" + _attr_selector("data-project-id", str(project_id)) + ":visible",
                    "button.bid-send--button:visible",
                    "a.bid-send--button:visible",
                    "[data-project-id=" + json.dumps(str(project_id)) + "]:visible",
                    "button:has-text('ارسال پیشنهاد'):visible",
                    "a:has-text('ارسال پیشنهاد'):visible",
                    "[data-href*='/conversation/ajax/bid-send/']:visible",
                    "button[data-href]:visible",
                ]
                cta = None
                for sel in cta_candidates:
                    try:
                        cand = page.locator(sel).first
                        if await cand.count() and await cand.is_visible():
                            cta = cand
                            log.info("[BID][BROWSER-CTA] source=%s id=%s cta_selector=%r", source_name, project_id, sel)
                            break
                    except Exception:
                        continue
                if cta is None:
                    return _failure(
                        "فرم پنهان است اما دکمه اصلی «ارسال پیشنهاد» برای بازکردنش پیدا نشد؛ "
                        "هیچ پیشنهادی ارسال نشد.",
                        debug=safe_debug,
                    )
                await cta.click()
                # Off-canvas animation takes ~400ms; wait for form to be visible and stable
                try:
                    await form.wait_for(state="visible", timeout=12_000)
                except Exception:
                    # Fallback: wait a bit and re-check
                    try:
                        await page.wait_for_timeout(800)
                    except Exception:
                        pass
                try:
                    await page.wait_for_timeout(600)
                except Exception:
                    pass
            else:
                try:
                    await page.wait_for_timeout(400)
                except Exception:
                    pass

            async def fill_field(logical_name: str, value: str) -> None:
                field_name = field_mapping.get(logical_name)
                if not field_name:
                    return
                control = form.locator(_attr_selector("name", field_name))
                count = await control.count()
                if count != 1:
                    raise ValueError(f"mapped field missing or ambiguous: {logical_name} (count={count})")
                properties = await control.evaluate(
                    "(el) => ({tag: el.tagName.toLowerCase(), type: (el.type || '').toLowerCase(), "
                    "disabled: Boolean(el.disabled), readOnly: Boolean(el.readOnly), "
                    "visible: Boolean(el.offsetParent !== null && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none')})"
                )
                if properties["disabled"] or properties["readOnly"] or properties["type"] == "hidden":
                    raise ValueError(f"mapped field is not editable: {logical_name}")
                if properties["tag"] == "select":
                    await control.select_option(value=value)
                    try:
                        await control.dispatch_event("change")
                    except Exception:
                        pass
                    return
                # For the rich-text proposal field, the underlying <textarea> is
                # often hidden (display:none) and replaced by a contenteditable
                # editor (Quill/CKEditor/Trumbowyg/TinyMCE). `fill()` requires a
                # visible element and would TimeoutError on such hidden fields.
                # Set the value via JS and, if a visible editor exists, mirror it there.
                if logical_name == "description":
                    # Always set the hidden textarea value directly
                    try:
                        await control.evaluate(
                            "(el, val) => {"
                            "  el.value = val;"
                            "  el.dispatchEvent(new Event('input', {bubbles:true}));"
                            "  el.dispatchEvent(new Event('change', {bubbles:true}));"
                            "  if (el._valueTracker) el._valueTracker.setValue('');"
                            "}",
                            value,
                        )
                    except Exception:
                        pass
                    # Try to find and fill the visible rich editor as well
                    editor_selectors = [
                        "div[contenteditable='true']",
                        "div.ql-editor",
                        "div.ck-editor__editable",
                        "div.ck-content",
                        "div.cke_editable",
                        "div.trumbowyg-editor",
                        "div.note-editable",
                        "div.trix-content",
                        "div[role='textbox']",
                    ]
                    filled_editor = False
                    for sel in editor_selectors:
                        try:
                            editor = form.locator(sel).first
                            if await editor.count() == 0:
                                # Some editors are outside the <form> but inside the offcanvas
                                editor = page.locator(sel).first
                                if await editor.count() == 0:
                                    continue
                            if not await editor.is_visible():
                                continue
                            # Clear and insert via execCommand for editors that expect it
                            try:
                                await editor.click()
                            except Exception:
                                pass
                            try:
                                await editor.evaluate(
                                    "(el, val) => {"
                                    "  el.focus();"
                                    "  el.innerHTML = '';"
                                    "  document.execCommand('selectAll', false, null);"
                                    "  document.execCommand('insertText', false, val);"
                                    "  el.dispatchEvent(new Event('input', {bubbles:true}));"
                                    "  el.dispatchEvent(new Event('change', {bubbles:true}));"
                                    "}",
                                    value,
                                )
                                filled_editor = True
                                break
                            except Exception:
                                try:
                                    await editor.evaluate(
                                        "(el, val) => { el.textContent = val; el.innerText = val; el.innerHTML = val.replace(/\\n/g, '<br>'); el.dispatchEvent(new Event('input', {bubbles:true})); }",
                                        value,
                                    )
                                    filled_editor = True
                                    break
                                except Exception:
                                    continue
                        except Exception:
                            continue
                    # Handle TinyMCE iframe case
                    if not filled_editor:
                        try:
                            iframe_loc = form.locator("iframe.tox-edit-area__iframe, iframe.cke_wysiwyg_frame").first
                            if await iframe_loc.count():
                                frame = iframe_loc.content_frame
                                if frame:
                                    body = frame.locator("body").first
                                    if await body.count():
                                        await body.evaluate(
                                            "(el, val) => { el.focus(); document.execCommand('selectAll', false, null); document.execCommand('insertText', false, val); el.dispatchEvent(new Event('input', {bubbles:true})); }",
                                            value,
                                        )
                                        filled_editor = True
                        except Exception:
                            pass
                    # Verify textarea now holds the value; if not, try visible fill as last resort
                    try:
                        current = await control.evaluate("el => el.value")
                        if current != value and await control.is_visible():
                            await control.fill(value)
                            await control.dispatch_event("change")
                    except Exception:
                        if await control.is_visible():
                            await control.fill(value)
                            await control.dispatch_event("change")
                    return
                # For numeric fields (amount/days/validDays), prefer visible fill but fall back to JS
                try:
                    if await control.is_visible():
                        await control.click()
                        await control.fill(value)
                        await control.dispatch_event("change")
                        await control.evaluate(
                            "(el, val) => { if (el.value !== val) { el.value = val; el.dispatchEvent(new Event('input', {bubbles:true})); el.dispatchEvent(new Event('change', {bubbles:true})); } }",
                            value,
                        )
                    else:
                        await control.evaluate(
                            "(el, val) => { el.value = val; el.dispatchEvent(new Event('input', {bubbles:true})); el.dispatchEvent(new Event('change', {bubbles:true})); el.dispatchEvent(new Event('blur', {bubbles:true})); }",
                            value,
                        )
                    # Give JS validators a moment
                    try:
                        await page.wait_for_timeout(200)
                    except Exception:
                        pass
                except Exception as fill_exc:
                    # Last resort: JS set even if visible fill failed
                    await control.evaluate(
                        "(el, val) => { el.value = val; el.dispatchEvent(new Event('input', {bubbles:true})); el.dispatchEvent(new Event('change', {bubbles:true})); }",
                        value,
                    )
                    raise fill_exc if await control.evaluate("el => el.value") != value else None

            # Determine a sensible validDays value (مدت اعتبار پیشنهاد).
            # The screenshot shows this field empty by default; the site's
            # client-side validation will keep the submit button disabled
            # until it is filled. Use config if provided, else 7 days.
            valid_days_value: str | None = None
            try:
                vd_field = form.locator(_attr_selector("name", "validDays"))
                if await vd_field.count() == 1:
                    # Check if already has a value; if empty, we need to fill it.
                    vd_current = await vd_field.evaluate("el => el.value || ''")
                    if not str(vd_current).strip():
                        # Prefer explicit config, else default 7
                        cfg_valid = str(bid_cfg.get("valid_days") or bid_cfg.get("validDays") or "7").strip()
                        # Ensure it's a plausible integer 1-90
                        try:
                            vd_int = int(cfg_valid)
                            if 1 <= vd_int <= 90:
                                valid_days_value = str(vd_int)
                            else:
                                valid_days_value = "7"
                        except Exception:
                            valid_days_value = "7"
                    else:
                        # Already has a value (e.g. 30 in some fixtures), leave it
                        valid_days_value = None
            except Exception:
                valid_days_value = None

            try:
                await fill_field("amount", str(int(amount)))
                if delivery_days is not None:
                    await fill_field("days", str(int(delivery_days)))
                await fill_field("description", proposal_text)
                if valid_days_value is not None:
                    # Temporarily extend mapping for this field
                    field_mapping["_validDays"] = "validDays"
                    await fill_field("_validDays", valid_days_value)
                    field_mapping.pop("_validDays", None)
                    # Wait a moment for the submit button to become enabled
                    try:
                        await page.wait_for_timeout(400)
                    except Exception:
                        pass
            except Exception as exc:
                field_name = str(exc).split(":")[0] if ":" in str(exc) else str(exc)[:80]
                log.warning(
                    "[BID][BROWSER-FORM] source=%s id=%s outcome=field_fill_failed error_type=%s field=%r detail=%r",
                    source_name,
                    project_id,
                    type(exc).__name__,
                    field_name,
                    str(exc)[:180],
                )
                return _failure(
                    "مرورگر نتوانست یکی از فیلدهای پیشنهاد را با اطمینان تکمیل کند؛ "
                    "هیچ کلیکی برای ثبت پیشنهاد انجام نشد.",
                    debug=safe_debug,
                )

            form_valid = await form.evaluate("(el) => el.checkValidity()")
            if not form_valid:
                try:
                    invalid_detail = await form.evaluate(
                        """(el) => {
                            const inv = el.querySelector(':invalid');
                            if (!inv) return '';
                            return (inv.outerHTML || '').slice(0,300) + ' name=' + (inv.name||'') + ' validity=' + JSON.stringify(inv.validity);
                        }"""
                    )
                    log.warning("[BID][BROWSER-FORM-INVALID] source=%s id=%s invalid=%r", source_name, project_id, invalid_detail)
                    safe_debug["invalid_field"] = invalid_detail[:300]
                except Exception:
                    pass
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
            # In some Parscoders offcanvas layouts the submit button is below the fold;
            # scroll it into view and give the JS validator a moment to enable it.
            try:
                if await submitter.count():
                    await submitter.scroll_into_view_if_needed()
                    await page.wait_for_timeout(400)
            except Exception:
                pass
            if await submitter.count() == 0 or not await submitter.is_visible():
                return _failure(
                    "دکمه اصلی ارسال در فرم مرورگر پیدا یا قابل‌مشاهده نیست؛ هیچ پیشنهادی ارسال نشد.",
                    debug=safe_debug,
                )
            # Wait briefly for the button to become enabled after the last field change
            for _ in range(6):
                try:
                    if await submitter.is_enabled():
                        break
                    await page.wait_for_timeout(500)
                except Exception:
                    break
            if not await submitter.is_enabled():
                # Log why it is still disabled
                try:
                    btn_outer = await submitter.evaluate("el => el.outerHTML.slice(0,400)")
                    log.warning("[BID][BROWSER-SUBMIT-DISABLED] source=%s id=%s button=%r", source_name, project_id, btn_outer)
                except Exception:
                    pass
                return _failure(
                    "دکمه ارسال سایت غیرفعال است؛ هیچ پیشنهادی ارسال نشد.",
                    debug=safe_debug,
                )

            live_action = str(await submitter.get_attribute(endpoint_attribute) or "").strip()
            live_endpoint = urljoin(current_url, live_action)
            if (
                not live_action
                or _origin(live_endpoint) != _origin(base_url)
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
                        and _origin(response.url) == _origin(base_url)
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
