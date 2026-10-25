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


# =====================================================================
# 0.8.0 — the wrong-form incident (production, project 621237): the
# proposal was POSTed to /project-report/<id>/new because the old
# heuristic took "the first form with a textarea", the POST bounced to
# /login with a JavaScript alert, and the pipeline hard-stopped the
# whole scheduler as "session expired". Each test pins one link of
# that chain so it can never reassemble.
# =====================================================================

_BID_CFG = {
    "fields": {
        "amount": ["input[name='amount']"],
        "description": ["textarea"],
        "days": ["input[name='days']"],
    }
}

_REPORT_FORM_HTML = """
<form action="/project-report/621237/new" method="post">
  <textarea name="report-textarea-report"></textarea>
  <input type="hidden" name="_csrf" value="tok">
  <button type="submit">ثبت گزارش</button>
</form>"""

_REAL_BID_FORM_HTML = """
<form id="frm-proposal" action="/project/621237/bid" method="post">
  <label>مبلغ پیشنهادی</label><input name="amount" type="number">
  <label>مدت انجام</label><input name="days" type="number">
  <textarea name="description"></textarea>
  <button type="submit">ثبت پیشنهاد</button>
</form>"""


def test_bid_form_scoring_prefers_real_bid_form_over_earlier_report_form():
    html = f"<html><body>{_REPORT_FORM_HTML}{_REAL_BID_FORM_HTML}</body></html>"
    form, _payload = parser.find_bid_form(html, _BID_CFG)
    assert form is not None
    assert form.get("id") == "frm-proposal"
    assert "report" not in (form.get("action") or "")


def test_report_form_alone_is_never_a_bid_form():
    html = f"<html><body>{_REPORT_FORM_HTML}</body></html>"
    form, _payload = parser.find_bid_form(html, _BID_CFG)
    assert form is None
    # …even when a stale configured selector matches it directly
    form, _payload = parser.find_bid_form(html, {**_BID_CFG, "form_selectors": ["form[action*='report']"]})
    assert form is None


def test_login_form_with_password_is_disqualified_even_with_textarea_and_price():
    html = """
    <html><body>
      <form action="/login" method="post">
        <input name="amount" type="number">
        <textarea name="description"></textarea>
        <input type="password" name="password">
      </form>
    </body></html>"""
    form, _payload = parser.find_bid_form(html, _BID_CFG)
    assert form is None


def test_textarea_only_form_is_not_trusted_without_an_amount():
    html = """
    <html><body>
      <form action="/project/621237" method="post">
        <textarea name="description"></textarea>
        <button type="submit">ارسال</button>
      </form>
    </body></html>"""
    form, _payload = parser.find_bid_form(html, _BID_CFG)
    assert form is None


def test_proposal_cta_url_is_found_but_login_and_hash_ctas_are_ignored():
    page_url = "https://example.com/project/621237"
    html = '<a href="/project/621237/proposal-form" class="btn btn-primary">ارسال پیشنهاد</a>'
    assert parser.find_proposal_cta_url(html, page_url) == "https://example.com/project/621237/proposal-form"

    login_only = '<a href="/login">ارسال پیشنهاد</a><a href="#">ثبت پیشنهاد</a>'
    assert parser.find_proposal_cta_url(login_only, page_url) is None


def test_js_cookie_assignments_extracts_only_static_values():
    html = """
    <script>
      document.cookie = "human_check=ok; path=/";
      Cookies.set('js_verified', '1');
      document.cookie = "token=" + location.hash;
      document.cookie = "ts=" + Date.now();
    </script>"""
    assert parser.extract_js_cookie_assignments(html) == {
        "human_check": "ok",
        "js_verified": "1",
    }


def test_generic_amount_and_days_field_name_fallbacks():
    from bs4 import BeautifulSoup

    form = BeautifulSoup(
        """
        <form action="/bid" method="post">
          <input name="my_price" type="text">
          <input name="delivery_time" type="text">
          <textarea name="proposal_text"></textarea>
        </form>""",
        "lxml",
    ).find("form")
    mapping = parser.map_bid_fields(form, {})
    assert mapping == {
        "amount": "my_price",
        "days": "delivery_time",
        "description": "proposal_text",
    }


