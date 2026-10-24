"""The AI discovery agent: it must browse the *real* site structure, not a list
of URLs we handed it, and it must bring back grounded facts (budget, deadline).
"""

from __future__ import annotations

import json
import re

import httpx
import pytest

from bidpilot.config import AppConfig, Profile, Secrets
from bidpilot.sources.discovery import DiscoveryAgent, DiscoveryError, digest_html
from bidpilot.sources.parscoders.adapter import ParscodersSource, load_default_settings

PROFILE = Profile(
    first_name="علی",
    full_name="علی اقبالی",
    role="برنامه‌نویس موبایل و بک‌اند",
    years_experience=5,
    stack=["Flutter", "Dart", "ASP.NET Core", "C#", "FastAPI"],
)

BASE = "https://parscoders.com"

HOME = """
<html><head><title>پارس‌کدرز | داشبورد</title></head><body>
  <nav>
    <a href="/project">پروژه‌های مرتبط با تخصص من</a>
    <a href="/project/only-available/1">همه پروژه‌های باز</a>
    <a href="/profile/kaspian">پروفایل من</a>
    <a href="/logout">خروج</a>
  </nav>
  <p>به پنل کاربری خوش آمدید.</p>
</body></html>
"""

MATCHED_LIST = """
<html><head><title>پروژه‌های مرتبط</title></head><body>
  <div class="project-list-item">
    <a href="/project/501/flutter-shop">ساخت اپلیکیشن فروشگاهی با Flutter</a>
    <span>بودجه: ۲۰ تا ۴۰ میلیون تومان</span><span>۱۰ روز</span>
  </div>
  <div class="project-list-item">
    <a href="/project/502/logo">طراحی لوگو برای کافه</a>
    <span>بودجه: ۲ میلیون تومان</span><span>۳ روز</span>
  </div>
</body></html>
"""

ALL_LIST = """
<html><head><title>همه پروژه‌ها</title></head><body>
  <a href="/project/501/flutter-shop">ساخت اپلیکیشن فروشگاهی با Flutter</a>
  <a href="/project/503/api">توسعه API با FastAPI</a>
</body></html>
"""

PROJECT_501 = """
<html><head><title>ساخت اپلیکیشن فروشگاهی با Flutter</title></head><body>
  <h1>ساخت اپلیکیشن فروشگاهی با Flutter</h1>
  <div id="project-description">یک فروشگاه موبایل با Flutter و بک‌اند ASP.NET Core نیاز دارم.
  سبد خرید، درگاه پرداخت و پنل مدیریت.</div>
  <div>بودجه: ۲۰ تا ۴۰ میلیون تومان</div>
  <div>مدت زمان مورد نظر کارفرما: ۱۰ روز</div>
  <div>مهارت‌ها: Flutter، C#، ASP.NET Core</div>
  <div>کارفرما: شرکت آوان</div>
</body></html>
"""

PROJECT_503 = """
<html><head><title>توسعه API با FastAPI</title></head><body>
  <h1>توسعه API با FastAPI</h1>
  <div id="project-description">بک‌اند اپلیکیشن با احراز هویت OTP.</div>
  <div>بودجه: توافقی</div>
  <div>مهلت: ۲ هفته</div>
</body></html>
"""

PAGES = {
    "/": HOME,
    "/project": MATCHED_LIST,
    "/project/only-available/1": ALL_LIST,
    "/profile/kaspian": "<html><title>پروفایل من</title><body>مهارت‌ها: Flutter, C#</body></html>",
    "/project/501/flutter-shop": PROJECT_501,
    "/project/503/api": PROJECT_503,
    "/project/502/logo": "<html><title>لوگو</title><body>طراحی لوگو</body></html>",
}

