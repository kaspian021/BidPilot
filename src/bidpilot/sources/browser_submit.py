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
from ..priority_lock import PriorityLock, submit_priority
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
_browser_lock: PriorityLock | None = None


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


def _debug_capture_enabled(bid_cfg: dict[str, Any]) -> bool:
    """Admin can override browser diagnostics at runtime from Telegram."""
    try:
        from ..settings_store import get_store

        override = get_store().get("admin.browser_debug_enabled")
        if override is not None:
            if isinstance(override, bool):
                return override
            return str(override).strip().lower() in {"1", "true", "yes", "on", "بله"}
    except Exception:  # noqa: BLE001 - diagnostics must not depend on the settings store
        pass
    return bid_cfg.get("browser_debug", True) is not False


_DEBUG_SNAPSHOT_JS = """
() => {
  const pathOnly = (raw) => {
    if (!raw) return "";
    try { return new URL(raw, location.href).pathname; } catch (_) { return String(raw).split(/[?#]/)[0]; }
  };
  const visible = (el) => {
    const r = el.getBoundingClientRect(), st = getComputedStyle(el);
    return st.display !== "none" && st.visibility !== "hidden" && Number(st.opacity || 1) > 0 &&
      r.width > 0 && r.height > 0 && r.right > 0 && r.bottom > 0 &&
      r.left < innerWidth && r.top < innerHeight;
  };
  const describe = (el) => ({
    tag: el.tagName.toLowerCase(),
    id: el.id || "",
    class: typeof el.className === "string" ? el.className.slice(0, 180) : "",
    name: el.getAttribute("name") || "",
    type: el.getAttribute("type") || "",
    text: (el.innerText || el.getAttribute("aria-label") || "").replace(/\\s+/g, " ").trim().slice(0, 180),
    role: el.getAttribute("role") || "",
    aria_controls: el.getAttribute("aria-controls") || "",
    aria_expanded: el.getAttribute("aria-expanded") || "",
    form: (el.form && (el.form.id || el.form.getAttribute("name"))) || (el.closest("form")?.id || ""),
    ancestor_project_id: el.getAttribute("data-project-id") || el.closest("[data-project-id]")?.getAttribute("data-project-id") || "",
    href_path: pathOnly(el.getAttribute("href")),
    action_path: pathOnly(el.getAttribute("action")),
    data_href_path: pathOnly(el.getAttribute("data-href")),
    data_url_path: pathOnly(el.getAttribute("data-url")),
    data_action_path: pathOnly(el.getAttribute("data-action")),
    data_target: el.getAttribute("data-target") || "",
    data_bs_target: el.getAttribute("data-bs-target") || "",
    onclick_present: el.hasAttribute("onclick"),
    disabled: Boolean(el.disabled),
    required: Boolean(el.required),
    visible: visible(el),
    rect: (() => { const r=el.getBoundingClientRect(); return {x:Math.round(r.x),y:Math.round(r.y),w:Math.round(r.width),h:Math.round(r.height)}; })()
  });
  const forms = [...document.querySelectorAll("form")].slice(0, 30).map(form => ({
    ...describe(form),
    method: (form.getAttribute("method") || "get").toLowerCase(),
    fields: [...form.querySelectorAll("input,textarea,select,button")].slice(0, 100).map(describe)
  }));
  const controls = [...document.querySelectorAll("button,input[type=submit],input[type=button],[role=button],a[href],[onclick],[data-bs-toggle],[data-toggle],[class*='bid-send'],[data-project-id]")]
    .slice(0, 400).map(describe);
  return {
    title: document.title || "",
    path: location.pathname,
    readyState: document.readyState,
    viewport: {width: innerWidth, height: innerHeight},
    counts: {forms: document.forms.length, buttons: document.querySelectorAll("button").length,
      inputs: document.querySelectorAll("input").length, textareas: document.querySelectorAll("textarea").length},
    forms, controls
  };
}
"""


async def _dump_debug(
    page: Any, form: Any, bid_cfg: dict[str, Any], source: str, project_id: str, reason: str
) -> None:
    """Save full-page, value-redacted HTML + DOM inventory + screenshots."""
    if not _debug_capture_enabled(bid_cfg):
        return
    try:
        target = Path(str(bid_cfg.get("browser_debug_dir") or "data/browser_debug"))
        target.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = target / f"{stamp}-{re.sub(r'[^A-Za-z0-9_.-]', '_', source)}-{project_id}-{reason}"

        # The full page image shows the CTA and panel context. A viewport image
        # is also saved because it is the most useful view for off-canvas bugs.
        try:
            await page.screenshot(path=str(base.with_suffix(".png")), full_page=True, timeout=8_000)
        except Exception:
            await page.screenshot(path=str(base.with_suffix(".png")), full_page=False, timeout=5_000)
        try:
            await page.screenshot(
                path=str(base.with_name(base.name + "-viewport.png")), full_page=False, timeout=5_000
            )
        except Exception:
            pass

        # Keep the whole DOM (including CTA siblings), but strip every field
        # value, textarea/editor contents, inline script and CSRF-like attribute.
        safe_html = await page.evaluate(
            """() => {
              const root = document.documentElement.cloneNode(true);
              root.querySelectorAll("script,noscript").forEach(el => {
                if (el.tagName === "SCRIPT") el.textContent = "/* script content omitted from diagnostics */";
                else el.remove();
              });
              root.querySelectorAll("input").forEach(el => {
                el.setAttribute("value", "[REDACTED]");
                if (el.hasAttribute("value")) el.value = "[REDACTED]";
              });
              root.querySelectorAll("textarea").forEach(el => { el.textContent = "[REDACTED]"; el.value = "[REDACTED]"; });
              root.querySelectorAll("select option").forEach(el => {
                if (el.hasAttribute("value")) el.setAttribute("value", "[REDACTED]");
              });
              root.querySelectorAll("[contenteditable=true],[contenteditable='']").forEach(el => {
                el.textContent = "[EDITOR CONTENT REDACTED]";
              });
              root.querySelectorAll("*").forEach(el => {
                for (const attr of [...el.attributes]) {
                  if (/(token|csrf|password|secret|cookie|api.?key)/i.test(attr.name)) {
                    el.setAttribute(attr.name, "[REDACTED]");
                  } else if (["href","action","src","data-href","data-url","data-action"].includes(attr.name.toLowerCase())) {
                    try {
                      const u = new URL(attr.value, location.href);
                      el.setAttribute(attr.name, u.pathname);
                    } catch (_) { el.setAttribute(attr.name, String(attr.value).split(/[?#]/)[0]); }
                  }
                }
              });
              return "<!doctype html>\\n" + root.outerHTML;
            }"""
        )
        base.with_suffix(".html").write_text(str(safe_html)[:500_000], encoding="utf-8")
        try:
            snapshot = await page.evaluate(_DEBUG_SNAPSHOT_JS)
            snapshot.update({
                "source": source,
                "project_id": str(project_id),
                "reason": reason,
                "page_url_path": urlsplit(str(page.url)).path,
                "form_selector_id": str(bid_cfg.get("_debug_form_id") or ""),
            })
            base.with_suffix(".json").write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            log.info("[BID][BROWSER-DEBUG] DOM inventory failed error_type=%s", type(exc).__name__)
        log.warning(
            "[BID][BROWSER-DEBUG] source=%s id=%s reason=%s saved=%s",
            source, project_id, reason, base
        )
    except Exception as exc:  # noqa: BLE001 - diagnostics must never break the run
        log.info("[BID][BROWSER-DEBUG] capture failed error_type=%s", type(exc).__name__)