# ------------------------------------------------------- submit-level pinning
@respx.mock
async def test_submit_never_posts_to_the_report_form():
    from bidpilot.models import Proposal

    respx.get("https://parscoders.com/project/12345").mock(
        return_value=httpx.Response(200, text=f"<html><body>{_REPORT_FORM_HTML}</body></html>")
    )
    post_route = respx.post(url__regex=r"https://parscoders\.com/.*").mock(
        return_value=httpx.Response(200, text="unexpected")
    )
    source = make_source()
    source._session_cookies = {"PHPSESSID": "x"}
    project = Project(source="parscoders", external_id="12345", title="تست")

    result = await source.submit_bid(project, Proposal(text="سلام"), 1_000_000, 14)

    assert result.status.value == "failed"
    assert "شناسایی نشد" in result.message
    assert post_route.call_count == 0
    await source.aclose()


@respx.mock
async def test_submit_follows_proposal_cta_and_finds_form_on_the_next_page():
    from bidpilot.models import Proposal

    landing = """
    <html><body>
      <a href="/project/12345/proposal-form" class="btn btn-primary">ارسال پیشنهاد</a>
    </body></html>"""
    respx.get("https://parscoders.com/project/12345").mock(return_value=httpx.Response(200, text=landing))
    respx.get("https://parscoders.com/project/12345/proposal-form").mock(
        return_value=httpx.Response(200, text=PROJECT_HTML)
    )
    post_route = respx.post("https://parscoders.com/project/12345/bid").mock(
        return_value=httpx.Response(200, text="<div>پیشنهاد شما با موفقیت ثبت شد.</div>")
    )
    source = make_source()
    source._session_cookies = {"PHPSESSID": "x"}
    project = Project(source="parscoders", external_id="12345", title="تست")

    result = await source.submit_bid(project, Proposal(text="سلام"), 1_000_000, 14)

    assert result.status.value == "submitted"
    assert post_route.call_count == 1
    await source.aclose()


@respx.mock
async def test_submit_replays_static_js_cookies_from_the_project_page():
    from bidpilot.models import Proposal

    page = PROJECT_HTML.replace(
        "<body>", '<body><script>document.cookie = "human_check=ok; path=/";</script>'
    )
    respx.get("https://parscoders.com/project/12345").mock(return_value=httpx.Response(200, text=page))
    post_route = respx.post("https://parscoders.com/project/12345/bid").mock(
        return_value=httpx.Response(200, text="<div>پیشنهاد شما با موفقیت ثبت شد.</div>")
    )
    source = make_source()
    source._session_cookies = {"PHPSESSID": "x"}
    project = Project(source="parscoders", external_id="12345", title="تست")

    result = await source.submit_bid(project, Proposal(text="سلام"), 1_000_000, 14)

    assert result.status.value == "submitted"
    cookie_header = post_route.calls[0].request.headers.get("cookie", "")
    assert "human_check=ok" in cookie_header
    await source.aclose()


@respx.mock
async def test_post_redirect_to_login_with_js_alert_is_javascript_not_expired_session():
    """The exact response shape from the incident log."""
    from bidpilot.models import Proposal

    respx.get("https://parscoders.com/project/12345").mock(
        return_value=httpx.Response(200, text=PROJECT_HTML)
    )
    respx.post("https://parscoders.com/project/12345/bid").mock(
        return_value=httpx.Response(302, headers={"Location": "/login"})
    )
    respx.get("https://parscoders.com/login").mock(
        return_value=httpx.Response(200, text=f"<html><body>{JS_ALERT}</body></html>")
    )
    source = make_source()
    source._session_cookies = {"PHPSESSID": "x"}
    project = Project(source="parscoders", external_id="12345", title="تست")

    result = await source.submit_bid(project, Proposal(text="سلام"), 1_000_000, 14)

    assert result.status.value == "failed"
    assert result.javascript_required
    assert "نشست منقضی" not in result.message
    await source.aclose()