DETAILS = {
    "/project/501/flutter-shop": {
        "is_project": True,
        "title": "ساخت اپلیکیشن فروشگاهی با Flutter",
        "description": "فروشگاه موبایل با Flutter و بک‌اند ASP.NET Core، سبد خرید و درگاه پرداخت.",
        "budget_text": "بودجه: ۲۰ تا ۴۰ میلیون تومان",
        "deadline_text": "۱۰ روز",
        "skills": ["Flutter", "C#", "ASP.NET Core"],
        "category": "برنامه‌نویسی موبایل",
        "employer": "شرکت آوان",
    },
    "/project/503/api": {
        "is_project": True,
        "title": "توسعه API با FastAPI",
        "description": "بک‌اند اپلیکیشن با احراز هویت OTP.",
        "budget_text": "توافقی",
        "deadline_text": "۲ هفته",
        "skills": ["FastAPI"],
        "category": "",
        "employer": "",
    },
}


class ScriptedLLM:
    """A tiny stand-in for the model that behaves like a careful browser."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def complete(self, system, user, *, max_tokens=None, response_format=None):
        self.calls.append(user)

        if "استخراج‌کننده" in system:  # extraction prompt
            path = re.search(r"# متن صفحه‌ی پروژه \((.*?)\)", user).group(1)
            from urllib.parse import urlparse

            data = DETAILS.get(urlparse(path).path, {"is_project": False})
            return _Answer(json.dumps(data, ensure_ascii=False))

        links = re.findall(r"^\[(\d+)\] (.*?) — ", user, re.M)
        current = re.search(r"آدرس: (\S+)", user).group(1)
        from urllib.parse import urlparse

        path = urlparse(current).path
        by_label = {label: index for index, label in links}

        if path == "/":
            # the model must find the lists from the navigation itself
            for label, index in by_label.items():
                if "مرتبط با تخصص" in label:
                    return _Answer(json.dumps({"action": "open", "link": int(index), "why": "فهرست مرتبط"}))
            return _Answer(json.dumps({"action": "done", "why": "فهرستی پیدا نشد"}))

        if path == "/project":
            picks = [int(index) for index, label in links if "Flutter" in label or "فلاتر" in label]
            return _Answer(json.dumps({"action": "collect", "links": picks, "why": "فهرست مرتبط"}))

        return _Answer(json.dumps({"action": "done", "why": "تمام"}))

    async def aclose(self) -> None:
        return None


class _Answer:
    def __init__(self, text: str) -> None:
        self.text = text


@pytest.fixture()
def fetch():
    async def _fetch(url: str) -> str:
        from urllib.parse import urlparse

        path = urlparse(url).path
        if path not in PAGES:
            raise httpx.HTTPError(f"404 {path}")
        return PAGES[path]

    return _fetch


def test_digest_keeps_real_links_and_drops_noise():
    page = digest_html(HOME, BASE + "/", base_url=BASE)

    labels = [link.label for link in page.links]
    assert "پروژه‌های مرتبط با تخصص من" in labels
    assert all("خروج" not in label for label in labels)
    assert all(link.url.startswith(BASE) for link in page.links)
    assert "خوش آمدید" in page.text
    assert page.title.startswith("پارس‌کدرز")


def test_login_page_is_detected():
    page = digest_html("<html><body><input type='password' name='p'>ورود</body></html>", BASE, base_url=BASE)
    assert page.is_login is True


def test_a_login_modal_is_not_a_login_wall():
    html = (
        "<html><body><nav><a href='/p/1'>پروژه یک</a></nav>"
        "<p>"
        + "متن بلند صفحه‌ی اصلی سایت. " * 40
        + "</p><div class='modal'><input type='password'></div></body></html>"
    )
    page = digest_html(html, BASE + "/", base_url=BASE)
    assert page.is_login is False


async def test_agent_reads_a_project_the_model_opened_directly(fetch):
    class Opener(ScriptedLLM):
        async def complete(self, system, user, *, max_tokens=None, response_format=None):
            if "استخراج‌کننده" in system:
                path = re.search(r"# متن صفحه‌ی پروژه \((.*?)\)", user).group(1)
                from urllib.parse import urlparse

                data = DETAILS.get(urlparse(path).path, {"is_project": False})
                return _Answer(json.dumps(data, ensure_ascii=False))
            if "آدرس: https://parscoders.com/project/501" in user:
                return _Answer(json.dumps({"action": "done", "why": "پروژه را خواندم"}))
            return _Answer(json.dumps({"action": "open", "link": 0, "why": "پروژه"}))

    agent = DiscoveryAgent(
        source_name="parscoders",
        base_url=BASE,
        fetch=fetch,
        llm=Opener(),
        profile=PROFILE,
        seed_paths=["/project/501/flutter-shop"],
    )

    result = await agent.discover()

    # no «collect» involved: the agent recognised the detail page by itself
    assert [p.external_id for p in result.projects] == ["501"]
    assert result.pages_read == 2  # the page itself + the detail extraction
    assert result.llm_calls == 1


async def test_a_login_wall_mid_run_is_an_error_not_an_empty_result(fetch):
    async def half_broken(url: str) -> str:
        if url.endswith("/project/501/flutter-shop"):
            return "<html><body><input type='password'>ورود به حساب</body></html>"
        return await fetch(url)

    class Collector(ScriptedLLM):
        async def complete(self, system, user, *, max_tokens=None, response_format=None):
            if "استخراج‌کننده" in system:
                return _Answer(json.dumps({"is_project": False}))
            return _Answer(json.dumps({"action": "collect", "links": [0], "why": "فهرست"}))

    agent = DiscoveryAgent(
        source_name="parscoders",
        base_url=BASE,
        fetch=half_broken,
        llm=Collector(),
        profile=PROFILE,
        seed_paths=["/project"],
    )

    with pytest.raises(DiscoveryError):
        await agent.discover()


async def test_agent_finds_the_lists_and_reads_projects(fetch):
    llm = ScriptedLLM()
    agent = DiscoveryAgent(
        source_name="parscoders",
        base_url=BASE,
        fetch=fetch,
        llm=llm,
        profile=PROFILE,
        brief="سایت دو فهرست دارد: مرتبط با تخصص و همه‌ی پروژه‌ها.",
        seed_paths=["/"],
        max_steps=6,
    )

    result = await agent.discover()

    # it started from the homepage and found the *expertise* list by itself
    assert result.visited[0] == BASE + "/"
    assert BASE + "/project" in result.visited
    # only the relevant project came back (the logo project was never collected)
    assert [p.external_id for p in result.projects] == ["501"]
    project = result.projects[0]
    assert project.title.startswith("ساخت اپلیکیشن فروشگاهی")
    assert project.budget is not None
    assert (project.budget.min, project.budget.max) == (20_000_000, 40_000_000)
    from bidpilot.text import parse_days

    # digits may come back normalized (10) — what matters is the parsed duration
    assert parse_days(project.employer_deadline) == 10
    assert "Flutter" in project.skills
    assert project.raw["discovered_by"] == "ai"
    assert result.pages_read >= 3


async def test_agent_reports_a_dead_session(fetch):
    async def login_fetch(url: str) -> str:
        return "<html><body><form><input type='password'></form></body></html>"

    agent = DiscoveryAgent(
        source_name="parscoders",
        base_url=BASE,
        fetch=login_fetch,
        llm=ScriptedLLM(),
        profile=PROFILE,
        seed_paths=["/"],
    )

    with pytest.raises(DiscoveryError) as exc:
        await agent.discover()
    assert "کوکی" in str(exc.value)


async def test_agent_refuses_links_that_were_not_on_the_page(fetch):
    class Hallucinating(ScriptedLLM):
        async def complete(self, system, user, *, max_tokens=None, response_format=None):
            if "استخراج‌کننده" in system:
                return _Answer(json.dumps({"is_project": False}))
            return _Answer(json.dumps({"action": "open", "link": 99, "why": "از خودم ساختم"}))

    agent = DiscoveryAgent(
        source_name="parscoders",
        base_url=BASE,
        fetch=fetch,
        llm=Hallucinating(),
        profile=PROFILE,
        seed_paths=["/"],
        max_steps=2,
    )

    result = await agent.discover()
    assert result.projects == []
    assert any("نامعتبر" in note for note in result.notes)


# ---------------------------------------------------------------------------
# Source integration: AI first, selectors as the safety net
# ---------------------------------------------------------------------------
def make_config() -> AppConfig:
    from bidpilot.config import SourceConfig

    return AppConfig(
        profile=PROFILE,
        llm={"enabled": True, "provider": "groq"},
        runtime={"poll_interval_seconds": 1, "jitter_seconds": 0},
        sources=[SourceConfig(type="parscoders", settings={})],
        database_url="sqlite+aiosqlite:///:memory:",
    )


def make_source(llm=None, monkeypatch=None) -> ParscodersSource:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in PAGES:
            return httpx.Response(200, text=PAGES[path])
        return httpx.Response(404, text="not found")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=BASE)
    source = ParscodersSource({}, make_config(), Secrets(), client=client)
    if llm is not None:
        from types import SimpleNamespace

        import bidpilot.sources.html_market as market

        source._discovery_providers = lambda: [SimpleNamespace(id="fake")]  # type: ignore[method-assign]
        if monkeypatch is not None:
            monkeypatch.setattr(market, "build_client", lambda provider, cfg: llm)
        else:  # pragma: no cover - only used by the selector-mode test
            market.build_client = lambda provider, cfg: llm  # type: ignore[assignment]
    return source


async def test_source_uses_ai_discovery_when_a_key_exists(monkeypatch):
    llm = ScriptedLLM()
    source = make_source(llm, monkeypatch)
    try:
        projects = await source.fetch_projects()
    finally:
        await source.aclose()

    assert [p.external_id for p in projects] == ["501"]
    assert source.last_discovery is not None
    assert source.last_discovery.used_llm


async def test_source_falls_back_to_selectors_when_the_model_fails(monkeypatch):
    class Broken(ScriptedLLM):
        async def complete(self, *args, **kwargs):
            raise RuntimeError("429 rate limit")

    original = make_source(Broken(), monkeypatch)
    try:
        projects = await original.fetch_projects()
    finally:
        await original.aclose()

    # the same pages, read by the deterministic parser instead of the model
    assert [p.external_id for p in projects] == ["501", "502"]
    assert projects[0].budget is not None
    assert "429" in original.last_discovery_error


async def test_selector_mode_never_calls_the_model(monkeypatch):
    class Exploding(ScriptedLLM):
        async def complete(self, *args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("the model must not be called in selectors mode")

    source = make_source(Exploding(), monkeypatch)
    source.settings["discovery"] = {"mode": "selectors", "seed_paths": ["/"]}
    try:
        await source.fetch_projects()
    finally:
        await source.aclose()

    assert source.last_discovery is None


def test_shipped_site_briefs_describe_the_logic_and_not_the_urls():
    for site in ("parscoders", "ponisha", "karlancer"):
        cfg = load_default_settings() if site == "parscoders" else None
        if cfg is None:
            from importlib import import_module

            cfg = import_module(f"bidpilot.sources.{site}.adapter").load_default_settings()
        block = cfg["discovery"]
        assert block["mode"] in {"ai", "auto", "selectors"}
        assert block["seed_paths"] == ["/"]
        brief = block["brief"]
        assert "فهرست" in brief
        # the prompt must teach the *logic*; it must not hand over the list URLs
        assert "only-available" not in brief
        assert "http" not in brief


async def test_a_list_page_sends_links_not_prose(fetch):
    """Groq's free tier is 8,000 tokens/min: a list page must not ship its text."""
    long_noise = "این یک متن توضیحی بسیار طولانی درباره‌ی قوانین سایت است. " * 60
    extra_links = "".join(f"<a href='/project/70{i}/job-{i}'>پروژه شماره {i}</a>" for i in range(4))
    list_html = MATCHED_LIST.replace("</body>", f"{extra_links}<p>{long_noise}</p></body>")

    async def list_fetch(url: str) -> str:
        return list_html

    captured: dict[str, str] = {}

    class Recorder(ScriptedLLM):
        async def complete(self, system, user, *, max_tokens=None, response_format=None):
            if "استخراج‌کننده" not in system:
                captured["prompt"] = user
            return _Answer(json.dumps({"action": "done", "why": "کافی است"}))

    agent = DiscoveryAgent(
        source_name="parscoders",
        base_url=BASE,
        fetch=list_fetch,
        llm=Recorder(),
        profile=PROFILE,
        seed_paths=["/project"],
    )
    await agent.discover()

    prompt = captured["prompt"]
    assert len(prompt) < 4_000, f"list prompt too big for 8k TPM: {len(prompt)} chars"
    # 60 repetitions of the paragraph would mean the full text was sent
    assert prompt.count("قوانین سایت") <= 3
    assert "فهرست پروژه است" in prompt


