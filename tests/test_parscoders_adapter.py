"""Integration tests for the Parscoders adapter using mocked HTTP (respx).

These run without touching the real site: they verify the whole request/response
contract (listing -> detail enrichment -> form submit -> success detection).
"""

from pathlib import Path

import httpx
import pytest
import respx

from bidpilot.config import AppConfig, Profile, RuntimeConfig, Secrets
from bidpilot.credentials import SiteCredentials
<<<<<<< HEAD
from bidpilot.models import Budget, Project, Proposal
=======
from bidpilot.models import Budget, Project, Proposal, SubmissionResult, SubmissionStatus
>>>>>>> a26d343 (Fall back to installed Edge or Chrome when Playwright Chromium is unavailable)
from bidpilot.sources.base import SourceError
from bidpilot.sources.parscoders.adapter import ParscodersSource

FIXTURES = Path(__file__).parent / "fixtures"
LIST_HTML = (FIXTURES / "sample_list.html").read_text(encoding="utf-8")
PROJECT_HTML = (FIXTURES / "sample_project.html").read_text(encoding="utf-8")


def make_config() -> AppConfig:
    return AppConfig(
        profile=Profile(first_name="محمد", full_name="محمد رضایی"),
        runtime=RuntimeConfig(submit_mode="live", request_timeout_seconds=5),
        sources=[{"type": "parscoders", "settings": {"list": {"fetch_details": False}}}],
    )


def make_source(**settings) -> ParscodersSource:
    return ParscodersSource(settings=settings, config=make_config(), secrets=Secrets(_env_file=None))


@respx.mock
async def test_fetch_projects_from_listing():
    respx.get("https://parscoders.com/project").mock(return_value=httpx.Response(200, text=LIST_HTML))
    source = make_source()

    projects = await source.fetch_projects()
    assert len(projects) == 3
    assert projects[0].source == "parscoders"
    assert {p.external_id for p in projects} == {"12345", "12346", "12347"}
    await source.aclose()


@respx.mock
async def test_fetch_enriches_from_detail_page():
    respx.get("https://parscoders.com/project").mock(return_value=httpx.Response(200, text=LIST_HTML))
    detail_route = respx.get(url__regex=r"https://parscoders\.com/project/\d+").mock(
        return_value=httpx.Response(200, text=PROJECT_HTML)
    )
    source = make_source(list={"detail_mode": "eager", "detail_concurrency": 2})

    projects = await source.fetch_projects()
    assert detail_route.call_count == 3
    first = next(p for p in projects if p.external_id == "12345")
    assert "زرین‌پال" in first.description
    assert first.employer == "کارفرمای نمونه"
    await source.aclose()


@respx.mock
async def test_submit_bid_posts_mapped_form_fields():
    respx.get("https://parscoders.com/project/12345").mock(
        return_value=httpx.Response(200, text=PROJECT_HTML)
    )
    post_route = respx.post("https://parscoders.com/project/12345/bid").mock(
        return_value=httpx.Response(200, text="<div>پیشنهاد شما با موفقیت ثبت شد.</div>")
    )

    source = make_source()
    source._session_cookies = {"PHPSESSID": "session-value"}

    project = Project(
        source="parscoders",
        external_id="12345",
        title="ساخت ربات تلگرام فروشگاهی",
        budget=Budget(min=500_000, max=1_500_000),
    )
    proposal = Proposal(text="سلام و وقت بخیر، متن پیشنهاد آزمایشی.")

    result = await source.submit_bid(project, proposal, 1_000_000, 14)

    assert result.status.value == "submitted"
    request = post_route.calls[0].request
    body = request.content.decode()
    assert "amount=1000000" in body
    assert "delivery_days=14" in body
    assert "_csrf=csrf-token-abc123" in body
    assert "PHPSESSID=session-value" in request.headers.get("cookie", "")
    await source.aclose()


