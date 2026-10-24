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


def test_budget_range_split_across_lines_keeps_both_bounds():
    from bidpilot.sources.html_parser import enrich_from_detail, find_budget

    raw = "بودجه 25,000 تومان\n تا\n 500,000 تومان مهلت برای انجام 2روز وضعیت مناقصه باز"
    budget = find_budget(raw)
    assert budget is not None and budget.is_known
    assert (budget.min, budget.max) == (25_000, 500_000)

    list_html = (FIXTURES / "sample_list.html").read_text(encoding="utf-8")
    project = parser.parse_list_page(list_html, SETTINGS["list"], "parscoders", "https://parscoders.com")[0]
    detail_html = f"""
    <html><body>
      <main>
        <h1>{project.title}</h1>
        <p>بودجه 25,000 تومان
         تا
         500,000 تومان مهلت برای انجام 2روز وضعیت مناقصه باز</p>
      </main>
    </body></html>
    """
    settings = {
        "detail_fields": {
            "description": [],
            "budget": [".missing-budget-selector"],
            "skills": [],
        }
    }
    enriched = enrich_from_detail(detail_html, project, settings)
    assert enriched.budget is not None and enriched.budget.is_known
    assert (enriched.budget.min, enriched.budget.max) == (25_000, 500_000)


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


def test_fallback_cards_keeps_budget_and_description_siblings_together():
    html = """
    <html><body>
      <section class="listing-row">
        <div class="title"><h2><a href="/project/555">ساخت سامانه فروشگاهی</a></h2></div>
        <div class="description"><p>یک فروشگاه آنلاین با پنل مدیریت لازم است.</p></div>
        <div class="budget-label">بودجه: ۲ میلیون تومان</div>
        <div class="skills">مهارت‌ها: Python</div>
      </section>
      <section class="listing-row">
        <div class="title"><h2><a href="/project/556">ساخت وب‌سایت شرکتی</a></h2></div>
        <div class="description"><p>طراحی چند صفحه‌ی شرکتی با نمایش مناسب موبایل.</p></div>
      </section>
      <section class="listing-row">
        <div class="title"><h2><a href="/project/557">ساخت پنل مدیریتی</a></h2></div>
        <div class="description"><p>پیاده‌سازی پنل مدیریت و گزارش‌های کاربردی.</p></div>
      </section>
    </body></html>
    """
    settings = {
        "card_selectors": ["article.project-card"],
        "id_regex": r"/project/(\d+)",
        "fields": {
            "title": ["h2 a"],
            "link": ["h2 a"],
            "description": [".description p"],
            "budget": [".missing-budget-selector"],
            "skills": [".missing-skill-selector"],
        },
    }

    projects = parser.parse_list_page(html, settings, "ponisha", "https://ponisha.ir")

    assert len(projects) == 3
    assert projects[0].external_id == "555"
    assert "پنل مدیریت" in projects[0].description
    assert projects[0].budget is not None and projects[0].budget.is_known
    assert projects[0].budget.min == 2_000_000


def test_detail_budget_does_not_leak_from_recommended_projects():
    from bidpilot.models import Project
    from bidpilot.sources.html_parser import enrich_from_detail

    title = "طراحی قالب سبک وردپرس مشابه باسکول"
    html = f"""
    <html><body>
      <nav><a href="/project/100">فهرست یک</a><a href="/project/101">فهرست دو</a></nav>
      <main>
        <article class="project-content">
          <h1>{title}</h1>
          <p>جزئیات قالب در ادامه با مجری هماهنگ می‌شود.</p>
        </article>
      </main>
      <section class="related-projects">
        <article><a href="/project/999">پروژه پیشنهادی اول</a>
          <p>هزینه پیشنهادی مجری دیگر: ۱ میلیون تومان</p>
        </article>
        <article><a href="/project/998">پروژه پیشنهادی دوم</a></article>
      </section>
    </body></html>
    """
    project = Project(
        source="ponisha",
        external_id="555",
        title=title,
        url="https://ponisha.ir/project/555",
    )

    enriched = enrich_from_detail(
        html,
        project,
        {
            "require_project_scope_for_budget": True,
            "detail_fields": {
                "description": [".missing-description"],
                "budget": [".project-budget", ".budget", "[class*='price']"],
                "skills": [],
            },
            "_diagnostics": True,
        },
    )

    assert enriched.budget is None
    assert enriched.raw["detail_budget_source"] == "not_found"