async def test_projects_collected_before_a_rate_limit_are_kept(fetch):
    class Flaky(ScriptedLLM):
        async def complete(self, system, user, *, max_tokens=None, response_format=None):
            if "استخراج‌کننده" in system:
                path = re.search(r"# متن صفحه‌ی پروژه \((.*?)\)", user).group(1)
                from urllib.parse import urlparse

                data = DETAILS.get(urlparse(path).path, {"is_project": False})
                return _Answer(json.dumps(data, ensure_ascii=False))
            if "آدرس: https://parscoders.com/project\n" in user:
                return _Answer(json.dumps({"action": "collect", "links": [0], "why": "فهرست"}))
            raise RuntimeError("429 rate limit")

    agent = DiscoveryAgent(
        source_name="parscoders",
        base_url=BASE,
        fetch=fetch,
        llm=Flaky(),
        profile=PROFILE,
        # the second seed hits the failing decision *after* the project was read
        seed_paths=["/project", "/"],
    )

    result = await agent.discover()

    assert [p.external_id for p in result.projects] == ["501"]
    assert any("نیمه‌کاره" in note for note in result.notes)


def test_provider_errors_become_actionable_persian():
    from bidpilot.sources.discovery import friendly_llm_error

    rate = friendly_llm_error(
        'LLM HTTP 429: {"error":{"message":"Rate limit reached ... tokens per minute (TPM)"}}'
    )
    assert "۸٬۰۰۰" in rate and "429" not in rate

    key = friendly_llm_error("LLM rejected the request (HTTP 401): invalid api key")
    assert "/llm" in key

    assert "چیز عجیبی" in friendly_llm_error("چیز عجیبی")