# ------------------------------------------ auth failures are confirmed first
async def _run_live_cycle(tmp_path, *, session_alive: bool):
    from bidpilot.app import Application
    from bidpilot.config import SourceConfig
    from bidpilot.models import Proposal, SubmissionResult, SubmissionStatus

    config = AppConfig(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.sqlite3'}",
        profile=Profile(first_name="محمد", full_name="محمد رضایی"),
        runtime=RuntimeConfig(
            submit_mode="live",
            max_bids_per_hour=50,
            max_bids_per_day=100,
            min_seconds_between_bids=0,
            notify_on_skip=False,
        ),
        filters=FiltersConfig(require_budget=False, require_skill_match=False),
        sources=[
            SourceConfig(
                type="mock",
                settings={
                    "projects": [
                        {
                            "external_id": "1",
                            "title": "ساخت ربات تلگرام فروشگاهی",
                            "description": "ربات تلگرام با سبد خرید و درگاه پرداخت",
                            "budget": {"min": 500_000, "max": 1_500_000},
                            "skills": ["Python"],
                        }
                    ],
                    "latency": 0,
                },
            )
        ],
        llm={"enabled": False},
    )
    application = Application.build(config, Secrets(_env_file=None))
    await application.setup()
    source = application.sources["mock"]

    async def not_bidded(project: Project) -> bool:
        return False

    async def bounced_to_login(
        project: Project, proposal: Proposal, amount: int, delivery_days: int | None
    ) -> SubmissionResult:
        return SubmissionResult(
            status=SubmissionStatus.FAILED,
            message="لینک «ارسال پیشنهاد» به مسیر ورود هدایت می‌شود؛ نشست منقضی شده است.",
        )

    async def verify() -> tuple[bool, str]:
        return session_alive, "verified" if session_alive else "no session"

    source.check_bid_status = not_bidded  # type: ignore[method-assign]
    source.submit_bid = bounced_to_login  # type: ignore[method-assign]
    source.verify_session = verify  # type: ignore[method-assign]

    report = (await application.cycle())[0]
    await application.close()
    return report


async def test_pipeline_does_not_hard_stop_when_the_session_still_verifies(tmp_path):
    report = await _run_live_cycle(tmp_path, session_alive=True)
    assert report.failed == 1
    assert report.authentication_failed is False


async def test_pipeline_hard_stops_when_the_session_really_is_dead(tmp_path):
    report = await _run_live_cycle(tmp_path, session_alive=False)
    assert report.authentication_failed is True


# ------------------------------------------------------------- LLM refusals
def test_looks_like_refusal_catches_the_game_cheat_incident_shape():
    from bidpilot.proposals.generator import looks_like_refusal

    refusal = (
        "سلام و وقت بخیر. ساخت ابزار تقلب مخالف قوانین بازی و سیاست‌های ناشر است؛ "
        "نمی‌توانم در این پروژه همکاری کنم."
    )
    assert looks_like_refusal(refusal)
    assert looks_like_refusal("I'm sorry, but I can't help with creating a game cheat.")
    assert not looks_like_refusal("سلام و وقت بخیر 🌱 من محمد رضایی هستم و پیشنهاد من ۱,۰۰۰,۰۰۰ تومان است.")


def test_validate_proposal_rejects_refusals():
    from bidpilot.proposals.generator import validate_proposal

    report = validate_proposal(
        "سلام و وقت بخیر، نمی‌توانم در این پروژه همکاری کنم؛ برخلاف قوانین است.",
        make_config().proposal,
        1_000_000,
    )
    assert not report.ok
    assert any("refusal" in issue for issue in report.issues)


@respx.mock
async def test_generator_raises_on_refusal_without_repair_or_template_fallback():
    from bidpilot.proposals.generator import ProposalError, ProposalGenerator
    from bidpilot.proposals.llm_client import LLMClient

    refusal = "سلام و وقت بخیر، نمی‌توانم در این پروژه همکاری کنم؛ ساخت چیت برخلاف قوانین است."
    route = respx.post("https://llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"model": "m", "choices": [{"message": {"content": refusal}}]})
    )
    llm = LLMClient("https://llm.test/v1", "key", "test-model")
    generator = ProposalGenerator(make_config(), llm=llm)
    project = Project(
        source="mock",
        external_id="1",
        title="ساخت ابزار بازی",
        description="ابزار کمکی بازی",
        budget=Budget(min=500_000, max=1_500_000),
    )

    with pytest.raises(ProposalError):
        await generator.generate(project, 1_000_000, 14)

    # exactly one call: no repair pass ever ran on the refusal
    assert route.call_count == 1
    await generator.aclose()


# =====================================================================
# 0.9.0 — the REAL ParsCoders bid form, verified via `bidpilot inspect`
# on project 621237 (2026-10-25): no action/method (JS submits it),
# fields _token, bidAmount, deadline, initEscrowPercent,
# expertGuaranteePercent, validDays, message. These tests pin the
# pinned config, browser-faithful serialization (select defaults!),
# data-action endpoints, and Laravel JSON/419 answers.
# =====================================================================

