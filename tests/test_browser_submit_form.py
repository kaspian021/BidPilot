"""End-to-end test of the browser workflow against a local replica of the ParsCoders bid off-canvas.

The replica mirrors what the screenshots show: the form is hidden until the page's
"ارسال پیشنهاد" button is clicked, the message is a rich contenteditable editor mirrored
into a hidden <textarea>, the amount input has a thousand-separator mask, validDays is
empty and keeps the submit button disabled until filled.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from bidpilot.models import SubmissionStatus
from bidpilot.sources.browser_submit import submit_js_managed_form

PAGE = """<!doctype html><html lang="fa" dir="rtl"><body>
<div id="bid_status_custom"></div>
<button class="bid-send--button" data-project-id="777" id="open"
  onclick="document.getElementById('panel').style.display='block';
           setTimeout(()=>document.getElementById('panel').style.transform='none',20)">ارسال پیشنهاد</button>
<div id="panel" style="display:none;position:fixed;top:0;right:0;width:520px;height:100%;
     transform:translateX(100%);transition:transform .4s;background:#222;color:#fff;overflow:auto">
<form id="init_message_form" name="init_message">
  <input type="hidden" name="_token" value="SECRET">
  <input name="bidAmount" inputmode="numeric" required>
  <input name="deadline" value="5" required>
  <input name="initEscrowPercent" value="100" readonly>
  <input name="expertGuaranteePercent" value="25" required>
  <input name="validDays" required>
  <div class="tools"><b>B</b></div>
  <div id="editor" contenteditable="true" style="min-height:120px;border:1px solid #888"></div>
  <textarea name="message" id="message" style="display:none" required></textarea>
  <button type="button" id="msg_submit_btn" disabled data-href="/conversation/ajax/bid-send/777">ارسال</button>