@respx.mock
async def test_submit_bid_aborts_js_ajax_form_without_posting_to_project_url():
    # Real ParsCoders pages use a JS-managed form: the <form> has no action,
    # while its submit button carries the AJAX endpoint in data-href.
    page = """
    <html><head><title>پروژه آزمایشی</title></head><body>
      <form id="init_message_form" name="init_message">
        <input type="hidden" name="_token" value="csrf-placeholder">
        <input type="text" name="bidAmount">
        <input type="text" name="deadline">
        <input type="text" name="initEscrowPercent">
        <input type="text" name="expertGuaranteePercent">
        <input type="text" name="validDays">
        <textarea name="message"></textarea>
        <button type="submit" id="msg_submit_btn"
                data-href="/conversation/ajax/bid-send/12345">ارسال پیشنهاد</button>
      </form>
    </body></html>
    """
<<<<<<< HEAD
    respx.get("https://parscoders.com/project/12345").mock(
        return_value=httpx.Response(200, text=page)
    )
    wrong_route = respx.post("https://parscoders.com/project/12345").mock(
        return_value=httpx.Response(200, text="project page returned")
    )
    source = make_source()
=======
    respx.get("https://parscoders.com/project/12345").mock(return_value=httpx.Response(200, text=page))
    wrong_route = respx.post("https://parscoders.com/project/12345").mock(
        return_value=httpx.Response(200, text="project page returned")
    )
    source = make_source(bid={"browser_submit": False})
>>>>>>> a26d343 (Fall back to installed Edge or Chrome when Playwright Chromium is unavailable)
    source._session_cookies = {"PHPSESSID": "session-value"}
    project = Project(source="parscoders", external_id="12345", title="تست")

    result = await source.submit_bid(project, Proposal(text="متن پیشنهاد آزمایشی"), 500_000, 14)

    assert result.status.value == "failed"
    assert result.javascript_required
    assert result.payload_debug["request_sent"] is False
    assert result.payload_debug["endpoint_path"] == "/conversation/ajax/bid-send/12345"
    assert wrong_route.call_count == 0
    await source.aclose()


<<<<<<< HEAD
=======
@pytest.mark.asyncio
@respx.mock
async def test_submit_bid_delegates_js_managed_form_to_browser(monkeypatch):
    page = """
    <html><head><title>Project</title></head><body>
      <form id="init_message_form" name="init_message">
        <input type="hidden" name="_token" value="csrf-placeholder">
        <input type="text" name="bidAmount">
        <input type="text" name="deadline" value="2">
        <input type="text" name="initEscrowPercent" value="100">
        <input type="text" name="expertGuaranteePercent" value="0">
        <input type="text" name="validDays">
        <textarea name="message"></textarea>
        <button type="submit" id="msg_submit_btn"
                data-href="/conversation/ajax/bid-send/12345">ارسال پیشنهاد</button>
      </form>
    </body></html>
    """
    respx.get("https://parscoders.com/project/12345").mock(return_value=httpx.Response(200, text=page))
    wrong_route = respx.post("https://parscoders.com/project/12345").mock(
        return_value=httpx.Response(200, text="project page returned")
    )
    calls = []

    async def fake_browser_submit(**kwargs):
        calls.append(kwargs)
        return SubmissionResult(
            status=SubmissionStatus.SUBMITTED,
            message="ثبت آزمایشی مرورگر تأیید شد",
            http_status=200,
            payload_debug={"browser": "playwright", "request_sent": True},
        )

    monkeypatch.setattr(
        "bidpilot.sources.html_market.submit_js_managed_form",
        fake_browser_submit,
    )
    source = make_source(bid={"browser_submit": True})
    source._session_cookies = {"PHPSESSID": "session-value"}
    project = Project(source="parscoders", external_id="12345", title="تست")

    result = await source.submit_bid(project, Proposal(text="متن پیشنهاد آزمایشی"), 500_000, 14)

    assert result.status is SubmissionStatus.SUBMITTED
    assert len(calls) == 1
    assert calls[0]["project_id"] == "12345"
    assert calls[0]["endpoint_attribute"] == "data-href"
    assert calls[0]["field_mapping"] == {
        "amount": "bidAmount",
        "days": "deadline",
        "description": "message",
    }
    assert calls[0]["proposal_text"] == "متن پیشنهاد آزمایشی"
    assert wrong_route.call_count == 0
    await source.aclose()


