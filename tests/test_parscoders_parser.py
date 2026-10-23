from pathlib import Path

from bidpilot.sources.parscoders import parser
from bidpilot.sources.parscoders.adapter import load_default_settings, parse_cookie_header

FIXTURES = Path(__file__).parent / "fixtures"
SETTINGS = load_default_settings()


def test_parse_list_page():
    html = (FIXTURES / "sample_list.html").read_text(encoding="utf-8")
    projects = parser.parse_list_page(html, SETTINGS["list"], "parscoders", "https://parscoders.com")

    assert len(projects) == 3
    first = projects[0]
    assert first.external_id == "12345"
    assert "ربات تلگرام" in first.title
    assert first.url.startswith("https://parscoders.com/project/12345")
    assert first.budget is not None
    assert first.budget.min == 500_000 and first.budget.max == 1_500_000
    assert "Python" in first.skills
    assert first.category == "برنامه‌نویسی و توسعه"
    assert first.published_at is not None


def test_colloquial_budget_and_negotiable():
    html = (FIXTURES / "sample_list.html").read_text(encoding="utf-8")
    projects = parser.parse_list_page(html, SETTINGS["list"], "parscoders", "https://parscoders.com")
    second = next(p for p in projects if p.external_id == "12346")
    assert second.budget is not None
    assert (second.budget.min, second.budget.max) == (300_000, 700_000)

    third = next(p for p in projects if p.external_id == "12347")
    assert third.budget is not None and not third.budget.is_known


def test_enrich_from_detail():
    html = (FIXTURES / "sample_project.html").read_text(encoding="utf-8")
    list_html = (FIXTURES / "sample_list.html").read_text(encoding="utf-8")
    project = parser.parse_list_page(list_html, SETTINGS["list"], "parscoders", "https://parscoders.com")[0]

    enriched = parser.enrich_from_detail(html, project, SETTINGS["detail"])
    assert "زرین‌پال" in enriched.description
    assert "WooCommerce" in enriched.skills
    assert enriched.employer == "کارفرمای نمونه"
    assert len(enriched.description) > len(project.description)


def test_bid_form_payload_and_field_mapping():
    html = (FIXTURES / "sample_project.html").read_text(encoding="utf-8")
    form, payload = parser.find_bid_form(html, SETTINGS["bid"])
    assert form is not None
    assert payload["_csrf"] == "csrf-token-abc123"
    assert payload["project_id"] == "12345"

    mapping = parser.map_bid_fields(form, SETTINGS["bid"])
    assert mapping["amount"] == "amount"
    assert mapping["description"] == "description"
    assert mapping["days"] == "delivery_days"


def test_detect_success_and_failure():
    ok, message = parser.detect_success(200, "<div>پیشنهاد شما با موفقیت ثبت شد.</div>", SETTINGS["bid"])
    assert ok, message

    ok, message = parser.detect_success(200, "<div>شما قبلاً پیشنهاد داده‌اید.</div>", SETTINGS["bid"])
    assert not ok and "قبلاً" in message

    ok, _ = parser.detect_success(500, "<div>خطای سرور</div>", SETTINGS["bid"])
    assert not ok


def test_parse_cookie_header():
    assert parse_cookie_header("PHPSESSID=abc; other=1") == {"PHPSESSID": "abc", "other": "1"}
    assert parse_cookie_header("raw-session-id") == {"PHPSESSID": "raw-session-id"}
    assert parse_cookie_header(None) == {}


def test_extract_external_id_and_fallback():
    assert parser.extract_external_id("https://parscoders.com/project/98765/slug") == "98765"
    assert parser.extract_external_id("https://parscoders.com/about") is None
    assert parser.fallback_id("a", "b").startswith("h")