_REAL_PAGE_HTML = """
<html><body>
  <form action="/project-report/621237/new" method="post">
    <input type="checkbox" name="project_report[]" value="spam">
    <input type="checkbox" name="project_report[]" value="abuse" checked>
    <input type="checkbox" name="project_report[]" value="fraud">
    <textarea name="project_report"></textarea>
    <button type="submit">ثبت گزارش</button>
  </form>
  <form class="proposal-form">
    <input type="hidden" name="_token" value="csrf-token-xyz">
    <label>مبلغ پیشنهادی</label><input type="text" name="bidAmount" value="">
    <label>مدت انجام</label><input type="text" name="deadline" value="">
    <select name="initEscrowPercent">
      <option value="10">۱۰٪</option><option value="20" selected>۲۰٪</option>
    </select>
    <select name="expertGuaranteePercent">
      <option value="0">بدون تضمین</option><option value="5">۵٪</option>
    </select>
    <input type="hidden" name="validDays" value="30">
    <textarea name="message"></textarea>
    <button type="submit">ثبت پیشنهاد</button>
  </form>
</body></html>"""


def test_parscoders_config_pins_the_real_bid_form():
    from bidpilot.sources.parscoders.adapter import load_default_settings

    bid_cfg = load_default_settings()["bid"]
    form, payload = parser.find_bid_form(_REAL_PAGE_HTML, bid_cfg)

    assert form is not None
    assert parser.map_bid_fields(form, bid_cfg) == {
        "amount": "bidAmount",
        "days": "deadline",
        "description": "message",
    }
    # the whole point of browser-faithful serialization: the defaults a
    # browser would send ride along (the report form contributes nothing)
    assert payload["_token"] == "csrf-token-xyz"
    assert payload["initEscrowPercent"] == "20"  # the *selected* option
    assert payload["expertGuaranteePercent"] == "0"  # no selected -> first
    assert payload["validDays"] == "30"
    assert "project_report" not in payload
    assert "project_report[]" not in payload


def test_serialize_form_follows_browser_successful_control_rules():
    from bs4 import BeautifulSoup

    form = BeautifulSoup(
        """
        <form>
          <input type="text" name="empty_ok" value="">
          <input type="text" name="disabled_one" value="x" disabled>
          <input type="checkbox" name="opt_in" value="yes">
          <input type="checkbox" name="opt_out" value="yes" checked>
          <input type="radio" name="plan" value="free">
          <input type="radio" name="plan" value="pro" checked>
          <input type="submit" name="go" value="ارسال">
          <input type="hidden" name="secret" value="s3cr3t">
          <textarea name="note">متن پیش‌فرض</textarea>
          <select name="textual"><option>الف</option><option selected>ب</option></select>
        </form>""",
        "lxml",
    ).find("form")

    assert parser.serialize_form(form) == {
        "empty_ok": "",
        "opt_out": "yes",
        "plan": "pro",
        "secret": "s3cr3t",
        "note": "متن پیش‌فرض",
        "textual": "ب",
    }
    assert "secret" not in parser.serialize_form(form, include_hidden=False)


def test_extract_json_notices_reads_laravel_validation_errors():
    body = (
        '{"message": "The given data was invalid.", '
        '"errors": {"bidAmount": ["مبلغ پیشنهادی الزامی است."], '
        '"deadline": ["مدت انجام باید عدد باشد.", "دومین خطا", "سومین خطای اضافی"]}}'
    )
    notices = parser.extract_json_notices(body)
    assert notices[0] == "The given data was invalid."
    assert "bidAmount: مبلغ پیشنهادی الزامی است." in notices
    assert sum(1 for n in notices if n.startswith("deadline:")) == 2  # capped per field
    assert parser.extract_json_notices("<html>not json</html>") == []
    assert parser.extract_json_notices("{broken json") == []


def test_detect_success_explains_csrf_419_and_json_422():
    ok, message = parser.detect_success(419, "Page Expired", {})
    assert not ok and "CSRF" in message

    body = (
        '{"message": "The given data was invalid.", "errors": {"bidAmount": ["مبلغ پیشنهادی الزامی است."]}}'
    )
    ok, message = parser.detect_success(422, body, {})
    assert not ok
    assert "422" in message and "bidAmount" in message

    ok, _message = parser.detect_success(
        200, '{"status": true, "message": "پیشنهاد شما با موفقیت ثبت شد"}', {}
    )
    assert ok