>>>>>>> a26d343 (Fall back to installed Edge or Chrome when Playwright Chromium is unavailable)
@respx.mock
async def test_submit_bid_detects_rejection():
    respx.get("https://parscoders.com/project/12345").mock(
        return_value=httpx.Response(200, text=PROJECT_HTML)
    )
    respx.post("https://parscoders.com/project/12345/bid").mock(
        return_value=httpx.Response(200, text="<div>شما قبلاً پیشنهاد داده‌اید.</div>")
    )
    source = make_source()
    source._session_cookies = {"PHPSESSID": "x"}

    project = Project(source="parscoders", external_id="12345", title="تست")
    result = await source.submit_bid(project, Proposal(text="سلام"), 1_000_000, 14)

    assert result.status.value == "failed"
    assert "قبلاً" in result.message
    await source.aclose()


@respx.mock
async def test_submit_bid_detects_javascript_requirement_and_marks_it():
    respx.get("https://parscoders.com/project/12345").mock(
        return_value=httpx.Response(200, text=PROJECT_HTML)
    )
    post_route = respx.post("https://parscoders.com/project/12345/bid").mock(
        return_value=httpx.Response(
            200,
            text=(
                '<div class="alert-danger">خطا: جاوا اسکریپت (JavaScript) مرورگر شما غیر فعال است! '
                "برای استفاده از پارس‌کدرز باید آن را فعال کنید.</div>"
            ),
        )
    )
    source = make_source()
    source._session_cookies = {"PHPSESSID": "session-value"}
    project = Project(source="parscoders", external_id="12345", title="تست")

    result = await source.submit_bid(project, Proposal(text="سلام"), 1_000_000, 14)

    assert result.status.value == "failed"
    assert result.javascript_required
    assert "JavaScript" in result.message
    assert "ثبت پیشنهاد تأیید نشده" in result.message
    assert post_route.call_count == 1
    await source.aclose()


@respx.mock
async def test_submit_bid_without_credentials_fails_fast():
    source = make_source()
<<<<<<< HEAD
=======
    # Isolate this test from credentials saved in the developer's local settings store.
    source._session_cookies = {}
    source.credentials = SiteCredentials(site="parscoders", method="cookie")
>>>>>>> a26d343 (Fall back to installed Edge or Chrome when Playwright Chromium is unavailable)
    project = Project(source="parscoders", external_id="1", title="تست")
    result = await source.submit_bid(project, Proposal(text="سلام"), 1_000_000, 14)
    assert result.status.value == "failed"
    assert "اعتبارنامه" in result.message
    await source.aclose()


@respx.mock
async def test_submit_bid_handles_expired_session():
    respx.get("https://parscoders.com/project/12345").mock(
        return_value=httpx.Response(200, text="<html><body>لطفاً وارد شوید</body></html>")
    )
    source = make_source()
    source._session_cookies = {"PHPSESSID": "expired"}

    project = Project(source="parscoders", external_id="12345", title="تست")
    result = await source.submit_bid(project, Proposal(text="سلام"), 1_000_000, 14)

    assert result.status.value == "failed"
    assert "صفحهٔ پروژه دریافت شد" in result.message or "نشست" in result.message
    await source.aclose()


@respx.mock
async def test_healthcheck_reports_when_selectors_find_nothing():
    respx.get("https://parscoders.com/project").mock(
        return_value=httpx.Response(200, text="<html><body>هیچ پروژه‌ای نیست</body></html>")
    )
    source = make_source()
    ok, message = await source.healthcheck()
    assert ok is False and "سلکتور" in message
    await source.aclose()