async def _find_open_form_cta(
    page: Any,
    source_name: str,
    project_id: str,
    current_url: str,
    *,
    form_id: str = "",
    diagnostics: dict[str, Any] | None = None,
) -> Any | None:
    """Find the opener for this exact project without guessing between cards.

    ParsCoders has used links, buttons, delegated jQuery handlers, custom
    role=button elements, and Bootstrap/off-canvas triggers. Inspect the live
    DOM and accept only a uniquely project-bound trigger or one unambiguous
    CTA on the validated detail page.
    """
    if source_name.casefold() != "parscoders":
        selectors = [
            "button.bid-send--button:visible",
            "a.bid-send--button:visible",
            "button:has-text('ارسال پیشنهاد'):visible",
            "a:has-text('ارسال پیشنهاد'):visible",
        ]
        for selector in selectors:
            try:
                group = page.locator(selector)
                if await group.count() == 1 and await group.first.is_visible():
                    return group.first
            except Exception:  # noqa: BLE001
                continue
        return None

    page_id_match = re.fullmatch(r"/project/(\d+)(?:/[^/]+)?/?", urlsplit(current_url).path)
    if not str(project_id).isdigit() or not page_id_match or page_id_match.group(1) != str(project_id):
        if diagnostics is not None:
            diagnostics.update({"page_matches_project": False, "reason": "detail_page_id_mismatch"})
        return None

    selector = (
        "button, a, [role='button'], [onclick], [data-bs-toggle], [data-toggle], "
        "[class*='bid-send'], [id*='bid'], [data-project-id]"
    )
    try:
        result = await page.evaluate(
            """({selector, projectId, formId}) => {
              const nodes = [...document.querySelectorAll(selector)];
              const endpoint = '/conversation/ajax/bid-send/' + projectId;
              const projectPath = '/project/' + projectId;
              const currentPath = location.pathname.replace(/\\/+$/, '') || '/';
              const form = formId ? document.getElementById(formId) : null;
              const visible = el => {
                const r = el.getBoundingClientRect(), s = getComputedStyle(el);
                return s.display !== 'none' && s.visibility !== 'hidden' &&
                  Number(s.opacity || 1) > 0 && r.width > 0 && r.height > 0 &&
                  r.right > 0 && r.bottom > 0 && r.left < innerWidth && r.top < innerHeight;
              };
              const norm = value => String(value || '').replace(/[\\u200c\\u200f\\s]+/g, '').toLowerCase();
              const strong = [], weak = [];
              let visibleCount = 0, semanticCount = 0;
              const samples = [];
              nodes.forEach((el, index) => {
                if (!visible(el) || el.disabled) return;
                visibleCount++;
                const tag = el.tagName.toLowerCase();
                const cls = typeof el.className === 'string' ? el.className : '';
                const identity = String(el.id || '') + ' ' + cls;
                const label = norm([
                  el.innerText, el.getAttribute('aria-label'), el.getAttribute('title'),
                  el.getAttribute('value')
                ].filter(Boolean).join(' '));
                const labelMatch = /ارسالپیشنهاد|ثبتپیشنهاد|پیشنهادداده|submitproposal|placebid|sendproposal/.test(label);
                const identityMatch = /bid|proposal|offer|place/i.test(identity);
                const owner = el.getAttribute('data-project-id') ||
                  (el.closest('[data-project-id]') && el.closest('[data-project-id]').getAttribute('data-project-id')) || '';
                const rawHref = el.getAttribute('href') || '';
                const rawDataTarget = el.getAttribute('data-href') || el.getAttribute('data-url') ||
                  el.getAttribute('data-action') || '';
                const rawToggleTarget = el.getAttribute('data-bs-target') || el.getAttribute('data-target') || '';
                const rawTarget = rawHref || rawDataTarget;
                let targetPath = '', targetForeignOrigin = false, targetIsUnsafe = false;
                if (rawTarget && !/^javascript:/i.test(rawTarget) && !rawTarget.startsWith('#')) {
                  try {
                    const targetUrl = new URL(rawTarget, location.href);
                    targetPath = targetUrl.pathname.replace(/\\/+$/, '') || '/';
                    targetForeignOrigin = targetUrl.origin !== location.origin;
                  } catch (_) { targetIsUnsafe = true; }
                } else if (rawTarget.startsWith('#')) {
                  targetPath = currentPath;
                }
                const numericIds = [...targetPath.matchAll(/(?:^|\\/)(\\d+)(?=\\/|$)/g)].map(match => match[1]);
                const foreignNumericId = numericIds.some(value => value !== String(projectId));
                const allowedTarget = !targetPath || targetPath === currentPath ||
                  targetPath === projectPath || targetPath === endpoint;
                if (targetPath && !allowedTarget) targetIsUnsafe = true;
                if (targetForeignOrigin || foreignNumericId) targetIsUnsafe = true;
                if (owner && owner !== String(projectId)) return;

                const controlled = (el.getAttribute('aria-controls') || '').split(/\\s+/).filter(Boolean);
                const targetSelectors = [rawToggleTarget, ...controlled.map(id => '#' + CSS.escape(id))]
                  .filter(Boolean);
                let linkedToForm = Boolean(form && controlled.includes(formId));
                for (const rawSelector of targetSelectors) {
                  try {
                    const targetEl = rawSelector.startsWith('#')
                      ? document.getElementById(rawSelector.slice(1))
                      : document.querySelector(rawSelector);
                    if (form && targetEl && (targetEl === form || targetEl.contains(form) || form.contains(targetEl))) {
                      linkedToForm = true;
                    }
                  } catch (_) {}
                }
                const exactProjectPath = new RegExp('^/project/' + projectId + '(?:/[^/]+)?
                const exactEndpoint = targetPath === endpoint;
                const exactProject = owner === String(projectId) || exactProjectPath || exactEndpoint || linkedToForm;
                const clickable = ['button', 'a'].includes(tag) ||
                  el.getAttribute('role') === 'button' || el.hasAttribute('onclick') ||
                  el.hasAttribute('data-bs-toggle') || el.hasAttribute('data-toggle') ||
                  /bid|proposal|offer|btn|button|trigger|action/i.test(identity);
                if (!clickable || (!labelMatch && !identityMatch && !exactProject) || targetIsUnsafe) return;
                semanticCount++;
                const item = {
                  index, tag, label: label.slice(0, 90), id: el.id || '',
                  project_bound: Boolean(owner), linked_to_form: linkedToForm,
                  exact_path: exactProjectPath || exactEndpoint
                };
                if (samples.length < 12) samples.push(item);
                if (exactProject) strong.push(item);
                else if (labelMatch || identityMatch) weak.push(item);
              });
              let selected = null, strategy = '';
              if (strong.length === 1) {
                selected = strong[0];
                strategy = selected.linked_to_form ? 'form_target' :
                  (selected.project_bound ? 'project_id' : 'exact_project_path');
              } else if (strong.length > 1) {
                const primary = strong.filter(item => ['button', 'a'].includes(item.tag));
                if (primary.length === 1) {
                  selected = primary[0];
                  strategy = 'unique_native_control_among_project_matches';
                }
              } else if (weak.length === 1) {
                selected = weak[0];
                strategy = 'unique_cta_on_project_detail';
              }
              return {
                selected_index: selected ? selected.index : null,
                selected_strategy: strategy,
                page_matches_project: true,
                visible_actionables: visibleCount,
                semantic_candidates: semanticCount,
                strong_matches: strong.length,
                weak_matches: weak.length,
                candidates: samples
              };
            }""",
            {"selector": selector, "projectId": str(project_id), "formId": str(form_id or "")},
        )
    except Exception as exc:  # noqa: BLE001 - a failed inspection is a safe no-click outcome
        if diagnostics is not None:
            diagnostics.update({"page_matches_project": True, "inspection_error": type(exc).__name__})
        log.warning(
            "[BID][CTA-DISCOVERY] source=%s id=%s outcome=inspection_failed error_type=%s",
            source_name,
            project_id,
            type(exc).__name__,
        )
        return None

    if diagnostics is not None:
        diagnostics.update(result)
    selected_index = result.get("selected_index")
    if selected_index is None:
        log.warning(
            "[BID][CTA-DISCOVERY] source=%s id=%s outcome=ambiguous_or_missing "
            "visible=%s semantic=%s strong=%s weak=%s",
            source_name,
            project_id,
            result.get("visible_actionables", 0),
            result.get("semantic_candidates", 0),
            result.get("strong_matches", 0),
            result.get("weak_matches", 0),
        )
        return None

    selected = next(
        (item for item in result.get("candidates", []) if item.get("index") == selected_index),
        {},
    )
    log.info(
        "[BID][CTA-DISCOVERY] source=%s id=%s outcome=selected strategy=%s "
        "tag=%s label=%r index=%s strong=%s weak=%s",
        source_name,
        project_id,
        result.get("selected_strategy"),
        selected.get("tag", ""),
        selected.get("label", ""),
        selected_index,
        result.get("strong_matches", 0),
        result.get("weak_matches", 0),
    )
    return page.locator(selector).nth(int(selected_index))