def test_detail_budget_is_parsed_when_present_inside_project_scope():
    from bidpilot.models import Project
    from bidpilot.sources.html_parser import enrich_from_detail

    title = "ساخت اپلیکیشن فروشگاهی"
    html = f"""
    <html><body>
      <main>
        <article class="project-content">
          <h1>{title}</h1>
          <p>یک اپلیکیشن فروشگاهی با پنل مدیریت لازم است.</p>
          <div class="budget-label">بودجه: ۲۰ میلیون تومان</div>
        </article>
      </main>
      <section class="related-projects">
        <article><a href="/project/777">پروژه دیگر</a><p>بودجه: ۱ میلیون تومان</p></article>
        <article><a href="/project/778">پروژه دیگر دوم</a></article>
      </section>
    </body></html>
    """
    project = Project(
        source="ponisha",
        external_id="555",
        title=title,
        url="https://ponisha.ir/project/555",
    )

    enriched = enrich_from_detail(
        html,
        project,
        {
            "require_project_scope_for_budget": True,
            "detail_fields": {
                "description": [".missing-description"],
                "budget": [".project-budget"],
                "skills": [],
            },
        },
    )

    assert enriched.budget is not None and enriched.budget.is_known
    assert enriched.budget.min == 20_000_000
    assert enriched.raw["detail_budget_source"] in {"budget_node", "budget_node_label", "labeled_text_block"}


def test_detail_description_fallback_uses_ponisha_markers():
    from bidpilot.models import Project
    from bidpilot.sources.html_parser import enrich_from_detail

    title = "گویندگی و ضبط نریشن فارسی"
    html = f"""
    <html><body>
      <h1>{title}</h1>
      <p>زمان باقی‌مانده برای ارسال پیشنهاد</p>
      <section>
        برای داستان‌های سایت‌مان، نریشن فارسی با صدای گرم نیاز داریم.
        شروع کار با پنج نریشن است و ادامه‌ی کار در بسته‌های پنج‌تایی انجام می‌شود.
      </section>
      <h2>مهارت‌های مورد نیاز</h2><p>گویندگی، خدمات صوتی</p>
      <section>سرویس‌های مرتبط و پروژه‌های دیگر</section>
    </body></html>
    """
    project = Project(
        source="ponisha",
        external_id="556",
        title=title,
        url="https://ponisha.ir/project/556",
    )

    enriched = enrich_from_detail(
        html,
        project,
        {
            "require_project_scope_for_budget": True,
            "detail_fields": {
                "description": [".missing-description"],
                "budget": [".missing-budget"],
                "skills": [],
            },
            "description_fallback": {
                "start_markers": ["زمان باقی‌مانده برای ارسال پیشنهاد"],
                "end_markers": ["مهارت‌های مورد نیاز", "سرویس‌های مرتبط"],
            },
        },
    )

    assert "نریشن فارسی با صدای گرم" in enriched.description
    assert "مهارت‌های مورد نیاز" not in enriched.description
    assert "سرویس‌های مرتبط" not in enriched.description