def test_a_403_is_explained_as_a_country_block():
    from bidpilot.sources.discovery import friendly_llm_error

    text = friendly_llm_error('LLM rejected the request (HTTP 403): {"error":{"message":"Forbidden"}}')

    assert "پروکسی" in text or "VPN" in text
    assert "/proxy" in text  # a next step, not just a complaint


def test_an_empty_reasoning_answer_is_explained():
    from bidpilot.sources.discovery import friendly_llm_error

    text = friendly_llm_error("مدل بودجه‌ی خروجی را با «استدلال» تمام کرد و پاسخ خالی ماند")

    assert "استدلال" in text
    assert "gpt-oss-20b" in text


async def test_a_failed_search_is_not_retried_every_cycle(monkeypatch):
    """A blocked provider must not be hammered on every 120-second poll."""
    attempts: list[str] = []

    class Blocked(ScriptedLLM):
        async def complete(self, *args, **kwargs):
            attempts.append("call")
            raise RuntimeError('LLM rejected the request (HTTP 403): {"error":{"message":"Forbidden"}}')

    source = make_source(Blocked(), monkeypatch)
    source.settings["discovery"] = {"mode": "auto", "seed_paths": ["/"], "failure_cooldown_seconds": 900}
    try:
        await source.fetch_projects()
        assert source.last_discovery_error
        calls_after_first = len(attempts)

        # the next cycles are inside the cooldown: selectors only, no model calls
        await source.fetch_projects()
        await source.fetch_projects()
        assert len(attempts) == calls_after_first
        assert source.ai_discovery_enabled() is False
    finally:
        await source.aclose()


