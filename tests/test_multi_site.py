"""Several marketplaces at once, one active at a time, switched from Telegram."""

from pathlib import Path

import pytest

from bidpilot import onboarding
from bidpilot.bot import BotService
from bidpilot.config import reset_secret_cache
from bidpilot.credentials import save_site_credentials, site_credentials
from bidpilot.settings_store import SettingsStore, get_store, reset_store_cache
from bidpilot.sources import available_sources, create_source, get_source_class

ROOT = Path(__file__).resolve().parents[1]
SITES = ("parscoders", "ponisha", "karlancer")


class FakeAPI:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict | None]] = []
        self.answered: list[str] = []

    async def send_message(self, chat_id, text, *, reply_markup=None, parse_mode="HTML"):
        self.sent.append((str(chat_id), text, reply_markup))

    async def answer_callback(self, callback_id, text=""):
        self.answered.append(callback_id)

    @property
    def last_text(self) -> str:
        return self.sent[-1][1] if self.sent else ""

    @property
    def last_keyboard(self) -> dict:
        return (self.sent[-1][2] or {}).get("inline_keyboard") or []


def make_service(tmp_path, monkeypatch) -> tuple[BotService, FakeAPI, SettingsStore]:
    monkeypatch.setenv("BIDPILOT_SETTINGS_FILE", str(tmp_path / "data" / "settings.json"))
    reset_store_cache()
    reset_secret_cache()
    store = get_store()
    api = FakeAPI()
    service = BotService(store, api, config_path=str(ROOT / "config" / "config.yaml"))
    return service, api, store


def make_store(tmp_path) -> SettingsStore:
    return SettingsStore(tmp_path / "data" / "settings.json")


def callback(data: str) -> dict:
    return {
        "update_id": 7,
        "callback_query": {"id": "cb", "data": data, "message": {"chat": {"id": 777}}},
    }


# --------------------------------------------------------------- registration
def test_the_three_iranian_sites_are_registered():
    names = available_sources()
    for site in SITES:
        assert site in names


@pytest.mark.parametrize(
    ("site", "base_url", "list_path"),
    [
        ("parscoders", "https://parscoders.com", "/project"),
        ("ponisha", "https://ponisha.ir", "/search/projects"),
        ("karlancer", "https://karlancer.com", "/projects"),
    ],
)
def test_each_site_loads_its_own_yaml(site, base_url, list_path, tmp_path, monkeypatch):
    monkeypatch.setenv("BIDPILOT_SETTINGS_FILE", str(tmp_path / "data" / "settings.json"))
    reset_store_cache()
    reset_secret_cache()
    from bidpilot.config import AppConfig, get_secrets

    source = create_source(site, {}, AppConfig(), get_secrets())
    assert source.base_url == base_url
    assert source.settings["list"]["path"] == list_path
    assert get_source_class(site).config_file.name == "config.yaml"


# ------------------------------------------------------------------- switching
def test_switch_keeps_each_sites_own_credentials(tmp_path):
    store = make_store(tmp_path)
    save_site_credentials(store, "parscoders", method="cookie", cookie="PHPSESSID=pars")
    save_site_credentials(store, "ponisha", method="cookie", cookie="PHPSESSID=poni")

    assert onboarding.switch_site(store, "ponisha") == "ponisha"
    assert store.get("marketplace.name") == "ponisha"
    assert site_credentials("parscoders", store).cookies == {"PHPSESSID": "pars"}
    assert site_credentials("ponisha", store).cookies == {"PHPSESSID": "poni"}

    # switching never wipes the previous site's account
    onboarding.switch_site(store, "karlancer")
    assert store.get("credentials.parscoders.cookie") == "PHPSESSID=pars"


