"""Submit JavaScript-managed marketplace forms through their real browser UI.

This module deliberately clicks the site's own controls; it does not reproduce
or guess private AJAX payloads, forge JavaScript state, or bypass challenges.
Playwright is an optional extra so non-browser sources do not need Chromium.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
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


_EDITOR_SELECTOR = ", ".join(
    [
        "div.ql-editor",
        "div.ck-editor__editable",
        "div.cke_editable",
        "div.trumbowyg-editor",
        "div.note-editable",
        "div.trix-content",
        "[contenteditable='true']",
        "[role='textbox']",
    ]
)
_NUMERIC_FIELDS = {"amount", "days", "_validDays"}
_STEP_TIMEOUT_MS = 3_000
_browser_lock: asyncio.Lock | None = None


def _digits(value: str) -> str:
    """Compare numbers regardless of thousand separators / Persian digits added by input masks."""
    table = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    return re.sub(r"\D", "", str(value).translate(table))


def _same_value(logical_name: str, actual: str, expected: str) -> bool:
    if logical_name in _NUMERIC_FIELDS:
        return _digits(actual) == _digits(expected)
    return " ".join(str(actual).split()) == " ".join(str(expected).split())


_SET_VALUE_JS = (
    "(el, val) => {"
    "  const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;"
    "  const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;"
    "  setter.call(el, val);"
    "  el.dispatchEvent(new Event('input', {bubbles: true}));"
    "  el.dispatchEvent(new Event('change', {bubbles: true}));"
    "  el.dispatchEvent(new Event('blur', {bubbles: true}));"
    "}"
)


async def _human_fill_input(page: Any, control: Any, logical_name: str, value: str) -> str:
    """Fill an <input>/<textarea> like a person: focus it, clear it, type, leave it.

    Each strategy has a short timeout so one stubborn control can never burn the
    whole 12-second page budget. Returns the strategy that worked and always
    verifies the value the page actually holds (input masks are tolerated).
    """
    attempts: list[str] = []

    async def current() -> str:
        return str(await control.evaluate("(el) => el.value || ''"))

    try:
        await control.scroll_into_view_if_needed(timeout=_STEP_TIMEOUT_MS)
        await control.click(timeout=_STEP_TIMEOUT_MS)
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Delete")
        await page.keyboard.type(value, delay=20)
        await page.keyboard.press("Tab")  # blur: sites validate/format on change
        if _same_value(logical_name, await current(), value):
            return "typed"
        attempts.append("typed:value_mismatch")
    except Exception as exc:  # noqa: BLE001 - every strategy may fail independently
        attempts.append(f"typed:{type(exc).__name__}")

    try:
        await control.fill(value, timeout=_STEP_TIMEOUT_MS, force=True)
        await control.dispatch_event("change")
        if _same_value(logical_name, await current(), value):
            return "fill"
        attempts.append("fill:value_mismatch")
    except Exception as exc:  # noqa: BLE001
        attempts.append(f"fill:{type(exc).__name__}")

    await control.evaluate(_SET_VALUE_JS, value)
    if _same_value(logical_name, await current(), value):
        return "js"
    raise ValueError(
        f"field value not applied: {logical_name} (tried {', '.join(attempts)}; js:value_mismatch)"
    )


_EDITOR_API_JS = """
(el, payload) => {
  const fire = (node, name) => node.dispatchEvent(new Event(name, {bubbles: true}));
  try {
    if (window.tinymce) {
      let ed = (el.id && tinymce.get && tinymce.get(el.id)) || null;
      if (!ed && tinymce.editors) {
        for (let i = 0; i < tinymce.editors.length; i++) {
          const cand = tinymce.editors[i];
          if (cand && (cand.targetElm === el || (cand.getElement && cand.getElement() === el))) { ed = cand; break; }
        }
      }
      if (ed) {
        ed.focus();
        ed.setContent(payload.html);
        ed.save();
        fire(el, 'input'); fire(el, 'change');
        return {kind: 'tinymce', text: ed.getContent({format: 'text'}) || ''};
      }
    }
  } catch (e) {}
  try {
    if (window.CKEDITOR && CKEDITOR.instances) {
      const ins = CKEDITOR.instances[el.name] || CKEDITOR.instances[el.id];
      if (ins) {
        ins.setData(payload.html);
        ins.updateElement();
        fire(el, 'change');
        return {kind: 'ckeditor4', text: String(ins.getData() || '').replace(/<[^>]+>/g, ' ')};
      }
    }
  } catch (e) {}
  return null;
}
"""

_IFRAME_EDITOR_SELECTORS = (
    "iframe.tox-edit-area__iframe",
    "iframe[id$='_ifr']",
    "iframe.cke_wysiwyg_frame",
    "iframe.note-editor",
)


def _norm_text(value: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", str(value or "")).split())


def _contains_probe(haystack: str, value: str) -> bool:
    probe = _norm_text(value)[:25]
    return bool(probe) and probe in _norm_text(haystack)


async def _type_lines(page: Any, value: str) -> None:
    lines = value.replace("\r\n", "\n").split("\n")
    for index, line in enumerate(lines):
        if index:
            await page.keyboard.press("Enter")
        if line:
            await page.keyboard.insert_text(line)


async def _human_fill_message(page: Any, form: Any, control: Any, value: str) -> str:
    """Write the proposal into the site's rich editor and keep the hidden <textarea> in sync.

    Order: the editor's own public API (TinyMCE/CKEditor — what the site's code
    reads on submit), then real typing inside an editor iframe, then a visible
    contenteditable, then the plain textarea. Whatever path wins, the textarea
    that is actually submitted is verified to hold the text.
    """
    import html as html_lib

    html_value = "".join(
        f"<p>{html_lib.escape(line)}</p>" if line.strip() else "<p><br></p>"
        for line in value.replace("\r\n", "\n").split("\n")
    )

    async def textarea_value() -> str:
        return str(await control.evaluate("(el) => el.value || ''"))

    async def ensure_textarea(method: str) -> str:
        if not _contains_probe(await textarea_value(), value):
            await control.evaluate(_SET_VALUE_JS, value)
            return method + "+textarea_js"
        return method

    try:
        outcome = await control.evaluate(_EDITOR_API_JS, {"html": html_value, "text": value})
        if outcome and _contains_probe(outcome.get("text", ""), value):
            return await ensure_textarea(str(outcome.get("kind") or "editor_api"))
    except Exception as exc:  # noqa: BLE001
        log.info("[BID][BROWSER-EDITOR] api fill failed error_type=%s", type(exc).__name__)

    for scope in (form, page):
        for selector in _IFRAME_EDITOR_SELECTORS:
            try:
                body = scope.frame_locator(selector).first.locator("body")
                if (
                    not await scope.locator(selector).count()
                    or not await scope.locator(selector).first.is_visible()
                ):
                    continue
                await body.click(timeout=_STEP_TIMEOUT_MS)
                await page.keyboard.press("Control+A")
                await page.keyboard.press("Delete")
                await _type_lines(page, value)
                if _contains_probe(await body.inner_text(timeout=_STEP_TIMEOUT_MS), value):
                    return await ensure_textarea("iframe_editor")
            except Exception as exc:  # noqa: BLE001
                log.info("[BID][BROWSER-EDITOR] iframe typing failed error_type=%s", type(exc).__name__)

    for scope in (form, page):
        candidate = scope.locator(_EDITOR_SELECTOR).first
        try:
            if not (await candidate.count() and await candidate.is_visible()):
                continue
            await candidate.scroll_into_view_if_needed(timeout=_STEP_TIMEOUT_MS)
            await candidate.click(timeout=_STEP_TIMEOUT_MS)
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Delete")
            await _type_lines(page, value)
            return await ensure_textarea("editor")
        except Exception as exc:  # noqa: BLE001
            log.info("[BID][BROWSER-EDITOR] editor typing failed error_type=%s", type(exc).__name__)

    try:
        if await control.is_visible():
            return await _human_fill_input(page, control, "description", value)
    except Exception:  # noqa: BLE001
        pass
    await control.evaluate(_SET_VALUE_JS, value)
    return "textarea_js"


async def control_message_state(form: Any, field_name: str) -> str:
    """One-line summary of what the message textarea holds right before the click (for diagnosis)."""
    if not field_name:
        return "no_field"
    control = form.locator(_attr_selector("name", field_name)).first
    length = int(await control.evaluate("(el) => (el.value || '').length"))
    return f"textarea_chars={length}"


_NOTICE_JS = """
() => {
  const selectors = ['.swal2-popup', '.swal-modal', '.sweet-alert', '.toast', '.toast-message', '.iziToast',
    '.noty_body', '.toastify', '.alert', '[role=alert]', '.modal.show .modal-body', '.modal.in .modal-body',
    '.invalid-feedback', '.text-danger', '.help-block', '.parsley-errors-list', 'label.error', '.error-message'];
  const out = [], seen = new Set();
  for (const sel of selectors) {
    for (const el of document.querySelectorAll(sel)) {
      const rect = el.getBoundingClientRect(), style = getComputedStyle(el);
      if (!rect.width || !rect.height || style.visibility === 'hidden' || style.display === 'none') continue;
      const text = (el.innerText || '').replace(/\\s+/g, ' ').trim();
      if (text && text.length < 400 && !seen.has(text)) { seen.add(text); out.push(text); }
    }
  }
  return out.slice(0, 8);
}
"""


async def _visible_notices(page: Any) -> list[str]:
    try:
        return [str(item) for item in await page.evaluate(_NOTICE_JS)]
    except Exception:  # noqa: BLE001 - page may be navigating
        return []


async def _dump_debug(
    page: Any, form: Any, bid_cfg: dict[str, Any], source: str, project_id: str, reason: str
) -> None:
    """Best-effort screenshot + sanitized form HTML so a failure can be diagnosed from real data."""
    if bid_cfg.get("browser_debug", True) is False:
        return
    try:
        target = Path(str(bid_cfg.get("browser_debug_dir") or "data/browser_debug"))
        target.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = target / f"{stamp}-{re.sub(r'[^A-Za-z0-9_.-]', '_', source)}-{project_id}-{reason}"
        await page.screenshot(path=str(base.with_suffix(".png")), full_page=False, timeout=5_000)
        html = ""
        try:
            html = await form.evaluate(
                "(el) => { const c = el.cloneNode(true);"
                " c.querySelectorAll('input[type=hidden]').forEach(i => i.setAttribute('value', '<redacted>'));"
                " return c.outerHTML; }"
            )
        except Exception:  # noqa: BLE001
            html = await page.content()
        base.with_suffix(".html").write_text(str(html)[:200_000], encoding="utf-8")
        log.warning(
            "[BID][BROWSER-DEBUG] source=%s id=%s reason=%s saved=%s", source, project_id, reason, base
        )
    except Exception as exc:  # noqa: BLE001 - diagnostics must never break the run
        log.info("[BID][BROWSER-DEBUG] capture failed error_type=%s", type(exc).__name__)


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


async def _submit_once(
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
                guest_markers = ("ورود با گوگل", "ثبت‌نام رایگان", "ثبت نام رایگان")
                if any(marker in body_text for marker in guest_markers):
                    return _failure(
                        "مرورگر صفحه را به‌صورت مهمان (وارد‌نشده) دید و دکمه «ارسال پیشنهاد» وجود نداشت؛ "
                        "نشست منقضی شده است. کوکی را در /market تازه کن و /test را اجرا کن.",
                        debug=safe_debug,
                    )
                await _dump_debug(page, form, bid_cfg, source_name, project_id, "form-missing")
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
                    "button.bid-send--button"
                    + _attr_selector("data-project-id", str(project_id))
                    + ":visible",
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
                            log.debug(
                                "[BID][BROWSER-CTA] source=%s id=%s cta_selector=%r",
                                source_name,
                                project_id,
                                sel,
                            )
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
                    "disabled: Boolean(el.disabled), readOnly: Boolean(el.readOnly)})"
                )
                if properties["disabled"] or properties["readOnly"] or properties["type"] == "hidden":
                    raise ValueError(f"mapped field is not editable: {logical_name}")
                if properties["tag"] == "select":
                    await control.select_option(value=value, timeout=_STEP_TIMEOUT_MS)
                    await control.dispatch_event("change")
                    method = "select"
                elif logical_name == "description":
                    method = await _human_fill_message(page, form, control, value)
                else:
                    method = await _human_fill_input(page, control, logical_name, value)
                log.info(
                    "[BID][BROWSER-FIELD] source=%s id=%s field=%s method=%s",
                    source_name,
                    project_id,
                    logical_name,
                    method,
                )

            # «مدت اعتبار پیشنهاد» (validDays) is empty by default and keeps the
            # site's submit button disabled until filled: use config, else 7 days.
            valid_days_value: str | None = None
            try:
                vd_field = form.locator(_attr_selector("name", "validDays"))
                if (
                    await vd_field.count() == 1
                    and not str(await vd_field.evaluate("el => el.value || ''")).strip()
                ):
                    cfg_valid = str(bid_cfg.get("valid_days") or bid_cfg.get("validDays") or "7").strip()
                    valid_days_value = cfg_valid if cfg_valid.isdigit() and 1 <= int(cfg_valid) <= 90 else "7"
            except Exception:  # noqa: BLE001
                valid_days_value = None

            current_field = "amount"
            try:
                await fill_field("amount", str(int(amount)))
                if delivery_days is not None:
                    current_field = "days"
                    await fill_field("days", str(int(delivery_days)))
                current_field = "description"
                await fill_field("description", proposal_text)
                if valid_days_value is not None:
                    current_field = "validDays"
                    field_mapping["_validDays"] = "validDays"
                    try:
                        await fill_field("_validDays", valid_days_value)
                    finally:
                        field_mapping.pop("_validDays", None)
                await page.wait_for_timeout(300)  # let the site's validator enable the button
            except Exception as exc:
                log.warning(
                    "[BID][BROWSER-FORM] source=%s id=%s outcome=field_fill_failed error_type=%s field=%s detail=%r",
                    source_name,
                    project_id,
                    type(exc).__name__,
                    current_field,
                    str(exc)[:240],
                )
                await _dump_debug(page, form, bid_cfg, source_name, project_id, f"fill-{current_field}")
                safe_debug["failed_field"] = current_field
                return _failure(
                    f"مرورگر نتوانست فیلد «{current_field}» را تکمیل کند؛ "
                    "هیچ کلیکی برای ثبت پیشنهاد انجام نشد. اسکرین‌شات در data/browser_debug ذخیره شد.",
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
                    log.warning(
                        "[BID][BROWSER-FORM-INVALID] source=%s id=%s invalid=%r",
                        source_name,
                        project_id,
                        invalid_detail,
                    )
                    safe_debug["invalid_field"] = invalid_detail[:300]
                except Exception:
                    pass
                await _dump_debug(page, form, bid_cfg, source_name, project_id, "form-invalid")
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
                    log.warning(
                        "[BID][BROWSER-SUBMIT-DISABLED] source=%s id=%s button=%r",
                        source_name,
                        project_id,
                        btn_outer,
                    )
                except Exception:
                    pass
                await _dump_debug(page, form, bid_cfg, source_name, project_id, "submit-disabled")
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

            def _is_bid_request(url: str, method: str) -> bool:
                return (
                    method.upper() == "POST"
                    and _origin(url) == _origin(base_url)
                    and urlsplit(url).path.rstrip("/") == expected_path
                )

            loop = asyncio.get_running_loop()
            response_future: asyncio.Future = loop.create_future()
            request_seen = {"value": False}

            def on_request(request: Any) -> None:
                if _is_bid_request(request.url, request.method):
                    request_seen["value"] = True

            def on_response(resp: Any) -> None:
                if not response_future.done() and _is_bid_request(resp.url, resp.request.method):
                    response_future.set_result(resp)

            try:
                editor_check = await control_message_state(form, field_mapping.get("description", ""))
            except Exception:  # noqa: BLE001
                editor_check = "unknown"
            log.info(
                "[BID][BROWSER-PRECLICK] source=%s id=%s message_state=%s",
                source_name,
                project_id,
                editor_check,
            )
            baseline_notices = set(await _visible_notices(page))
            response_timeout = float(bid_cfg.get("response_timeout_seconds") or 20)
            page.on("request", on_request)
            page.on("response", on_response)
            new_notices: list[str] = []
            try:
                # Exactly one real click. Never replay the endpoint directly if
                # the site does not issue the request.
                action_clicked = True
                await submitter.click()
                clicked_at = time.monotonic()
                while not response_future.done():
                    elapsed = time.monotonic() - clicked_at
                    if elapsed >= response_timeout:
                        break
                    if elapsed >= 2.5 and not request_seen["value"]:
                        # Nothing left the browser: the page itself is telling us why.
                        new_notices = [n for n in await _visible_notices(page) if n not in baseline_notices]
                        if new_notices:
                            break
                    await asyncio.sleep(0.35)
            finally:
                page.remove_listener("request", on_request)
                page.remove_listener("response", on_response)

            if not response_future.done():
                if not new_notices:
                    new_notices = [n for n in await _visible_notices(page) if n not in baseline_notices]
                # The UI may already show the success state even if we missed the XHR.
                try:
                    await page.wait_for_function(
                        "(initial) => { const el = document.querySelector('#bid_status_custom'); "
                        "const txt = (el?.textContent || '').trim(); "
                        "return ['پیشنهاد ثبت شد', 'پیشنهاد ویرایش شد'].includes(txt) && txt !== initial; }",
                        arg=initial_bid_status,
                        timeout=2_000,
                    )
                    status_text = (await page.locator("#bid_status_custom").first.inner_text()).strip()
                    log.info(
                        "[BID][BROWSER-SUBMIT] source=%s id=%s outcome=ui_confirmed_without_response status_text=%r",
                        source_name,
                        project_id,
                        status_text,
                    )
                    return SubmissionResult(
                        status=SubmissionStatus.SUBMITTED,
                        message=f"ثبت پیشنهاد با تغییر وضعیت در رابط سایت تأیید شد: {status_text}",
                        http_status=200,
                        payload_debug=safe_debug,
                    )
                except Exception:  # noqa: BLE001
                    pass

                joined = " | ".join(new_notices)[:400]
                if not request_seen["value"]:
                    # Definite: no request left the browser, so nothing was registered.
                    safe_debug["request_sent"] = False
                    try:
                        invalid = await form.evaluate(
                            "(el) => [...el.querySelectorAll(':invalid, .is-invalid')]"
                            ".map(e => e.name || e.id || e.className).slice(0, 6)"
                        )
                    except Exception:  # noqa: BLE001
                        invalid = []
                    await _dump_debug(page, form, bid_cfg, source_name, project_id, "click-no-request")
                    log.warning(
                        "[BID][BROWSER-SUBMIT] source=%s id=%s outcome=click_sent_no_request notices=%r invalid=%r",
                        source_name,
                        project_id,
                        new_notices,
                        invalid,
                    )
                    if new_notices and html_parser.requires_subscription(f"<div>{joined}</div>"):
                        daily = html_parser.is_daily_limit_error(f"<div>{joined}</div>")
                        return SubmissionResult(
                            status=SubmissionStatus.FAILED,
                            message=(
                                f"⛔ سقف ارسال روزانه پر شده است ({joined[:120]}). حساب رایگان روزی ۱ پیشنهاد اجازه می‌دهد. "
                                if daily
                                else f"⛔ اشتراک حساب به پایان رسیده یا محدود شده ({joined[:120]}). "
                            )
                            + "برای ادامه اشتراک تهیه کنید. جست‌وجو متوقف شد.",
                            subscription_required=True,
                            payload_debug=safe_debug,
                        )
                    reason = (
                        f"سایت پس از کلیک پیام داد: «{joined}»."
                        if joined
                        else "پس از کلیک هیچ درخواستی به سایت فرستاده نشد و پیامی هم نمایش داده نشد "
                        + (f"(فیلدهای نامعتبر: {', '.join(map(str, invalid))})" if invalid else "")
                        + "؛ اعتبارسنجی سمت مرورگر فرم را رد کرده است."
                    )
                    return SubmissionResult(
                        status=SubmissionStatus.FAILED,
                        message=(
                            f"پیشنهاد ثبت نشد. {reason} اسکرین‌شات و فرم در data/browser_debug ذخیره شد؛ "
                            "همین پروژه چند بار پشت‌سرهم تلاش نمی‌شود."
                        ),
                        payload_debug=safe_debug,
                    )

                # A request left the browser but no answer came back: genuinely ambiguous.
                safe_debug["request_sent"] = "unknown"
                log.warning(
                    "[BID][BROWSER-SUBMIT] source=%s id=%s outcome=request_sent_no_response waited=%.0fs",
                    source_name,
                    project_id,
                    response_timeout,
                )
                return SubmissionResult(
                    status=SubmissionStatus.FAILED,
                    message=(
                        "درخواست ثبت پیشنهاد فرستاده شد ولی سایت پاسخی نداد (تایم‌اوت شبکه). ممکن است پیشنهاد ثبت شده "
                        "باشد؛ دور بعد وضعیت از روی صفحهٔ پروژه سنجیده می‌شود و ارسال تکراری انجام نمی‌شود."
                    ),
                    payload_debug=safe_debug,
                )
            response = response_future.result()

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
                # Subscription / daily-limit outranks everything
                if html_parser.requires_subscription(response_text):
                    sub_snippet = html_parser.subscription_limit_snippet(response_text)
                    daily = html_parser.is_daily_limit_error(response_text)
                    if daily:
                        msg = (
                            f"⛔ سقف ارسال روزانه پر شده است ({sub_snippet[:120]}). "
                            "حساب رایگان پارس‌کدرز روزی ۱ پیشنهاد اجازه می‌دهد. "
                            "برای ادامه، اشتراک تهیه کنید. جست‌وجو متوقف شد."
                        )
                    else:
                        msg = (
                            f"⛔ اشتراک حساب پارس‌کدرز به پایان رسیده ({sub_snippet[:120]}). "
                            "برای ارسال پیشنهاد بیشتر باید اشتراک را تمدید کنید. جست‌وجو متوقف شد."
                        )
                    log.warning(
                        "[BID][BROWSER-SUBMIT] source=%s id=%s status=%s outcome=subscription_required daily=%s snippet=%r",
                        source_name,
                        project_id,
                        response.status,
                        daily,
                        sub_snippet,
                    )
                    return SubmissionResult(
                        status=SubmissionStatus.FAILED,
                        message=msg,
                        http_status=response.status,
                        subscription_required=True,
                        payload_debug=safe_debug,
                    )
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
        # A browser/page that died before any click is safe to retry once on a fresh browser.
        safe_debug["retryable"] = (not action_clicked) and type(exc).__name__ == "TargetClosedError"
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


async def submit_js_managed_form(**kwargs: Any) -> SubmissionResult:
    """Run the browser workflow serially; retry once only if Chromium died before any click."""
    global _browser_lock
    if _browser_lock is None:
        _browser_lock = asyncio.Lock()
    async with _browser_lock:
        result = await _submit_once(**kwargs)
        if (result.payload_debug or {}).get("retryable"):
            log.warning(
                "[BID][BROWSER-RETRY] source=%s id=%s reason=browser_closed_before_click",
                kwargs.get("source_name"),
                kwargs.get("project_id"),
            )
            await asyncio.sleep(1.0)
            result = await _submit_once(**kwargs)
        if result.payload_debug:
            result.payload_debug.pop("retryable", None)
        return result