def test_digest_keeps_page_navigation_and_late_labeled_metadata():
    html = """
    <html><head><title>صفحه پروژه</title></head><body>
      <nav><a href="/project">پروژه‌های مرتبط</a><a href="/profile">پروفایل من</a></nav>
      <main><h1>ساخت سامانه فروشگاهی</h1>
        <div id="project-description"><p>شرح نیازمندی اصلی فروشگاه با جزئیات فنی.</p></div>
      </main>
      <aside>بودجه: ۲۰ تا ۴۰ میلیون تومان</aside>
      <aside>مهلت تحویل: ۱۰ روز</aside>
      <footer>لینک‌های شبکه اجتماعی و اطلاعات غیرمرتبط سایت</footer>
    </body></html>
    """
    page = digest_html(html, BASE + "/project/901", base_url=BASE, max_text=1_000)

    assert any(link.label == "پروژه‌های مرتبط" for link in page.links)
    assert "شرح نیازمندی اصلی فروشگاه" in page.text
    assert "بودجه: 20 تا 40 میلیون تومان" in page.text
    assert "مهلت تحویل: 10 روز" in page.text
    assert "اطلاعات غیرمرتبط سایت" not in page.text


def test_model_cannot_replace_a_visible_budget_with_an_invented_one(fetch):
    from bidpilot.sources.discovery import _extract_description

    url = BASE + "/project/501/flutter-shop"
    page = digest_html(PROJECT_501, url, base_url=BASE, max_text=8_000)
    agent = DiscoveryAgent(
        source_name="parscoders",
        base_url=BASE,
        fetch=fetch,
        llm=ScriptedLLM(),
        profile=PROFILE,
    )
    extracted = agent._to_project(
        url,
        {
            "is_project": True,
            "title": "ساخت اپلیکیشن فروشگاهی با Flutter",
            "description": "خلاصه‌ی کوتاه مدل",
            "budget_text": "بودجه: ۹۹۹ میلیون تومان",
            "deadline_text": "۹۹ روز",
            "skills": ["Flutter", "مهارت ساختگی"],
            "category": "دسته‌ی ساختگی",
            "employer": "کارفرمای ساختگی",
        },
        page,
        source_description=_extract_description(PROJECT_501),
    )

    assert extracted is not None
    assert extracted.budget is not None
    assert (extracted.budget.min, extracted.budget.max) == (20_000_000, 40_000_000)
    assert extracted.employer_deadline == "10 روز"
    assert extracted.skills == ["Flutter"]
    assert extracted.category is None
    assert extracted.employer is None
    assert "سبد خرید" in extracted.description
    assert extracted.raw["budget_source"] == "page_text"