@respx.mock
async def test_submit_bid_detects_proposal_action_gated_by_login():
    page = """
    <html><head><title>Project</title></head><body>
      <a href="/login">ارسال پیشنهاد</a>
      <form id="search-form">
        <input type="text" name="q">
        <button type="submit">جستجو</button>
      </form>
    </body></html>
    """
    respx.get("https://parscoders.com/project/12345").mock(return_value=httpx.Response(200, text=page))
    post_route = respx.post("https://parscoders.com/project/12345").mock(
        return_value=httpx.Response(200, text="unexpected")
    )
    source = make_source()
    source._session_cookies = {"PHPSESSID": "session-value"}

    project = Project(source="parscoders", external_id="12345", title="تست")
    result = await source.submit_bid(project, Proposal(text="سلام"), 500_000, 14)

    assert result.status.value == "failed"
    assert "مسیر ورود" in result.message
    assert post_route.call_count == 0
    await source.aclose()


@respx.mock
async def test_verify_session_does_not_treat_generic_account_text_as_login_proof():
    login_page = """
    <html><body>
      <a>حساب کاربری</a>
      <a>ورود به حساب</a>
      <div>فراموشی رمز</div>
    </body></html>
    """
    respx.get("https://parscoders.com/dashboard").mock(return_value=httpx.Response(200, text=login_page))
    source = make_source()
    source.credentials = SiteCredentials(site="parscoders", method="cookie", cookie="PHPSESSID=session-value")

    ok, message = await source.verify_session()

    assert ok is False
    assert "ورود" in message or "کوکی" in message
    await source.aclose()


@respx.mock
async def test_get_html_retries_transient_server_disconnect():
    calls = {"count": 0}

    def flaky_response(request):
        calls["count"] += 1
        if calls["count"] == 1:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response")
        return httpx.Response(200, text="<html>ok</html>")

    route = respx.get("https://parscoders.com/project").mock(side_effect=flaky_response)
    source = make_source()

    html = await source.get_html("/project")

    assert html == "<html>ok</html>"
    assert route.call_count == 2
    await source.aclose()


@respx.mock
async def test_get_html_reports_exception_type_when_transport_error_message_is_empty():
    def always_disconnect(request):
        raise httpx.RemoteProtocolError("")

    route = respx.get("https://parscoders.com/project").mock(side_effect=always_disconnect)
    source = make_source()

    with pytest.raises(SourceError) as raised:
        await source.get_html("/project")

    assert route.call_count == 3
    assert "RemoteProtocolError" in str(raised.value)
    await source.aclose()


@respx.mock
async def test_detail_html_is_cached_between_poll_cycles():
    list_route = respx.get("https://parscoders.com/project").mock(
        return_value=httpx.Response(200, text=LIST_HTML)
    )
    detail_route = respx.get(url__regex=r"https://parscoders[.]com/project/[0-9]+").mock(
        return_value=httpx.Response(200, text=PROJECT_HTML)
    )
    source = make_source(list={"detail_mode": "eager", "detail_concurrency": 2})

    first = await source.fetch_projects()
    second = await source.fetch_projects()

    assert len(first) == 3 and len(second) == 3
    assert list_route.call_count == 2
    assert detail_route.call_count == 3
    await source.aclose()


@respx.mock
async def test_submit_redirect_without_success_confirmation_is_not_counted_as_submitted():
    respx.get("https://parscoders.com/project/12345").mock(
        return_value=httpx.Response(200, text=PROJECT_HTML)
    )
    respx.post("https://parscoders.com/project/12345/bid").mock(
        return_value=httpx.Response(302, headers={"Location": "/project/12345"})
    )
    source = make_source()
    source._session_cookies = {"PHPSESSID": "session-value"}
    project = Project(source="parscoders", external_id="12345", title="تست")

    result = await source.submit_bid(project, Proposal(text="سلام و وقت بخیر"), 1_000_000, 14)

    assert result.status.value == "failed"
    assert "نامشخص" in result.message
    await source.aclose()