def test_ponisha_header_budget_is_found_outside_title_scope_and_excluded_from_description():
    from bidpilot.models import Project
    from bidpilot.sources.html_parser import enrich_from_detail

    title = "طراحی قالب سبک وردپرس مشابه باسکول"
    html = f"""
    <html><body>
      <main>
        <article class="project-content">
          <h1>{title}</h1>
          <p>زمان باقی‌مانده برای ارسال پیشنهاد</p>
          <p>طراحی و پیاده‌سازی یک قالب سبک برای وردپرس با ظاهری مشابه باسکول.</p>
        </article>
        <div class="plain-metadata">تصریح 1 میلیون تا 12 میلیون تومان 02 آبان 1405 14:06</div>
        <h2>مهارت‌های مورد نیاز</h2>
        <p>وردپرس (WordPress) قالب</p>
        <section class="related-projects">
          <article><a href="/project/777">پروژه مرتبط اول</a><p>بودجه: ۹۹ میلیون تومان</p></article>
          <article><a href="/project/778">پروژه مرتبط دوم</a><p>بودجه: ۸۸ میلیون تومان</p></article>
        </section>
      </main>
    </body></html>
    """
    project = Project(
        source="ponisha",
        external_id="762250",
        title=title,
        url="https://ponisha.ir/project/762250",
    )

    enriched = enrich_from_detail(
        html,
        project,
        {
            "require_project_scope_for_budget": True,
            "detail_fields": {
                "description": [".missing-description"],
                "budget": [".project-budget", ".budget", "[class*='price']"],
                "skills": [".missing-skill"],
            },
            "description_fallback": {
                "start_markers": ["زمان باقی‌مانده برای ارسال پیشنهاد"],
                "end_markers": ["مهارت‌های مورد نیاز", "سرویس‌های مرتبط"],
            },
            "_diagnostics": True,
        },
    )

    assert enriched.budget is not None and enriched.budget.is_known
    assert enriched.budget.min == 1_000_000
    assert enriched.budget.max == 12_000_000
    assert enriched.raw["detail_budget_source"] == "project_header_money_node"
    assert "قالب سبک برای وردپرس" in enriched.description
    assert "12 میلیون" not in enriched.description
    assert "تصریح" not in enriched.description
    assert "02 آبان 1405" not in enriched.description


def test_ponisha_header_budget_never_uses_recommended_project_budget_after_skills():
    from bidpilot.models import Project
    from bidpilot.sources.html_parser import enrich_from_detail

    title = "پروژه اصلی بدون بودجه"
    html = f"""
    <html><body>
      <main>
        <article>
          <h1>{title}</h1>
          <p>زمان باقی‌مانده برای ارسال پیشنهاد</p>
          <p>شرح پروژه اصلی بدون مبلغ اعلام‌شده.</p>
        </article>
        <h2>مهارت‌های مورد نیاز</h2><p>Python</p>
        <section class="related-projects">
          <article><a href="/project/777">پروژه مرتبط اول</a><p>بودجه: ۹۹ میلیون تومان</p></article>
          <article><a href="/project/778">پروژه مرتبط دوم</a><p>بودجه: ۸۸ میلیون تومان</p></article>
        </section>
      </main>
    </body></html>
    """
    project = Project(
        source="ponisha",
        external_id="762251",
        title=title,
        url="https://ponisha.ir/project/762251",
    )

    enriched = enrich_from_detail(
        html,
        project,
        {
            "require_project_scope_for_budget": True,
            "detail_fields": {
                "description": [".missing-description"],
                "budget": [".missing-budget"],
                "skills": [],
            },
            "description_fallback": {
                "start_markers": ["زمان باقی‌مانده برای ارسال پیشنهاد"],
                "end_markers": ["مهارت‌های مورد نیاز", "سرویس‌های مرتبط"],
            },
        },
    )

    assert enriched.budget is None
    assert enriched.raw["detail_budget_source"] == "not_found"


def test_proposal_action_link_to_login_is_detected():
    from bidpilot.sources.html_parser import find_bid_form, find_proposal_login_link

    html = """
    <html><head><title>Project</title></head><body>
      <a href="/login">ارسال پیشنهاد</a>
      <form id="search-form">
        <input type="text" name="q">
        <button type="submit">جستجو</button>
      </form>
    </body></html>
    """
    form, _payload = find_bid_form(html, SETTINGS["bid"])
    assert form is None
    assert find_proposal_login_link(html, "https://parscoders.com/project/123") == "/login"


def test_detail_page_extracts_explicit_employer_deadline():
    from bidpilot.sources.html_parser import enrich_from_detail

    project = parser.parse_list_page(
        (FIXTURES / "sample_list.html").read_text(encoding="utf-8"),
        SETTINGS["list"],
        "parscoders",
        "https://parscoders.com",
    )[0]
    html = f"""
    <html><body><main>
      <h1>{project.title}</h1>
      <p>مهلت برای انجام 1روز وضعیت مناقصه باز (آماده دریافت پیشنهاد)</p>
    </main></body></html>
    """
    enriched = enrich_from_detail(
        html,
        project,
        {"detail_fields": {"description": [], "budget": [], "skills": []}},
    )
    assert enriched.employer_deadline == "1 روز"

