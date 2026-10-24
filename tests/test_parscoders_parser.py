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


def test_budget_is_read_from_the_card_text_when_the_selector_is_empty():
    html = """
    <html><body>
      <div class="project-list-item">
        <h2><a href="/project/555">ساخت اپلیکیشن فلاتر</a></h2>
        <p class="project-description">نیاز به برنامه‌نویس Flutter</p>
        <div class="meta">بودجه: ۵۰۰,۰۰۰ تا ۱,۵۰۰,۰۰۰ تومان</div>
        <div class="meta">۱٬۲۳۴ پروژه ثبت شده</div>
      </div>
    </body></html>
    """
    settings = {**SETTINGS["list"], "fields": {**SETTINGS["list"]["fields"]}}
    settings["fields"]["budget"] = [".no-such-budget-element"]
    projects = parser.parse_list_page(html, settings, "parscoders", "https://parscoders.com")

    assert len(projects) == 1
    budget = projects[0].budget
    assert budget is not None and budget.is_known
    # the page counter (۱۲۳۴ پروژه) must never become the budget
    assert (budget.min, budget.max) == (500_000, 1_500_000)


def test_find_budget_handles_labels_and_rejects_counters():
    from bidpilot.sources.html_parser import find_budget

    assert find_budget("حق‌الزحمه: ۲ میلیون تومان").min == 2_000_000
    labeled = find_budget("بودجه‌ی پروژه ۳۰۰ تا ۷۰۰ هزار تومان است")
    assert labeled is not None and (labeled.min, labeled.max) == (300_000, 700_000)
    assert find_budget("۱٬۲۳۴ پروژه ثبت شده") is None
    assert find_budget("سلام، پروژه‌ی ساده‌ای است") is None


def test_detail_page_budget_fallback():
    html = """
    <html><body>
      <div class="project-description">توضیح کامل پروژه</div>
      <div class="side">بودجه: ۱٬۰۰۰٬۰۰۰ تا ۲٬۰۰۰٬۰۰۰ تومان</div>
    </body></html>
    """
    project = parser.parse_list_page(
        (FIXTURES / "sample_list.html").read_text(encoding="utf-8"),
        SETTINGS["list"],
        "parscoders",
        "https://parscoders.com",
    )[2]
    settings = {**SETTINGS["detail"], "detail_fields": {**SETTINGS["detail"]["detail_fields"]}}
    settings["detail_fields"]["budget"] = [".no-such-budget-element"]
    enriched = parser.enrich_from_detail(html, project, settings)
    assert enriched.budget is not None and enriched.budget.is_known
    assert enriched.budget.min == 1_000_000 and enriched.budget.max == 2_000_000


def test_diagnostics_log_raw_card_and_detail_budget_evidence(caplog):
    import logging

    from bidpilot.sources.html_parser import enrich_from_detail, parse_list_page

    list_html = """
    <html><body>
      <article class="project-list-item">
        <h2><a href="/project/9001">ساخت فروشگاه اینترنتی با وردپرس</a></h2>
        <p>اتصال درگاه پرداخت و طراحی صفحه محصول</p>
        <div class="meta">بودجه: ۲۰,۰۰۰,۰۰۰ تا ۴۰,۰۰۰,۰۰۰ تومان</div>
      </article>
    </body></html>
    """
    list_settings = {
        "card_selectors": ["article.project-list-item"],
        "id_regex": r"/project/(\\d+)",
        "fields": {
            "title": ["h2 a"],
            "link": ["h2 a"],
            "budget": [".missing-budget-selector"],
            "description": ["p"],
        },
        "_diagnostics": True,
    }

    with caplog.at_level(logging.INFO, logger="bidpilot.sources.html_parser"):
        projects = parse_list_page(list_html, list_settings, "ponisha", "https://ponisha.ir")

        detail_html = """
        <html><body>
          <h1>ساخت فروشگاه اینترنتی با وردپرس</h1>
          <div class="description">اتصال درگاه پرداخت و طراحی صفحه محصول با جزئیات کامل پروژه.</div>
          <section class="metadata">بودجه: ۲۰,۰۰۰,۰۰۰ تا ۴۰,۰۰۰,۰۰۰ تومان</section>
          <p>مدت زمان مورد نظر کارفرما: ۱۰ روز</p>
        </body></html>
        """
        enriched = enrich_from_detail(
            detail_html,
            projects[0],
            {
                "detail_fields": {
                    "description": [".description"],
                    "budget": [".missing-budget-selector"],
                    "skills": [],
                },
                "_diagnostics": True,
            },
        )

    assert len(projects) == 1
    assert enriched.budget is not None and enriched.budget.is_known
    assert enriched.budget.min == 20_000_000 and enriched.budget.max == 40_000_000
    assert enriched.raw["detail_budget_source"] == "detail_page_text"
    assert "[PARSER][CARD-TEXT]" in caplog.text
    assert "[PARSER][DETAIL-TEXT]" in caplog.text
    assert "بودجه: 20,000,000 تا 40,000,000 تومان" in caplog.text
    assert "detail_budget_source" not in caplog.text or "detail_page_text" in caplog.text