async def _find_submitter(
    page: Any, form: Any, submitter_id: str, endpoint_attribute: str, endpoint_url: str
) -> tuple[Any | None, str]:
    """Find a visible submitter associated with this form.

    The exact ID is searched in the whole document (some sites place a
    form-associated button beside/outside the <form>), then scoped fallbacks
    are checked inside the real bid form. No unrelated page button is clicked.
    """
    form_id = str(await form.get_attribute("id") or "")
    expected_path = urlsplit(endpoint_url).path.rstrip("/")
    candidates: list[tuple[str, Any, bool]] = []
    if submitter_id:
        candidates.append(("id", page.locator(_attr_selector("id", submitter_id)).first, True))
    if endpoint_attribute and expected_path:
        candidates.append(("endpoint", form.locator(f"[{endpoint_attribute}]").first, False))
    candidates.extend([
        ("type=submit", form.locator("button[type=submit], input[type=submit]").first, False),
        ("text", form.locator("button").filter(has_text=re.compile("ارسال|ثبت پیشنهاد")).first, False),
        ("input-button", form.locator("input[type=button][value*='ارسال']").first, False),
        ("button", form.locator("button").first, False),
    ])

    seen: dict[str, str] = {}
    for _ in range(8):
        for label, loc, require_association in candidates:
            try:
                if not await loc.count():
                    seen[label] = "پیدا نشد"
                    continue
                if not await loc.is_visible():
                    seen[label] = "پنهان"
                    continue
                if require_association:
                    associated_form_id = await loc.evaluate(
                        """(el) => {
                          const owner = el.form || el.closest("form");
                          return owner ? String(owner.id || "") : "";
                        }"""
                    )
                    associated = bool(form_id) and associated_form_id == form_id
                    if not associated:
                        seen[label] = "به فرم متصل نیست"
                        continue
                attr_value = await loc.get_attribute(endpoint_attribute) if endpoint_attribute else None
                if attr_value:
                    actual_path = urlsplit(urljoin(str(page.url), attr_value)).path.rstrip("/")
                    if actual_path != expected_path:
                        seen[label] = "مسیر متفاوت"
                        continue
                await loc.scroll_into_view_if_needed(timeout=1_500)
                if await loc.is_visible():
                    return loc, ""
                seen[label] = "پس از اسکرول پنهان"
            except Exception as exc:  # noqa: BLE001
                seen[label] = type(exc).__name__
        await page.wait_for_timeout(500)
    try:
        form_visible = await form.is_visible()
    except Exception:
        form_visible = None
    detail = "، ".join(f"{key}: {value}" for key, value in seen.items())
    return None, f"{detail}؛ فرم {'نمایان' if form_visible else 'پنهان'}"


_STAGE_FA = {
    "launch": "راه‌اندازی مرورگر",
    "page_load": "بازشدن صفحهٔ پروژه",
    "page_check": "بررسی صفحه",
    "find_form": "پیداکردن فرم",
    "open_form": "بازکردن پنل ارسال پیشنهاد",
    "fill": "پرکردن فرم",
    "validate": "اعتبارسنجی فرم",
    "submit_button": "آماده‌شدن دکمهٔ ارسال",
}
#: stages after which nothing was clicked, so a fresh browser can safely try again
_RETRYABLE_STAGES = {"launch", "page_load"}
_NOISE_RESOURCE_TYPES = {"image", "media", "font"}
_TRACKER_HOSTS = (
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "googlesyndication.com",
    "connect.facebook.net",
    "hotjar.com",
    "clarity.ms",
    "mc.yandex.ru",
    "metrika",
)


