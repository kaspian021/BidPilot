"""Integration tests for the Parscoders adapter using mocked HTTP (respx).

These run without touching the real site: they verify the whole request/response
contract (listing -> detail enrichment -> form submit -> success detection).
"""

from pathlib import Path

import httpx
import respx

from bidpilot.config import AppConfig, Profile, RuntimeConfig, Secrets
from bidpilot.models import Budget, Project, Proposal
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
    source = make_source(list={"fetch_details": True, "detail_concurrency": 2})

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
async def test_submit_bid_without_credentials_fails_fast():
    source = make_source()
    project = Project(source="parscoders", external_id="1", title="تست")
    result = await source.submit_bid(project, Proposal(text="سلام"), 1_000_000, 14)
    assert result.status.value == "failed"
    assert "PARSOCODERS_COOKIE" in result.message
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
    assert "فرم ارسال پیشنهاد پیدا نشد" in result.message or "نشست" in result.message
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
