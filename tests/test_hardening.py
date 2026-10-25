"""Regression tests for the 0.7.0 hardening pass (see docs/CODE_REVIEW.md).

Each test pins one failure mode observed in the production log of
2026-10-25: the JavaScript false-positive that stopped the scheduler, the
25-detail-pages-per-poll request storm, repeated downloads of the same
project page, and Persian relative timestamps never being parsed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import respx

from bidpilot.config import AppConfig, FiltersConfig, Profile, RuntimeConfig, Secrets
from bidpilot.filters import ProjectFilter
from bidpilot.models import Budget, Project
from bidpilot.sources import html_parser as parser
from bidpilot.sources.base import JavaScriptRequiredError
from bidpilot.sources.parscoders.adapter import ParscodersSource

FIXTURES = Path(__file__).parent / "fixtures"
LIST_HTML = (FIXTURES / "sample_list.html").read_text(encoding="utf-8")
PROJECT_HTML = (FIXTURES / "sample_project.html").read_text(encoding="utf-8")
JS_ALERT = (
    '<div class="alert-danger">خطا: جاوا اسکریپت (JavaScript) مرورگر شما غیر فعال است! '
    "برای استفاده از پارس‌کدرز باید آن را فعال کنید.</div>"
)


def make_config() -> AppConfig:
    return AppConfig(
        profile=Profile(first_name="محمد", full_name="محمد رضایی"),
        runtime=RuntimeConfig(submit_mode="live", request_timeout_seconds=5),
        sources=[{"type": "parscoders", "settings": {"list": {"fetch_details": False}}}],
    )


def make_source(**settings) -> ParscodersSource:
    settings.setdefault("http", {"request_delay_seconds": 0})
    return ParscodersSource(settings=settings, config=make_config(), secrets=Secrets(_env_file=None))


# ------------------------------------------------------- JavaScript detection
def test_javascript_skill_tag_plus_unrelated_disabled_word_is_not_a_js_wall():
    filler = "توضیحات طولانی پروژه درباره‌ی نیازمندی‌ها و خروجی‌های مورد انتظار کارفرما " * 4
    html = f"""
    <html><body>
      <ul class="skills"><li>جاوا اسکریپت</li><li>PHP</li></ul>
      <p>{filler}</p>
      <aside>پروفایل این کاربر در حال حاضر فعال نیست</aside>
    </body></html>
    """
    assert not parser.requires_javascript(html)


def test_noscript_and_script_text_do_not_count_as_visible_notice():
    assert not parser.requires_javascript(
        "<html><body><noscript>لطفاً جاوا اسکریپت را فعال کنید</noscript><p>شرح کامل پروژه…</p></body></html>"
    )
    assert not parser.requires_javascript(
        '<html><head><script>var m = "Please enable JavaScript";</script></head>'
        "<body><p>شرح پروژه</p></body></html>"
    )


def test_real_visible_javascript_alert_is_still_detected():
    assert parser.requires_javascript(f"<html><body>{JS_ALERT}</body></html>")
    assert parser.requires_javascript("<p>Please enable JavaScript in your browser.</p>")
    assert "جاوا اسکریپت" in parser.javascript_requirement_snippet(JS_ALERT)


# ------------------------------------------------------- bid-status precedence
@respx.mock
async def test_bid_status_prefers_the_real_form_over_a_js_banner():
    """A usable bid form is functional proof the page works without JS."""
    page = PROJECT_HTML.replace("<body>", f"<body>{JS_ALERT}")
    respx.get("https://parscoders.com/project/12345").mock(return_value=httpx.Response(200, text=page))
    source = make_source()
    project = Project(source="parscoders", external_id="12345", title="تست")

    assert await source.check_bid_status(project) is False
    await source.aclose()


@respx.mock
async def test_bid_status_raises_typed_error_for_a_pure_js_wall():
    respx.get("https://parscoders.com/project/12345").mock(
        return_value=httpx.Response(200, text=f"<html><body>{JS_ALERT}</body></html>")
    )
    source = make_source()
    project = Project(source="parscoders", external_id="12345", title="تست")

    with pytest.raises(JavaScriptRequiredError):
        await source.check_bid_status(project)
    await source.aclose()


# ------------------------------------------------------------- lazy details
@respx.mock
async def test_on_demand_mode_fetches_no_detail_pages_during_listing():
    respx.get("https://parscoders.com/project").mock(return_value=httpx.Response(200, text=LIST_HTML))
    detail_route = respx.get(url__regex=r"https://parscoders\.com/project/\d+").mock(
        return_value=httpx.Response(200, text=PROJECT_HTML)
    )
    source = make_source(list={"detail_mode": "on_demand"})

    projects = await source.fetch_projects()

    assert len(projects) == 3
    assert detail_route.call_count == 0  # the whole point: no request storm
    await source.aclose()


@respx.mock
async def test_enrich_project_and_bid_status_share_one_detail_download():
    detail_route = respx.get("https://parscoders.com/project/12345").mock(
        return_value=httpx.Response(200, text=PROJECT_HTML)
    )
    source = make_source(list={"detail_mode": "on_demand"})
    project = Project(
        source="parscoders",
        external_id="12345",
        title="ساخت ربات تلگرام فروشگاهی با درگاه پرداخت",
        description="کوتاه",
    )

    enriched = await source.enrich_project(project)
    state = await source.check_bid_status(enriched)

    assert "زرین‌پال" in enriched.description
    assert state is False
    assert detail_route.call_count == 1  # second read came from the fresh cache
    await source.aclose()


# ---------------------------------------------------------- Persian timestamps
def test_parse_published_understands_spelled_out_persian_numbers():
    now = datetime.now(timezone.utc)

    four_hours = parser.parse_published("چهار ساعت پیش")
    assert four_hours is not None and four_hours.tzinfo is not None
    assert abs((now - four_hours) - timedelta(hours=4)) < timedelta(minutes=1)

    two_days = parser.parse_published("دو روز پیش")
    assert two_days is not None
    assert abs((now - two_days) - timedelta(days=2)) < timedelta(minutes=1)

    assert parser.parse_published("دیروز") is not None
    recent = parser.parse_published("لحظاتی پیش")
    assert recent is not None and abs(now - recent) < timedelta(minutes=1)

    digits = parser.parse_published("۳ ساعت پیش".translate(str.maketrans("۳", "3")))
    assert digits is not None and abs((now - digits) - timedelta(hours=3)) < timedelta(minutes=1)


# ---------------------------------------------------------------- skill match
def _java_filter() -> ProjectFilter:
    return ProjectFilter(
        FiltersConfig(require_budget=False, require_skill_match=True),
        profile_skills=["Java"],
        profile_role="",
    )


def _project_with_skills(skills: list[str]) -> Project:
    return Project(
        source="mock",
        external_id="1",
        title="پروژه‌ی وب",
        budget=Budget(min=1_000_000, max=2_000_000),
        skills=skills,
    )


def test_java_profile_does_not_match_javascript_projects():
    project_filter = _java_filter()

    assert not project_filter.evaluate(_project_with_skills(["JavaScript"])).allowed
    assert not project_filter.evaluate(_project_with_skills(["جاوا اسکریپت"])).allowed
    assert project_filter.evaluate(_project_with_skills(["Java"])).allowed
    assert project_filter.evaluate(_project_with_skills(["جاوا"])).allowed


def test_short_latin_skill_does_not_match_inside_other_words():
    project_filter = ProjectFilter(
        FiltersConfig(require_budget=False, require_skill_match=True),
        profile_skills=["Hive"],
        profile_role="",
    )
    archived = Project(
        source="mock",
        external_id="1",
        title="Archive migration",
        description="move the archive to cold storage",
        budget=Budget(min=1_000_000, max=2_000_000),
    )
    assert not project_filter.evaluate(archived).allowed


# ---------------------------------------------- pipeline enriches lazily only
async def test_pipeline_enriches_only_projects_that_pass_the_filter(tmp_path):
    from bidpilot.app import Application
    from bidpilot.config import SourceConfig

    projects = [
        {
            "external_id": "1",
            "title": "ساخت ربات تلگرام فروشگاهی",
            "description": "ربات تلگرام با سبد خرید و درگاه پرداخت",
            "budget": {"min": 500_000, "max": 1_500_000},
            "skills": ["Python"],
        },
        {
            "external_id": "2",
            "title": "هک اکانت اینستاگرام",
            "description": "بازیابی اکانت",
            "budget": {"min": 100_000, "max": 200_000},
            "skills": [],
        },
        {
            "external_id": "3",
            "title": "توسعه API با FastAPI",
            "description": "بک‌اند اپلیکیشن موبایل",
            "budget": {"min": 300_000, "max": 700_000},
            "skills": ["FastAPI"],
        },
    ]
    config = AppConfig(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.sqlite3'}",
        profile=Profile(first_name="محمد", full_name="محمد رضایی"),
        runtime=RuntimeConfig(
            submit_mode="dry_run",
            max_bids_per_hour=50,
            max_bids_per_day=100,
            min_seconds_between_bids=0,
            notify_on_skip=False,
        ),
        filters=FiltersConfig(
            exclude_keywords=["هک"],
            require_budget=False,
            require_skill_match=False,
        ),
        sources=[SourceConfig(type="mock", settings={"projects": projects, "latency": 0})],
        llm={"enabled": False},
    )
    application = Application.build(config, Secrets(_env_file=None))
    await application.setup()

    source = application.sources["mock"]
    enriched_ids: list[str] = []

    async def counting_enrich(project: Project) -> Project:
        enriched_ids.append(project.external_id)
        return project.model_copy(update={"description": project.description + " (کامل)"})

    source.enrich_project = counting_enrich  # type: ignore[method-assign]

    report = (await application.cycle())[0]

    assert report.fetched == 3
    # the blacklisted project ("2") never earned a detail-page request
    assert enriched_ids == ["1", "3"]
    await application.close()