def test_project_description_extractor_preserves_late_requirements():
    from bidpilot.sources.discovery import _extract_description

    important = "شرط پذیرش پایانی و جزئیات نهایی باید حفظ شود"
    html = (
        '<html><body><h1>پروژه</h1><div id="project-description">'
        + ("شرح فنی قابل بررسی و محدودیت اجرای پروژه. " * 80)
        + important
        + "</div></body></html>"
    )

    description = _extract_description(html)

    assert important in description
    assert len(description) > 2_600


async def test_ai_discovery_logs_visited_pages_decisions_and_extracted_project(caplog, fetch):
    import logging

    caplog.set_level(logging.INFO, logger="bidpilot.sources.discovery")
    llm = ScriptedLLM()
    agent = DiscoveryAgent(
        source_name="ponisha",
        base_url="https://ponisha.ir",
        fetch=fetch,
        llm=llm,
        profile=PROFILE,
        brief="پیدا کردن فهرست پروژه‌های مرتبط و خواندن صفحه جزئیات.",
        seed_paths=["/"],
        max_steps=5,
        max_detail_pages=2,
        max_projects=2,
    )

    result = await agent.discover()

    assert result.projects
    assert "[AI-DISCOVERY][PAGE]" in caplog.text
    assert "[AI-DISCOVERY][PAGE-LINKS]" in caplog.text
    assert "[AI-DISCOVERY][MODEL-OUTPUT]" in caplog.text
    assert "[AI-EXTRACT][VISIBLE-TEXT]" in caplog.text
    assert "[AI-EXTRACT][MODEL-OUTPUT]" in caplog.text
    assert "[AI-EXTRACT][NORMALIZED]" in caplog.text
    assert "بودجه: ۲۰ تا ۴۰ میلیون تومان" in caplog.text
    assert "https://parscoders.com/project/501/flutter-shop" in caplog.text