async def _block_page_noise(context: Any) -> None:
    """Skip images, fonts, media and analytics: the bid form needs none of them.

    On slow or filtered networks these (not the form) are what keeps a page
    "loading" for half a minute. Scripts and styles stay untouched so the site's
    own form logic and layout work exactly as for a person.
    """

    async def handler(route: Any) -> None:
        request = route.request
        if request.resource_type in _NOISE_RESOURCE_TYPES or any(host in request.url for host in _TRACKER_HOSTS):
            await route.abort()
        else:
            await route.continue_()

    await context.route("**/*", handler)


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
        page_match = re.fullmatch(r"/project/(\d+)(?:/[^/]+)?/?", page_parts.path)
        if not str(project_id).isdigit() or not page_match or page_match.group(1) != str(project_id):
            return _failure(
                "آدرس مرورگر، شناسه پروژه و مقصد ارسال با هم تطابق ندارند؛ هیچ درخواستی انجام نشد.",
                debug=safe_debug,
            )
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
    stage = "launch"
    page = None
    form_ref = None
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
            if bid_cfg.get("browser_block_noise", True):
                await _block_page_noise(context)
            page = await context.new_page()
            page.set_default_timeout(12_000)

            def _on_page_error(error: Exception) -> None:
                log.warning(
                    "[BID][BROWSER-PAGE-ERROR] source=%s id=%s error_type=%s detail=%r",
                    source_name,
                    project_id,
                    type(error).__name__,
                    str(error).splitlines()[0][:180] if str(error) else "",
                )

            def _on_request_failed(request: Any) -> None:
                if request.resource_type in _NOISE_RESOURCE_TYPES or any(
                    host in request.url for host in _TRACKER_HOSTS
                ):
                    return
                log.warning(
                    "[BID][BROWSER-REQUEST-FAILED] source=%s id=%s method=%s resource=%s path=%s failure=%r",
                    source_name,
                    project_id,
                    request.method,
                    request.resource_type,
                    urlsplit(request.url).path,
                    str(request.failure or "")[:160],
                )

            page.on("pageerror", _on_page_error)
            page.on("requestfailed", _on_request_failed)
            try:
                stage = "page_load"
                load_timeout_ms = int(float(bid_cfg.get("page_load_timeout_seconds") or 30) * 1000)
                # "commit" = the server answered; do not wait for every sub-resource. A slow
                # third-party file must not turn a usable page into a 25 s timeout.
                navigation_started = time.monotonic()
                navigation = await page.goto(page_url, wait_until="commit", timeout=load_timeout_ms)
                dom_ready = False
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=4_000)
                    dom_ready = True
                except PlaywrightTimeoutError:
                    # Some marketplace pages keep resources open indefinitely. The
                    # form/DOM can still be usable; inspect document.readyState rather
                    # than waiting another 6-10 seconds for window.load.
                    pass
                page_state = await page.evaluate(
                    """() => ({
                      ready_state: document.readyState,
                      body_present: Boolean(document.body),
                      forms: document.forms.length,
                      title_chars: (document.title || '').length
                    })"""
                )
                safe_debug["navigation"] = {
                    "http_status": navigation.status if navigation is not None else None,
                    "dom_content_loaded": dom_ready,
                    **page_state,
                    "elapsed_ms": int((time.monotonic() - navigation_started) * 1000),
                }
                log.info(
                    "[BID][BROWSER-NAVIGATION] source=%s id=%s status=%s dom_ready=%s "
                    "ready_state=%s forms=%s elapsed_ms=%s",
                    source_name,
                    project_id,
                    navigation.status if navigation is not None else "unknown",
                    dom_ready,
                    page_state.get("ready_state"),
                    page_state.get("forms"),
                    safe_debug["navigation"]["elapsed_ms"],
                )
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

                stage = "page_check"
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
                stage = "find_form"
                bid_cfg["_debug_form_id"] = form_id
                form = page.locator(form_selector).first
                form_ref = form
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

                # The form may live in a hidden off-canvas. Open it only via
                # a CTA tied to this project's numeric ID, then require visibility.
                stage = "open_form"
                if not await form.is_visible():
                    cta_diagnostics: dict[str, Any] = {}
                    cta = await _find_open_form_cta(
                        page,
                        source_name,
                        str(project_id),
                        current_url,
                        form_id=form_id,
                        diagnostics=cta_diagnostics,
                    )
                    safe_debug["cta_discovery"] = cta_diagnostics
                    if cta is None:
                        log.error(
                            "[BID][CTA-NOT-FOUND] source=%s id=%s page_path=%s form_id=%s diagnostics=%s",
                            source_name,
                            project_id,
                            urlsplit(current_url).path,
                            form_id,
                            {
                                key: cta_diagnostics.get(key)
                                for key in ("visible_actionables", "semantic_candidates", "strong_matches", "weak_matches", "reason")
                            },
                        )
                        await _dump_debug(page, form, bid_cfg, source_name, project_id, "project-cta-not-found")
                        return _failure(
                            "دکمه بازکردن فرم برای همین پروژه پیدا نشد؛ لینک‌های پروژه‌های مرتبط نادیده گرفته شدند.",
                            debug=safe_debug,
                        )
                    await cta.scroll_into_view_if_needed(timeout=3_000)
                    await cta.click(timeout=8_000)
                    open_timeout = max(250, min(30_000, int(bid_cfg.get("form_open_timeout_ms") or 12_000)))
                    try:
                        await form.wait_for(state="visible", timeout=open_timeout)
                    except Exception:
                        safe_debug["form_open_timeout_ms"] = open_timeout
                        await _dump_debug(page, form, bid_cfg, source_name, project_id, "form-did-not-open")
                        return _failure(
                            "پس از کلیک روی دکمه مخصوص پروژه، پنل ارسال واقعاً باز نشد؛ "
                            "هیچ فیلدی تغییر نکرد و پیشنهادی ارسال نشد.",
                            debug=safe_debug,
                        )
                    await page.wait_for_timeout(500)
                if not await form.is_visible():
                    await _dump_debug(page, form, bid_cfg, source_name, project_id, "form-still-hidden")
                    return _failure(
                        "پنل ارسال هنوز مخفی است؛ برای جلوگیری از تغییر فرم نامرئی، ارسال متوقف شد.",
                        debug=safe_debug,
                    )
                try:
                    await form.scroll_into_view_if_needed(timeout=3_000)
                except Exception:
                    pass

                if not await form.is_visible():
                    await _dump_debug(page, form, bid_cfg, source_name, project_id, "form-hidden-before-fill")
                    return _failure(
                        "فرم پیشنهاد پیش از تکمیل دوباره مخفی شد؛ هیچ فیلدی تغییر نکرد.",
                        debug=safe_debug,
                    )

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

                stage = "fill"
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

                stage = "validate"
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

                submitter, submit_diag = await _find_submitter(
                    page, form, submitter_id, endpoint_attribute, endpoint_url
                )
                if submitter is None:
                    log.warning(
                        "[BID][BROWSER-SUBMIT-NOT-FOUND] source=%s id=%s diag=%s",
                        source_name,
                        project_id,
                        submit_diag,
                    )
                    safe_debug["submit_button"] = submit_diag
                    await _dump_debug(page, form, bid_cfg, source_name, project_id, "submit-not-found")
                    return _failure(
                        "دکمهٔ «ارسال پیشنهاد» بعد از پر شدن فرم دیده نشد "
                        f"({submit_diag}). هیچ پیشنهادی ارسال نشد؛ احتمالاً چیدمان صفحهٔ سایت عوض شده یا "
                        "صفحه کامل بارگذاری نشده. اسکرین‌شات در data/browser_debug ذخیره شد.",
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
                if not action_clicked and page is not None:
                    await _dump_debug(page, form_ref, bid_cfg, source_name, project_id, f"timeout-{stage}")
                raise
    except PlaywrightTimeoutError:
        safe_debug["request_sent"] = "unknown" if action_clicked else False
        safe_debug["stage"] = stage
        retryable = (not action_clicked) and stage in _RETRYABLE_STAGES
        safe_debug["retryable"] = retryable
        if retryable:
            safe_debug["retry_reason"] = f"timeout_before_submit:{stage}"
        log.warning(
            "[BID][BROWSER-SUBMIT] source=%s id=%s outcome=timeout stage=%s clicked=%s will_retry=%s",
            source_name,
            project_id,
            stage,
            action_clicked,
            retryable,
        )
        where = _STAGE_FA.get(stage, stage)
        return _failure(
            f"مرورگر در مرحلهٔ «{where}» زمان کافی نداشت (شبکه کند یا سایت دیر پاسخ می‌دهد)؛ "
            + (
                "پیشنهادی ثبت نشد و یک‌بار با مرورگر تازه دوباره تلاش می‌شود."
                if retryable
                else "کلیک ارسال انجام شده و نتیجه قطعی نیست؛ وضعیت پروژه را از صفحهٔ سایت بررسی کن."
            ),
            pause_source=action_clicked,
            debug=safe_debug,
        )
    except Exception as exc:
        safe_debug["request_sent"] = "unknown" if action_clicked else False
        # A browser/page that died before any click is safe to retry once on a fresh browser.
        safe_debug["retryable"] = (not action_clicked) and type(exc).__name__ == "TargetClosedError"
        if safe_debug["retryable"]:
            safe_debug["retry_reason"] = f"browser_closed_before_submit:{stage}"
        log.warning(
            "[BID][BROWSER-SUBMIT] source=%s id=%s outcome=browser_error stage=%s error_type=%s detail=%r clicked=%s",
            source_name,
            project_id,
            stage,
            type(exc).__name__,
            str(exc).splitlines()[0][:180] if str(exc) else "",
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
        _browser_lock = PriorityLock()
    # paid plans are served first when several users/sites wait for the one browser
    async with _browser_lock.hold(submit_priority.get()):
        result = await _submit_once(**kwargs)
        if (result.payload_debug or {}).get("retryable"):
            log.warning(
                "[BID][BROWSER-RETRY] source=%s id=%s reason=%s",
                kwargs.get("source_name"),
                kwargs.get("project_id"),
                (result.payload_debug or {}).get("retry_reason", "safe_pre_submit_retry"),
            )
            await asyncio.sleep(1.0)
            result = await _submit_once(**kwargs)
        if result.payload_debug:
            result.payload_debug.pop("retryable", None)
        return result
).test(targetPath) ||
                  (targetPath === currentPath && new RegExp('^/project/' + projectId + '(?:/[^/]+)?
                const exactEndpoint = targetPath === endpoint;
                const exactProject = owner === String(projectId) || exactProjectPath || exactEndpoint || linkedToForm;
                const clickable = ['button', 'a'].includes(tag) ||
                  el.getAttribute('role') === 'button' || el.hasAttribute('onclick') ||
                  el.hasAttribute('data-bs-toggle') || el.hasAttribute('data-toggle') ||
                  /bid-send/i.test(identity) || Boolean(owner);
                if (!clickable || (!labelMatch && !identityMatch && !exactProject) || targetIsUnsafe) return;
                semanticCount++;
                const item = {
                  index, tag, label: label.slice(0, 90), id: el.id || '',
                  project_bound: Boolean(owner), linked_to_form: linkedToForm,
                  exact_path: exactProjectPath || exactEndpoint
                };
                if (samples.length < 12) samples.push(item);
                if (exactProject) strong.push(item);
                else if (labelMatch || identityMatch) weak.push(item);
              });
              let selected = null, strategy = '';
              if (strong.length === 1) {
                selected = strong[0];
                strategy = selected.linked_to_form ? 'form_target' :
                  (selected.project_bound ? 'project_id' : 'exact_project_path');
              } else if (strong.length > 1) {
                const primary = strong.filter(item => ['button', 'a'].includes(item.tag));
                if (primary.length === 1) {
                  selected = primary[0];
                  strategy = 'unique_native_control_among_project_matches';
                }
              } else if (weak.length === 1) {
                selected = weak[0];
                strategy = 'unique_cta_on_project_detail';
              }
              return {
                selected_index: selected ? selected.index : null,
                selected_strategy: strategy,
                page_matches_project: true,
                visible_actionables: visibleCount,
                semantic_candidates: semanticCount,
                strong_matches: strong.length,
                weak_matches: weak.length,
                candidates: samples
              };
            }""",
            {"selector": selector, "projectId": str(project_id), "formId": str(form_id or "")},
        )
    except Exception as exc:  # noqa: BLE001 - a failed inspection is a safe no-click outcome
        if diagnostics is not None:
            diagnostics.update({"page_matches_project": True, "inspection_error": type(exc).__name__})
        log.warning(
            "[BID][CTA-DISCOVERY] source=%s id=%s outcome=inspection_failed error_type=%s",
            source_name,
            project_id,
            type(exc).__name__,
        )
        return None

    if diagnostics is not None:
        diagnostics.update(result)
    selected_index = result.get("selected_index")
    if selected_index is None:
        log.warning(
            "[BID][CTA-DISCOVERY] source=%s id=%s outcome=ambiguous_or_missing "
            "visible=%s semantic=%s strong=%s weak=%s",
            source_name,
            project_id,
            result.get("visible_actionables", 0),
            result.get("semantic_candidates", 0),
            result.get("strong_matches", 0),
            result.get("weak_matches", 0),
        )
        return None

    selected = next(
        (item for item in result.get("candidates", []) if item.get("index") == selected_index),
        {},
    )
    log.info(
        "[BID][CTA-DISCOVERY] source=%s id=%s outcome=selected strategy=%s "
        "tag=%s label=%r index=%s strong=%s weak=%s",
        source_name,
        project_id,
        result.get("selected_strategy"),
        selected.get("tag", ""),
        selected.get("label", ""),
        selected_index,
        result.get("strong_matches", 0),
        result.get("weak_matches", 0),
    )
    return page.locator(selector).nth(int(selected_index))


async def _find_submitter(
    page: Any, form: Any, submitter_id: str, endpoint_attribute: str, endpoint_url: str
) -> tuple[Any | None, str]:
    """Find a visible submitter associated with this form.

    The exact ID is searched in the whole document (some sites place a
    form-associated button beside/outside the <form>), then scoped fallbacks
    are checked inside the real bid form. No unrelated page button is clicked.
    """
    form_id = str(await form.get_attribute("id") or "")
    expected_path = urlsplit(endpoint_url).path.rstrip("/")
    candidates: list[tuple[str, Any, bool]] = []
    if submitter_id:
        candidates.append(("id", page.locator(_attr_selector("id", submitter_id)).first, True))
    if endpoint_attribute and expected_path:
        candidates.append(("endpoint", form.locator(f"[{endpoint_attribute}]").first, False))
    candidates.extend([
        ("type=submit", form.locator("button[type=submit], input[type=submit]").first, False),
        ("text", form.locator("button").filter(has_text=re.compile("ارسال|ثبت پیشنهاد")).first, False),
        ("input-button", form.locator("input[type=button][value*='ارسال']").first, False),
        ("button", form.locator("button").first, False),
    ])

    seen: dict[str, str] = {}
    for _ in range(8):
        for label, loc, require_association in candidates:
            try:
                if not await loc.count():
                    seen[label] = "پیدا نشد"
                    continue
                if not await loc.is_visible():
                    seen[label] = "پنهان"
                    continue
                if require_association:
                    associated_form_id = await loc.evaluate(
                        """(el) => {
                          const owner = el.form || el.closest("form");
                          return owner ? String(owner.id || "") : "";
                        }"""
                    )
                    associated = bool(form_id) and associated_form_id == form_id
                    if not associated:
                        seen[label] = "به فرم متصل نیست"
                        continue
                attr_value = await loc.get_attribute(endpoint_attribute) if endpoint_attribute else None
                if attr_value:
                    actual_path = urlsplit(urljoin(str(page.url), attr_value)).path.rstrip("/")
                    if actual_path != expected_path:
                        seen[label] = "مسیر متفاوت"
                        continue
                await loc.scroll_into_view_if_needed(timeout=1_500)
                if await loc.is_visible():
                    return loc, ""
                seen[label] = "پس از اسکرول پنهان"
            except Exception as exc:  # noqa: BLE001
                seen[label] = type(exc).__name__
        await page.wait_for_timeout(500)
    try:
        form_visible = await form.is_visible()
    except Exception:
        form_visible = None
    detail = "، ".join(f"{key}: {value}" for key, value in seen.items())
    return None, f"{detail}؛ فرم {'نمایان' if form_visible else 'پنهان'}"


_STAGE_FA = {
    "launch": "راه‌اندازی مرورگر",
    "page_load": "بازشدن صفحهٔ پروژه",
    "page_check": "بررسی صفحه",
    "find_form": "پیداکردن فرم",
    "open_form": "بازکردن پنل ارسال پیشنهاد",
    "fill": "پرکردن فرم",
    "validate": "اعتبارسنجی فرم",
    "submit_button": "آماده‌شدن دکمهٔ ارسال",
}
#: stages after which nothing was clicked, so a fresh browser can safely try again
_RETRYABLE_STAGES = {"launch", "page_load"}
_NOISE_RESOURCE_TYPES = {"image", "media", "font"}
_TRACKER_HOSTS = (
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "googlesyndication.com",
    "connect.facebook.net",
    "hotjar.com",
    "clarity.ms",
    "mc.yandex.ru",
    "metrika",
)


async def _block_page_noise(context: Any) -> None:
    """Skip images, fonts, media and analytics: the bid form needs none of them.

    On slow or filtered networks these (not the form) are what keeps a page
    "loading" for half a minute. Scripts and styles stay untouched so the site's
    own form logic and layout work exactly as for a person.
    """

    async def handler(route: Any) -> None:
        request = route.request
        if request.resource_type in _NOISE_RESOURCE_TYPES or any(host in request.url for host in _TRACKER_HOSTS):
            await route.abort()
        else:
            await route.continue_()

    await context.route("**/*", handler)


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
        page_match = re.fullmatch(r"/project/(\d+)(?:/[^/]+)?/?", page_parts.path)
        if not str(project_id).isdigit() or not page_match or page_match.group(1) != str(project_id):
            return _failure(
                "آدرس مرورگر، شناسه پروژه و مقصد ارسال با هم تطابق ندارند؛ هیچ درخواستی انجام نشد.",
                debug=safe_debug,
            )
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
    stage = "launch"
    page = None
    form_ref = None
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
            if bid_cfg.get("browser_block_noise", True):
                await _block_page_noise(context)
            page = await context.new_page()
            page.set_default_timeout(12_000)

            def _on_page_error(error: Exception) -> None:
                log.warning(
                    "[BID][BROWSER-PAGE-ERROR] source=%s id=%s error_type=%s detail=%r",
                    source_name,
                    project_id,
                    type(error).__name__,
                    str(error).splitlines()[0][:180] if str(error) else "",
                )

            def _on_request_failed(request: Any) -> None:
                if request.resource_type in _NOISE_RESOURCE_TYPES or any(
                    host in request.url for host in _TRACKER_HOSTS
                ):
                    return
                log.warning(
                    "[BID][BROWSER-REQUEST-FAILED] source=%s id=%s method=%s resource=%s path=%s failure=%r",
                    source_name,
                    project_id,
                    request.method,
                    request.resource_type,
                    urlsplit(request.url).path,
                    str(request.failure or "")[:160],
                )

            page.on("pageerror", _on_page_error)
            page.on("requestfailed", _on_request_failed)
            try:
                stage = "page_load"
                load_timeout_ms = int(float(bid_cfg.get("page_load_timeout_seconds") or 30) * 1000)
                # "commit" = the server answered; do not wait for every sub-resource. A slow
                # third-party file must not turn a usable page into a 25 s timeout.
                navigation_started = time.monotonic()
                navigation = await page.goto(page_url, wait_until="commit", timeout=load_timeout_ms)
                dom_ready = False
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=4_000)
                    dom_ready = True
                except PlaywrightTimeoutError:
                    # Some marketplace pages keep resources open indefinitely. The
                    # form/DOM can still be usable; inspect document.readyState rather
                    # than waiting another 6-10 seconds for window.load.
                    pass
                page_state = await page.evaluate(
                    """() => ({
                      ready_state: document.readyState,
                      body_present: Boolean(document.body),
                      forms: document.forms.length,
                      title_chars: (document.title || '').length
                    })"""
                )
                safe_debug["navigation"] = {
                    "http_status": navigation.status if navigation is not None else None,
                    "dom_content_loaded": dom_ready,
                    **page_state,
                    "elapsed_ms": int((time.monotonic() - navigation_started) * 1000),
                }
                log.info(
                    "[BID][BROWSER-NAVIGATION] source=%s id=%s status=%s dom_ready=%s "
                    "ready_state=%s forms=%s elapsed_ms=%s",
                    source_name,
                    project_id,
                    navigation.status if navigation is not None else "unknown",
                    dom_ready,
                    page_state.get("ready_state"),
                    page_state.get("forms"),
                    safe_debug["navigation"]["elapsed_ms"],
                )
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

                stage = "page_check"
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
                stage = "find_form"
                bid_cfg["_debug_form_id"] = form_id
                form = page.locator(form_selector).first
                form_ref = form
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

                # The form may live in a hidden off-canvas. Open it only via
                # a CTA tied to this project's numeric ID, then require visibility.
                stage = "open_form"
                if not await form.is_visible():
                    cta_diagnostics: dict[str, Any] = {}
                    cta = await _find_open_form_cta(
                        page,
                        source_name,
                        str(project_id),
                        current_url,
                        form_id=form_id,
                        diagnostics=cta_diagnostics,
                    )
                    safe_debug["cta_discovery"] = cta_diagnostics
                    if cta is None:
                        log.error(
                            "[BID][CTA-NOT-FOUND] source=%s id=%s page_path=%s form_id=%s diagnostics=%s",
                            source_name,
                            project_id,
                            urlsplit(current_url).path,
                            form_id,
                            {
                                key: cta_diagnostics.get(key)
                                for key in ("visible_actionables", "semantic_candidates", "strong_matches", "weak_matches", "reason")
                            },
                        )
                        await _dump_debug(page, form, bid_cfg, source_name, project_id, "project-cta-not-found")
                        return _failure(
                            "دکمه بازکردن فرم برای همین پروژه پیدا نشد؛ لینک‌های پروژه‌های مرتبط نادیده گرفته شدند.",
                            debug=safe_debug,
                        )
                    await cta.scroll_into_view_if_needed(timeout=3_000)
                    await cta.click(timeout=8_000)
                    open_timeout = max(250, min(30_000, int(bid_cfg.get("form_open_timeout_ms") or 12_000)))
                    try:
                        await form.wait_for(state="visible", timeout=open_timeout)
                    except Exception:
                        safe_debug["form_open_timeout_ms"] = open_timeout
                        await _dump_debug(page, form, bid_cfg, source_name, project_id, "form-did-not-open")
                        return _failure(
                            "پس از کلیک روی دکمه مخصوص پروژه، پنل ارسال واقعاً باز نشد؛ "
                            "هیچ فیلدی تغییر نکرد و پیشنهادی ارسال نشد.",
                            debug=safe_debug,
                        )
                    await page.wait_for_timeout(500)
                if not await form.is_visible():
                    await _dump_debug(page, form, bid_cfg, source_name, project_id, "form-still-hidden")
                    return _failure(
                        "پنل ارسال هنوز مخفی است؛ برای جلوگیری از تغییر فرم نامرئی، ارسال متوقف شد.",
                        debug=safe_debug,
                    )
                try:
                    await form.scroll_into_view_if_needed(timeout=3_000)
                except Exception:
                    pass

                if not await form.is_visible():
                    await _dump_debug(page, form, bid_cfg, source_name, project_id, "form-hidden-before-fill")
                    return _failure(
                        "فرم پیشنهاد پیش از تکمیل دوباره مخفی شد؛ هیچ فیلدی تغییر نکرد.",
                        debug=safe_debug,
                    )

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

                stage = "fill"
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

                stage = "validate"
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

                submitter, submit_diag = await _find_submitter(
                    page, form, submitter_id, endpoint_attribute, endpoint_url
                )
                if submitter is None:
                    log.warning(
                        "[BID][BROWSER-SUBMIT-NOT-FOUND] source=%s id=%s diag=%s",
                        source_name,
                        project_id,
                        submit_diag,
                    )
                    safe_debug["submit_button"] = submit_diag
                    await _dump_debug(page, form, bid_cfg, source_name, project_id, "submit-not-found")
                    return _failure(
                        "دکمهٔ «ارسال پیشنهاد» بعد از پر شدن فرم دیده نشد "
                        f"({submit_diag}). هیچ پیشنهادی ارسال نشد؛ احتمالاً چیدمان صفحهٔ سایت عوض شده یا "
                        "صفحه کامل بارگذاری نشده. اسکرین‌شات در data/browser_debug ذخیره شد.",
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
                if not action_clicked and page is not None:
                    await _dump_debug(page, form_ref, bid_cfg, source_name, project_id, f"timeout-{stage}")
                raise
    except PlaywrightTimeoutError:
        safe_debug["request_sent"] = "unknown" if action_clicked else False
        safe_debug["stage"] = stage
        retryable = (not action_clicked) and stage in _RETRYABLE_STAGES
        safe_debug["retryable"] = retryable
        if retryable:
            safe_debug["retry_reason"] = f"timeout_before_submit:{stage}"
        log.warning(
            "[BID][BROWSER-SUBMIT] source=%s id=%s outcome=timeout stage=%s clicked=%s will_retry=%s",
            source_name,
            project_id,
            stage,
            action_clicked,
            retryable,
        )
        where = _STAGE_FA.get(stage, stage)
        return _failure(
            f"مرورگر در مرحلهٔ «{where}» زمان کافی نداشت (شبکه کند یا سایت دیر پاسخ می‌دهد)؛ "
            + (
                "پیشنهادی ثبت نشد و یک‌بار با مرورگر تازه دوباره تلاش می‌شود."
                if retryable
                else "کلیک ارسال انجام شده و نتیجه قطعی نیست؛ وضعیت پروژه را از صفحهٔ سایت بررسی کن."
            ),
            pause_source=action_clicked,
            debug=safe_debug,
        )
    except Exception as exc:
        safe_debug["request_sent"] = "unknown" if action_clicked else False
        # A browser/page that died before any click is safe to retry once on a fresh browser.
        safe_debug["retryable"] = (not action_clicked) and type(exc).__name__ == "TargetClosedError"
        if safe_debug["retryable"]:
            safe_debug["retry_reason"] = f"browser_closed_before_submit:{stage}"
        log.warning(
            "[BID][BROWSER-SUBMIT] source=%s id=%s outcome=browser_error stage=%s error_type=%s detail=%r clicked=%s",
            source_name,
            project_id,
            stage,
            type(exc).__name__,
            str(exc).splitlines()[0][:180] if str(exc) else "",
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
        _browser_lock = PriorityLock()
    # paid plans are served first when several users/sites wait for the one browser
    async with _browser_lock.hold(submit_priority.get()):
        result = await _submit_once(**kwargs)
        if (result.payload_debug or {}).get("retryable"):
            log.warning(
                "[BID][BROWSER-RETRY] source=%s id=%s reason=%s",
                kwargs.get("source_name"),
                kwargs.get("project_id"),
                (result.payload_debug or {}).get("retry_reason", "safe_pre_submit_retry"),
            )
            await asyncio.sleep(1.0)
            result = await _submit_once(**kwargs)
        if result.payload_debug:
            result.payload_debug.pop("retryable", None)
        return result
).test(currentPath));
                const exactEndpoint = targetPath === endpoint;
                const exactProject = owner === String(projectId) || exactProjectPath || exactEndpoint || linkedToForm;
                const clickable = ['button', 'a'].includes(tag) ||
                  el.getAttribute('role') === 'button' || el.hasAttribute('onclick') ||
                  el.hasAttribute('data-bs-toggle') || el.hasAttribute('data-toggle') ||
                  /bid-send/i.test(identity) || Boolean(owner);
                if (!clickable || (!labelMatch && !identityMatch && !exactProject) || targetIsUnsafe) return;
                semanticCount++;
                const item = {
                  index, tag, label: label.slice(0, 90), id: el.id || '',
                  project_bound: Boolean(owner), linked_to_form: linkedToForm,
                  exact_path: exactProjectPath || exactEndpoint
                };
                if (samples.length < 12) samples.push(item);
                if (exactProject) strong.push(item);
                else if (labelMatch || identityMatch) weak.push(item);
              });
              let selected = null, strategy = '';
              if (strong.length === 1) {
                selected = strong[0];
                strategy = selected.linked_to_form ? 'form_target' :
                  (selected.project_bound ? 'project_id' : 'exact_project_path');
              } else if (strong.length > 1) {
                const primary = strong.filter(item => ['button', 'a'].includes(item.tag));
                if (primary.length === 1) {
                  selected = primary[0];
                  strategy = 'unique_native_control_among_project_matches';
                }
              } else if (weak.length === 1) {
                selected = weak[0];
                strategy = 'unique_cta_on_project_detail';
              }
              return {
                selected_index: selected ? selected.index : null,
                selected_strategy: strategy,
                page_matches_project: true,
                visible_actionables: visibleCount,
                semantic_candidates: semanticCount,
                strong_matches: strong.length,
                weak_matches: weak.length,
                candidates: samples
              };
            }""",
            {"selector": selector, "projectId": str(project_id), "formId": str(form_id or "")},
        )
    except Exception as exc:  # noqa: BLE001 - a failed inspection is a safe no-click outcome
        if diagnostics is not None:
            diagnostics.update({"page_matches_project": True, "inspection_error": type(exc).__name__})
        log.warning(
            "[BID][CTA-DISCOVERY] source=%s id=%s outcome=inspection_failed error_type=%s",
            source_name,
            project_id,
            type(exc).__name__,
        )
        return None

    if diagnostics is not None:
        diagnostics.update(result)
    selected_index = result.get("selected_index")
    if selected_index is None:
        log.warning(
            "[BID][CTA-DISCOVERY] source=%s id=%s outcome=ambiguous_or_missing "
            "visible=%s semantic=%s strong=%s weak=%s",
            source_name,
            project_id,
            result.get("visible_actionables", 0),
            result.get("semantic_candidates", 0),
            result.get("strong_matches", 0),
            result.get("weak_matches", 0),
        )
        return None

    selected = next(
        (item for item in result.get("candidates", []) if item.get("index") == selected_index),
        {},
    )
    log.info(
        "[BID][CTA-DISCOVERY] source=%s id=%s outcome=selected strategy=%s "
        "tag=%s label=%r index=%s strong=%s weak=%s",
        source_name,
        project_id,
        result.get("selected_strategy"),
        selected.get("tag", ""),
        selected.get("label", ""),
        selected_index,
        result.get("strong_matches", 0),
        result.get("weak_matches", 0),
    )
    return page.locator(selector).nth(int(selected_index))