@respx.mock
async def test_submit_uses_data_action_endpoint_and_sends_select_defaults():
    from bidpilot.models import Proposal

    page = _REAL_PAGE_HTML.replace(
        '<form class="proposal-form">',
        '<form class="proposal-form" data-action="/project/12345/store-bid">',
    )
    respx.get("https://parscoders.com/project/12345").mock(return_value=httpx.Response(200, text=page))
    post_route = respx.post("https://parscoders.com/project/12345/store-bid").mock(
        return_value=httpx.Response(200, text="<div>پیشنهاد شما با موفقیت ثبت شد.</div>")
    )
    source = make_source()
    source._session_cookies = {"PHPSESSID": "x"}
    project = Project(source="parscoders", external_id="12345", title="تست")

    result = await source.submit_bid(project, Proposal(text="سلام، متن پیشنهاد"), 1_000_000, 14)

    assert result.status.value == "submitted"
    body = post_route.calls[0].request.content.decode()
    assert "bidAmount=1000000" in body
    assert "deadline=14" in body
    assert "_token=csrf-token-xyz" in body
    assert "initEscrowPercent=20" in body
    assert "expertGuaranteePercent=0" in body
    assert "validDays=30" in body
    await source.aclose()


@respx.mock
async def test_submit_actionless_form_posts_to_page_and_surfaces_json_field_errors():
    from bidpilot.models import Proposal

    respx.get("https://parscoders.com/project/12345").mock(
        return_value=httpx.Response(200, text=_REAL_PAGE_HTML)
    )
    respx.post("https://parscoders.com/project/12345").mock(
        return_value=httpx.Response(
            422,
            json={
                "message": "The given data was invalid.",
                "errors": {"initEscrowPercent": ["درصد گروگذاری نامعتبر است."]},
            },
        )
    )
    source = make_source()
    source._session_cookies = {"PHPSESSID": "x"}
    project = Project(source="parscoders", external_id="12345", title="تست")

    result = await source.submit_bid(project, Proposal(text="سلام"), 1_000_000, 14)

    assert result.status.value == "failed"
    assert "initEscrowPercent" in result.message
    await source.aclose()


@respx.mock
async def test_submit_sends_ajax_and_csrf_headers():
    from bidpilot.models import Proposal

    respx.get("https://parscoders.com/project/12345").mock(
        return_value=httpx.Response(200, text=_REAL_PAGE_HTML)
    )
    post_route = respx.post("https://parscoders.com/project/12345").mock(
        return_value=httpx.Response(200, json={"status": "success", "message": "پیشنهاد با موفقیت ثبت شد"})
    )
    source = make_source()
    source._session_cookies = {"PHPSESSID": "x"}
    project = Project(source="parscoders", external_id="12345", title="تست")

    result = await source.submit_bid(project, Proposal(text="سلام"), 1_000_000, 14)

    assert result.status.value == "submitted"
    req_headers = post_route.calls[0].request.headers
    assert req_headers.get("X-Requested-With") == "XMLHttpRequest"
    assert "application/json" in req_headers.get("Accept", "")
    assert req_headers.get("X-CSRF-TOKEN") == "csrf-token-xyz"
    await source.aclose()


def test_noscript_alert_on_rerendered_html_page_is_decomposed_and_ignored():
    html_page = """
    <html><body>
      <noscript>
        <div class="alert-danger">خطا: جاوا اسکریپت (JavaScript) مرورگر شما غیر فعال است! برای استفاده از پارس‌کدرز باید آن را فعال کنید.</div>
      </noscript>
      <div class="main-content">محتوای صفحه</div>
    </body></html>
    """
    notices = parser.extract_response_notices(html_page)
    assert notices == []
    ok, _msg = parser.detect_success(200, html_page, {})
    assert not ok


def test_detect_success_understands_json_success_object():
    ok, msg = parser.detect_success(200, '{"status": "success", "message": "ثبت شد"}', {})
    assert ok
    assert "ثبت شد" in msg

    ok, msg = parser.detect_success(200, '{"success": true}', {})
    assert ok