</form></div>
<script>
const f = document.getElementById('init_message_form');
const amount = f.bidAmount;
amount.addEventListener('input', () => {
  const d = amount.value.replace(/\\D/g, '');
  amount.value = d.replace(/\\B(?=(\\d{3})+(?!\\d))/g, ',');
});
document.getElementById('editor').addEventListener('input', e => { f.message.value = e.target.innerText; refresh(); });
f.addEventListener('input', refresh); f.addEventListener('change', refresh);
function refresh(){ document.getElementById('msg_submit_btn').disabled = !f.checkValidity(); }
const mode = new URLSearchParams(location.search).get('mode') || 'plain';
let ifr = null;
if (mode === 'iframe') {
  // TinyMCE-like: the real editor lives in an iframe and is copied to the textarea on submit only.
  document.getElementById('editor').style.display = 'none';
  f.message.required = false;
  ifr = document.createElement('iframe');
  ifr.className = 'tox-edit-area__iframe';
  ifr.style.cssText = 'width:480px;height:140px';
  ifr.srcdoc = '<body contenteditable="true" style="min-height:100px"></body>';
  f.insertBefore(ifr, f.message);
}
if (mode === 'tinymce') {
  document.getElementById('editor').style.display = 'none';
  f.message.required = false;
  const store = {html: ''};
  const ed = {
    focus() {}, fire() {}, targetElm: f.message,
    setContent(h) { store.html = h; },
    getContent() { return store.html.replace(/<[^>]+>/g, ' '); },
    save() { f.message.value = store.html.replace(/<[^>]+>/g, ' ').trim(); },
  };
  window.tinymce = {get: id => (id === 'message' ? ed : null), editors: [ed]};
}
function showNotice(cls, text) {
  const d = document.createElement('div'); d.className = cls; d.textContent = text; document.body.appendChild(d);
}
document.getElementById('msg_submit_btn').addEventListener('click', async e => {
  if (mode === 'iframe') f.message.value = ifr.contentDocument.body.innerText;
  if (mode === 'tinymce') window.tinymce.editors[0].save();
  if (mode === 'block') { showNotice('toast', 'مدت انجام کار از حداقل مجاز کمتر است'); return; }
  if (mode === 'block-sub') {
    showNotice('swal2-popup', 'اشتراک شما به پایان رسیده است؛ برای ارسال پیشنهاد اشتراک تهیه کنید.');
    return;
  }
  if (!f.message.value.trim()) { showNotice('invalid-feedback', 'متن پیشنهاد الزامی است'); return; }
  const body = new FormData(f); body.set('bidAmount', amount.value.replace(/,/g, ''));
  const r = await fetch(e.target.dataset.href, {method: 'POST', body});
  await r.text();
  document.getElementById('bid_status_custom').textContent = 'پیشنهاد ثبت شد';
});
</script></body></html>"""

RECEIVED: list[dict] = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass

    def do_GET(self):
        body = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8", "replace")
        RECEIVED.append({"path": self.path, "raw": raw})
        body = json.dumps({"message": "پیشنهاد شما با موفقیت ثبت شد"}, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def site():
    RECEIVED.clear()
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def _args(base: str, tmp_path, **overrides):
    values = {
        "source_name": "testsite",
        "base_url": base,
        "page_url": f"{base}/project/777",
        "project_id": "777",
        "user_agent": "",
        "cookies": {},
        "form_id": "init_message_form",
        "form_name": "init_message",
        "submitter_id": "msg_submit_btn",
        "endpoint_attribute": "data-href",
        "endpoint_url": f"{base}/conversation/ajax/bid-send/777",
        "field_mapping": {"amount": "bidAmount", "days": "deadline", "description": "message"},
        "proposal_text": "سلام\nاین یک پیشنهاد آزمایشی است.\nخط سوم",
        "amount": 500_000,
        "delivery_days": 14,
        "bid_cfg": {"valid_days": 7, "browser_debug_dir": str(tmp_path / "dbg")},
        "auth_cfg": {},
    }
    values.update(overrides)
    return values


def _require_chromium():
    pytest.importorskip("playwright")


@pytest.mark.asyncio
async def test_opens_offcanvas_fills_like_a_human_and_submits(site, tmp_path):
    _require_chromium()
    result = await submit_js_managed_form(**_args(site, tmp_path))
    if "Playwright" in result.message and (
        "نصب نیست" in result.message or "دسترس نیست" in result.message or "در دسترس نیست" in result.message
    ):
        pytest.skip("playwright browser unavailable")
    assert result.status is SubmissionStatus.SUBMITTED, result.message
    assert len(RECEIVED) == 1
    raw = RECEIVED[0]["raw"]
    assert "500000" in raw and "14" in raw and "SECRET" in raw
    assert "اين" in raw or "این" in raw
    assert "7" in raw  # validDays auto-filled


@pytest.mark.asyncio
async def test_field_failure_never_clicks_and_saves_debug(site, tmp_path):
    _require_chromium()
    result = await submit_js_managed_form(
        **_args(
            site,
            tmp_path,
            field_mapping={"amount": "bidAmount", "days": "initEscrowPercent", "description": "message"},
        )
    )
    if "نصب نیست" in result.message or "دسترس نیست" in result.message or "در دسترس نیست" in result.message:
        pytest.skip("playwright browser unavailable")
    assert result.status is SubmissionStatus.FAILED
    assert RECEIVED == []
    assert result.payload_debug["request_sent"] is False
    assert list((tmp_path / "dbg").glob("*.png"))
    html = next((tmp_path / "dbg").glob("*.html")).read_text(encoding="utf-8")
    assert "SECRET" not in html


@pytest.mark.asyncio
async def test_editor_inside_iframe_is_typed_into_not_just_the_hidden_textarea(site, tmp_path):
    """TinyMCE copies its iframe content over the textarea on submit; filling only the textarea sends nothing."""
    _require_chromium()
    result = await submit_js_managed_form(**_args(site, tmp_path, page_url=f"{site}/project/777?mode=iframe"))
    if "نصب نیست" in result.message:
        pytest.skip("playwright browser unavailable")
    assert result.status is SubmissionStatus.SUBMITTED, result.message
    assert len(RECEIVED) == 1
    assert "پیشنهاد آزمایشی" in RECEIVED[0]["raw"] or "%D9%BE" in RECEIVED[0]["raw"]


@pytest.mark.asyncio
async def test_editor_public_api_is_used_when_present(site, tmp_path):
    _require_chromium()
    result = await submit_js_managed_form(
        **_args(site, tmp_path, page_url=f"{site}/project/777?mode=tinymce")
    )
    if "نصب نیست" in result.message:
        pytest.skip("playwright browser unavailable")
    assert result.status is SubmissionStatus.SUBMITTED, result.message
    assert len(RECEIVED) == 1


@pytest.mark.asyncio
async def test_click_without_request_reports_the_sites_own_message(site, tmp_path):
    _require_chromium()
    result = await submit_js_managed_form(**_args(site, tmp_path, page_url=f"{site}/project/777?mode=block"))
    if "نصب نیست" in result.message:
        pytest.skip("playwright browser unavailable")
    assert result.status is SubmissionStatus.FAILED
    assert RECEIVED == []
    assert result.payload_debug["request_sent"] is False
    assert "حداقل مجاز" in result.message
    assert not result.subscription_required
    assert list((tmp_path / "dbg").glob("*click-no-request.png"))


@pytest.mark.asyncio
async def test_subscription_notice_after_click_stops_the_source(site, tmp_path):
    _require_chromium()
    result = await submit_js_managed_form(
        **_args(site, tmp_path, page_url=f"{site}/project/777?mode=block-sub")
    )
    if "نصب نیست" in result.message:
        pytest.skip("playwright browser unavailable")
    assert result.status is SubmissionStatus.FAILED
    assert result.subscription_required is True
    assert RECEIVED == []