async def _find_submitter(
    page: Any, form: Any, submitter_id: str, endpoint_attribute: str, endpoint_url: str
) -> tuple[Any | None, str]:
    """Find a visible submitter associated with this form.

    The exact ID is searched in the whole document (some sites place a
    form-associated button beside/outside the <form>), then scoped fallbacks
    are checked inside the real bid form. No unrelated page button is clicked.
    """
    form_id = str(await form.get_attribute("id") or "")
    expected_path = urlsplit(endpoint_url).path.rstrip("/")
    candidates: list[tuple[str, Any, bool]] = []
    if submitter_id:
        candidates.append(("id", page.locator(_attr_selector("id", submitter_id)).first, True))
    if endpoint_attribute and expected_path:
        candidates.append(("endpoint", form.locator(f"[{endpoint_attribute}]").first, False))
    candidates.extend([
        ("type=submit", form.locator("button[type=submit], input[type=submit]").first, False),
        ("text", form.locator("button").filter(has_text=re.compile("ارسال|ثبت پیشنهاد")).first, False),
        ("input-button", form.locator("input[type=button][value*='ارسال']").first, False),
        ("button", form.locator("button").first, False),
    ])

    seen: dict[str, str] = {}
    for _ in range(8):
        for label, loc, require_association in candidates:
            try:
                if not await loc.count():
                    seen[label] = "پیدا نشد"
                    continue
                if not await loc.is_visible():
                    seen[label] = "پنهان"
                    continue
                if require_association:
                    associated_form_id = await loc.evaluate(
                        """(el) => {
                          const owner = el.form || el.closest("form");
                          return owner ? String(owner.id || "") : "";
                        }"""
                    )
                    associated = bool(form_id) and associated_form_id == form_id
                    if not associated:
                        seen[label] = "به فرم متصل نیست"
                        continue
                attr_value = await loc.get_attribute(endpoint_attribute) if endpoint_attribute else None
                if attr_value:
                    actual_path = urlsplit(urljoin(str(page.url), attr_value)).path.rstrip("/")
                    if actual_path != expected_path:
                        seen[label] = "مسیر متفاوت"
                        continue
                await loc.scroll_into_view_if_needed(timeout=1_500)
                if await loc.is_visible():
                    return loc, ""
                seen[label] = "پس از اسکرول پنهان"
            except Exception as exc:  # noqa: BLE001
                seen[label] = type(exc).__name__
        await page.wait_for_timeout(500)
    try:
        form_visible = await form.is_visible()
    except Exception:
        form_visible = None
    detail = "، ".join(f"{key}: {value}" for key, value in seen.items())
    return None, f"{detail}؛ فرم {'نمایان' if form_visible else 'پنهان'}"