def test_switch_to_an_unknown_site_is_rejected(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(onboarding.QuestionError) as exc:
        onboarding.switch_site(store, "freelancer.com")
    assert "پونیشا" in str(exc.value) or "ponisha" in str(exc.value)
    assert store.get("marketplace.name") is None


def test_sites_overview_marks_the_active_site_first(tmp_path):
    store = make_store(tmp_path)
    save_site_credentials(store, "ponisha", method="cookie", cookie="PHPSESSID=poni")
    onboarding.switch_site(store, "ponisha")

    rows = onboarding.sites_overview(store)
    assert rows[0]["site"] == "ponisha"
    assert rows[0]["active"] is True and rows[0]["ready"] is True
    by_site = {row["site"]: row for row in rows}
    assert by_site["karlancer"]["ready"] is False
    assert by_site["parscoders"]["ready"] is False
    assert by_site["mock"]["ready"] is True


def test_market_questions_follow_the_active_site(tmp_path):
    store = make_store(tmp_path)
    onboarding.switch_site(store, "ponisha")
    keys = [q.store_key(store) for q in onboarding.marketplace_questions(store)]
    assert "credentials.ponisha.method" in keys
    assert "credentials.ponisha.cookie" in keys
    assert not any(key.startswith("credentials.parscoders.") for key in keys)

    method = next(q for q in onboarding.marketplace_questions(store) if q.key == "credentials.method")
    assert onboarding.parse_answer(method, "credentials", store) == "credentials"
    username = next(
        q
        for q in onboarding.marketplace_questions(store)
        if q.store_key(store) == "credentials.ponisha.username"
    )
    assert username.when(store) is False  # cookie mode: username is not asked

    onboarding.apply_answer(store, method, "credentials")
    assert username.when(store) is True


def test_readiness_and_status_report_the_active_site(tmp_path):
    store = make_store(tmp_path)
    onboarding.switch_site(store, "karlancer")
    state = onboarding.readiness(store)
    assert state["marketplace"] == "karlancer"
    assert state["credentials"] is False
    assert "کارلنسر" in onboarding.status_text(store)

    save_site_credentials(store, "karlancer", method="cookie", cookie="PHPSESSID=k")
    assert onboarding.readiness(store)["credentials"] is True


# ------------------------------------------------------------------- telegram
async def test_sites_command_lists_all_sites_and_switches(tmp_path, monkeypatch):
    service, api, store = make_service(tmp_path, monkeypatch)
    store.set("telegram.chat_ids", ["777"])
    save_site_credentials(store, "parscoders", method="cookie", cookie="PHPSESSID=pars")

    await service.handle_command("777", "/sites", "")
    text = api.last_text
    assert "پارس‌کدرز" in text and "پونیشا" in text and "کارلنسر" in text
    assert "کوکی ثبت شده" in text  # parscoders has an account
    callbacks = [row[0]["callback_data"] for row in api.last_keyboard]
    assert "site:ponisha" in callbacks and "site:karlancer" in callbacks

    await service.handle_update(callback("site:ponisha"))
    assert store.get("marketplace.name") == "ponisha"
    assert "پونیشا" in api.last_text
    assert "/market" in api.last_text  # no account for ponisha yet


async def test_site_command_switches_and_remembers(tmp_path, monkeypatch):
    service, api, store = make_service(tmp_path, monkeypatch)
    store.set("telegram.chat_ids", ["777"])

    await service.handle_command("777", "/site", "karlancer")
    assert store.get("marketplace.name") == "karlancer"
    assert "کارلنسر" in api.last_text
    assert any(btn["callback_data"] == "cmd:market" for row in api.last_keyboard for btn in row)

    await service.handle_command("777", "/site", "doesnotexist")
    assert store.get("marketplace.name") == "karlancer"  # unchanged
    assert "ثبت نشده" in api.last_text


async def test_status_lists_every_site(tmp_path, monkeypatch):
    service, api, store = make_service(tmp_path, monkeypatch)
    store.set("telegram.chat_ids", ["777"])
    onboarding.switch_site(store, "ponisha")

    await service.handle_command("777", "/status", "")

    text = api.last_text
    assert "پونیشا" in text and "کارلنسر" in text and "پارس‌کدرز" in text
    assert "(فعال)" in text


async def test_market_flow_writes_the_active_sites_keys(tmp_path, monkeypatch):
    service, api, store = make_service(tmp_path, monkeypatch)
    store.set("telegram.chat_ids", ["777"])
    onboarding.switch_site(store, "karlancer")

    await service.handle_command("777", "/market", "")
    assert "کدام سایت" in api.last_text  # the flow can switch site first
    await service.handle_text("777", "karlancer")

    assert "ورود به سایت" in api.last_text
    await service.handle_text("777", "cookie")  # first choice of the method question
    await service.handle_text("777", "PHPSESSID=karl-cookie")

    assert store.get("credentials.karlancer.cookie") == "PHPSESSID=karl-cookie"
    assert store.get("credentials.parscoders.cookie") is None


# --------------------------------------------------------- html market source
def test_source_reads_only_its_own_cookie(tmp_path, monkeypatch):
    monkeypatch.setenv("BIDPILOT_SETTINGS_FILE", str(tmp_path / "data" / "settings.json"))
    reset_store_cache()
    reset_secret_cache()
    store = get_store()
    save_site_credentials(store, "parscoders", method="cookie", cookie="PHPSESSID=pars")
    save_site_credentials(store, "ponisha", method="cookie", cookie="PHPSESSID=poni")

    from bidpilot.config import AppConfig, get_secrets

    ponisha = create_source("ponisha", {}, AppConfig(), get_secrets())
    parscoders = create_source("parscoders", {}, AppConfig(), get_secrets())

    assert ponisha.cookies()["PHPSESSID"] == "poni"
    assert parscoders.cookies()["PHPSESSID"] == "pars"


def test_unknown_site_listing_falls_back_to_content():
    from bidpilot.sources.html_parser import parse_list_page

    html = """
    <html><body><nav><a href="/about">about</a></nav>
      <section>
        <li><a href="/job/1001">طراحی سایت فروشگاهی با ووکامرس</a></li>
        <li><a href="/job/1002">ربات تلگرام برای فروش بلیت</a></li>
        <li><a href="/job/1003">بهینه‌سازی سرعت یک اپ اندروید</a></li>
      </section>
    </body></html>
    """

    # no selectors at all — the generic link heuristics must still find the cards
    projects = parse_list_page(html, {}, "unknown", "https://example.com")
    titles = [project.title for project in projects]
    assert titles == [
        "طراحی سایت فروشگاهی با ووکامرس",
        "ربات تلگرام برای فروش بلیت",
        "بهینه‌سازی سرعت یک اپ اندروید",
    ]
    assert projects[0].url == "https://example.com/job/1001"


def test_menu_links_alone_do_not_create_projects():
    from bidpilot.sources.html_parser import parse_list_page

    html = '<a href="/page/1">1</a><a href="/page/2">2</a>'
    assert parse_list_page(html, {}, "unknown", "https://example.com") == []


class _FakeHtmlSource:
    """Minimal stand-in for an HTML marketplace (no network in tests)."""

    name = "parscoders"

    async def verify_session(self):
        return True, "ورود تأیید شد."

    async def healthcheck(self):
        return True, "۲۴ پروژه در صفحه اول پیدا شد."

    async def aclose(self):
        return None


async def test_site_check_prints_the_project_count_only_once(tmp_path, monkeypatch):
    from bidpilot import sitecheck
    from bidpilot.settings_store import SettingsStore, reset_store_cache

    monkeypatch.setenv("BIDPILOT_SETTINGS_FILE", str(tmp_path / "settings.json"))
    reset_store_cache()
    store = SettingsStore(str(tmp_path / "settings.json"))
    store.update({"marketplace.name": "parscoders", "credentials.parscoders.method": "cookie"})
    monkeypatch.setattr(sitecheck, "site_credentials", lambda name, store=None: _ReadyCreds())
    monkeypatch.setattr(sitecheck, "create_source", lambda *a, **k: _FakeHtmlSource())

    check = await sitecheck.check_site(store, config_path=str(ROOT / "config" / "config.yaml"))
    message = check.message()

    assert check.ok and check.projects == 24
    assert message.count("پروژه") == 1
    assert message.count("📋") == 1
    assert "📄" not in message


class _ReadyCreds:
    method = "cookie"
    ready = True
    summary = "کوکی ثبت شده"



async def test_http_diagnostics_log_redirects_without_query_values(caplog):
    import logging

    import httpx

    from bidpilot.config import AppConfig, Secrets
    from bidpilot.sources.ponisha.adapter import PonishaSource

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search/projects":
            return httpx.Response(
                302,
                headers={"location": "/dashboard?session=must-not-be-logged"},
                request=request,
            )
        return httpx.Response(
            200,
            text="<html><body>ok</body></html>",
            headers={"content-type": "text/html; charset=utf-8"},
            request=request,
        )

    client = httpx.AsyncClient(
        base_url="https://ponisha.ir",
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )
    source = PonishaSource(
        {"discovery": {"log_extraction_details": True}},
        AppConfig(),
        Secrets(),
        client=client,
    )

    try:
        with caplog.at_level(logging.INFO, logger="bidpilot.sources.html_market"):
            html = await source.get_html(
                "/search/projects?private=query-secret",
                params={"access_token": "parameter-secret"},
            )
    finally:
        await client.aclose()

    assert html == "<html><body>ok</body></html>"
    assert "[HTTP][REQUEST]" in caplog.text
    assert "[HTTP][RESPONSE]" in caplog.text
    assert "status=200" in caplog.text
    assert "/dashboard" in caplog.text
    assert "redirects=" in caplog.text and "302" in caplog.text
    assert "query-secret" not in caplog.text
    assert "parameter-secret" not in caplog.text
    assert "must-not-be-logged" not in caplog.text