_STAGE_FA = {
    "launch": "راه‌اندازی مرورگر",
    "page_load": "بازشدن صفحهٔ پروژه",
    "page_check": "بررسی صفحه",
    "find_form": "پیداکردن فرم",
    "open_form": "بازکردن پنل ارسال پیشنهاد",
    "fill": "پرکردن فرم",
    "validate": "اعتبارسنجی فرم",
    "submit_button": "آماده‌شدن دکمهٔ ارسال",
}
#: stages after which nothing was clicked, so a fresh browser can safely try again
_RETRYABLE_STAGES = {"launch", "page_load"}
_NOISE_RESOURCE_TYPES = {"image", "media", "font"}
_TRACKER_HOSTS = (
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "googlesyndication.com",
    "connect.facebook.net",
    "hotjar.com",
    "clarity.ms",
    "mc.yandex.ru",
    "metrika",
)


async def _block_page_noise(context: Any) -> None:
    """Skip images, fonts, media and analytics: the bid form needs none of them.

    On slow or filtered networks these (not the form) are what keeps a page
    "loading" for half a minute. Scripts and styles stay untouched so the site's
    own form logic and layout work exactly as for a person.
    """

    async def handler(route: Any) -> None:
        request = route.request
        if request.resource_type in _NOISE_RESOURCE_TYPES or any(host in request.url for host in _TRACKER_HOSTS):
            await route.abort()
        else:
            await route.continue_()

    await context.route("**/*", handler)


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
        page_match = re.fullmatch(r"/project/(\d+)(?:/[^/]+)?/?", page_parts.path)
        if not str(project_id).isdigit() or not page_match or page_match.group(1) != str(project_id):
            return _failure(
                "آدرس مرورگر، شناسه پروژه و مقصد ارسال با هم تطابق ندارند؛ هیچ درخواستی انجام نشد.",
                debug=safe_debug,
            )
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
    stage = "launch"
    page = None
    form_ref = None
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
            if bid_cfg.get("browser_block_noise", True):
                await _block_page_noise(context)
            page = await context.new_page()
            page.set_default_timeout(12_000)

            def _on_page_error(error: Exception) -> None:
                log.warning(
                    "[BID][BROWSER-PAGE-ERROR] source=%s id=%s error_type=%s detail=%r",
                    source_name,
                    project_id,
                    type(error).__name__,
                    str(error).splitlines()[0][:180] if str(error) else "",
                )

            def _on_request_failed(request: Any) -> None:
                if request.resource_type in _NOISE_RESOURCE_TYPES or any(
                    host in request.url for host in _TRACKER_HOSTS
                ):
                    return
                log.warning(
                    "[BID][BROWSER-REQUEST-FAILED] source=%s id=%s method=%s resource=%s path=%s failure=%r",
                    source_name,
                    project_id,
                    request.method,
                    request.resource_type,
                    urlsplit(request.url).path,
                    str(request.failure or "")[:160],
                )

            page.on("pageerror", _on_page_error)
            page.on("requestfailed", _on_request_failed)
            try:
                stage = "page_load"
                load_timeout_ms = int(float(bid_cfg.get("page_load_timeout_seconds") or 30) * 1000)
                # "commit" = the server answered; do not wait for every sub-resource. A slow
                # third-party file must not turn a usable page into a 25 s timeout.
                navigation_started = time.monotonic()
                navigation = await page.goto(page_url, wait_until="commit", timeout=load_timeout_ms)
                dom_ready = False
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=4_000)
                    dom_ready = True
                except PlaywrightTimeoutError:
                    # Some marketplace pages keep resources open indefinitely. The
                    # form/DOM can still be usable; inspect document.readyState rather
                    # than waiting another 6-10 seconds for window.load.
                    pass
                page_state = await page.evaluate(
                    """() => ({
                      ready_state: document.readyState,
                      body_present: Boolean(document.body),
                      forms: document.forms.length,
                      title_chars: (document.title || '').length
                    })"""
                )
                safe_debug["navigation"] = {
                    "http_status": navigation.status if navigation is not None else None,
                    "dom_content_loaded": dom_ready,
                    **page_state,
                    "elapsed_ms": int((time.monotonic() - navigation_started) * 1000),
                }
                log.info(
                    "[BID][BROWSER-NAVIGATION] source=%s id=%s status=%s dom_ready=%s "
                    "ready_state=%s forms=%s elapsed_ms=%s",
                    source_name,
                    project_id,
                    navigation.status if navigation is not None else "unknown",
                    dom_ready,
                    page_state.get("ready_state"),
                    page_state.get("forms"),
                    safe_debug["navigation"]["elapsed_ms"],
                )
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

                stage = "page_check"
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
                stage = "find_form"
                bid_cfg["_debug_form_id"] = form_id
                form = page.locator(form_selector).first
                form_ref = form
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

                # The form may live in a hidden off-canvas. Open it only via
                # a CTA tied to this project's numeric ID, then require visibility.
                stage = "open_form"
                if not await form.is_visible():
                    cta_diagnostics: dict[str, Any] = {}
                    cta = await _find_open_form_cta(
                        page,
                        source_name,
                        str(project_id),
                        current_url,
                        form_id=form_id,
                        diagnostics=cta_diagnostics,
                    )
                    safe_debug["cta_discovery"] = cta_diagnostics
                    if cta is None:
                        log.error(
                            "[BID][CTA-NOT-FOUND] source=%s id=%s page_path=%s form_id=%s diagnostics=%s",
                            source_name,
                            project_id,
                            urlsplit(current_url).path,
                            form_id,
                            {
                                key: cta_diagnostics.get(key)
                                for key in ("visible_actionables", "semantic_candidates", "strong_matches", "weak_matches", "reason")
                            },
                        )
                        await _dump_debug(page, form, bid_cfg, source_name, project_id, "project-cta-not-found")
                        return _failure(
                            "دکمه بازکردن فرم برای همین پروژه پیدا نشد؛ لینک‌های پروژه‌های مرتبط نادیده گرفته شدند.",
                            debug=safe_debug,
                        )
                    await cta.scroll_into_view_if_needed(timeout=3_000)
                    await cta.click(timeout=8_000)
                    open_timeout = max(250, min(30_000, int(bid_cfg.get("form_open_timeout_ms") or 12_000)))
                    try:
                        await form.wait_for(state="visible", timeout=open_timeout)
                    except Exception:
                        safe_debug["form_open_timeout_ms"] = open_timeout
                        await _dump_debug(page, form, bid_cfg, source_name, project_id, "form-did-not-open")
                        return _failure(
                            "پس از کلیک روی دکمه مخصوص پروژه، پنل ارسال واقعاً باز نشد؛ "
                            "هیچ فیلدی تغییر نکرد و پیشنهادی ارسال نشد.",
                            debug=safe_debug,
                        )
                    await page.wait_for_timeout(500)
                if not await form.is_visible():
                    await _dump_debug(page, form, bid_cfg, source_name, project_id, "form-still-hidden")
                    return _failure(
                        "پنل ارسال هنوز مخفی است؛ برای جلوگیری از تغییر فرم نامرئی، ارسال متوقف شد.",
                        debug=safe_debug,
                    )
                try:
                    await form.scroll_into_view_if_needed(timeout=3_000)
                except Exception:
                    pass

                if not await form.is_visible():
                    await _dump_debug(page, form, bid_cfg, source_name, project_id, "form-hidden-before-fill")
                    return _failure(
                        "فرم پیشنهاد پیش از تکمیل دوباره مخفی شد؛ هیچ فیلدی تغییر نکرد.",
                        debug=safe_debug,
                    )

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

                stage = "fill"
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

                stage = "validate"
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

                submitter, submit_diag = await _find_submitter(
                    page, form, submitter_id, endpoint_attribute, endpoint_url
                )
                if submitter is None:
                    log.warning(
                        "[BID][BROWSER-SUBMIT-NOT-FOUND] source=%s id=%s diag=%s",
                        source_name,
                        project_id,
                        submit_diag,
                    )
                    safe_debug["submit_button"] = submit_diag
                    await _dump_debug(page, form, bid_cfg, source_name, project_id, "submit-not-found")
                    return _failure(
                        "دکمهٔ «ارسال پیشنهاد» بعد از پر شدن فرم دیده نشد "
                        f"({submit_diag}). هیچ پیشنهادی ارسال نشد؛ احتمالاً چیدمان صفحهٔ سایت عوض شده یا "
                        "صفحه کامل بارگذاری نشده. اسکرین‌شات در data/browser_debug ذخیره شد.",
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
                if not action_clicked and page is not None:
                    await _dump_debug(page, form_ref, bid_cfg, source_name, project_id, f"timeout-{stage}")
                raise
    except PlaywrightTimeoutError:
        safe_debug["request_sent"] = "unknown" if action_clicked else False
        safe_debug["stage"] = stage
        retryable = (not action_clicked) and stage in _RETRYABLE_STAGES
        safe_debug["retryable"] = retryable
        if retryable:
            safe_debug["retry_reason"] = f"timeout_before_submit:{stage}"
        log.warning(
            "[BID][BROWSER-SUBMIT] source=%s id=%s outcome=timeout stage=%s clicked=%s will_retry=%s",
            source_name,
            project_id,
            stage,
            action_clicked,
            retryable,
        )
        where = _STAGE_FA.get(stage, stage)
        return _failure(
            f"مرورگر در مرحلهٔ «{where}» زمان کافی نداشت (شبکه کند یا سایت دیر پاسخ می‌دهد)؛ "
            + (
                "پیشنهادی ثبت نشد و یک‌بار با مرورگر تازه دوباره تلاش می‌شود."
                if retryable
                else "کلیک ارسال انجام شده و نتیجه قطعی نیست؛ وضعیت پروژه را از صفحهٔ سایت بررسی کن."
            ),
            pause_source=action_clicked,
            debug=safe_debug,
        )
    except Exception as exc:
        safe_debug["request_sent"] = "unknown" if action_clicked else False
        # A browser/page that died before any click is safe to retry once on a fresh browser.
        safe_debug["retryable"] = (not action_clicked) and type(exc).__name__ == "TargetClosedError"
        if safe_debug["retryable"]:
            safe_debug["retry_reason"] = f"browser_closed_before_submit:{stage}"
        log.warning(
            "[BID][BROWSER-SUBMIT] source=%s id=%s outcome=browser_error stage=%s error_type=%s detail=%r clicked=%s",
            source_name,
            project_id,
            stage,
            type(exc).__name__,
            str(exc).splitlines()[0][:180] if str(exc) else "",
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
        _browser_lock = PriorityLock()
    # paid plans are served first when several users/sites wait for the one browser
    async with _browser_lock.hold(submit_priority.get()):
        result = await _submit_once(**kwargs)
        if (result.payload_debug or {}).get("retryable"):
            log.warning(
                "[BID][BROWSER-RETRY] source=%s id=%s reason=%s",
                kwargs.get("source_name"),
                kwargs.get("project_id"),
                (result.payload_debug or {}).get("retry_reason", "safe_pre_submit_retry"),
            )
            await asyncio.sleep(1.0)
            result = await _submit_once(**kwargs)
        if result.payload_debug:
            result.payload_debug.pop("retryable", None)
        return result
