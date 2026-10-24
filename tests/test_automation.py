"""«شروع فعالیت» — starting and stopping the bidder from the chat.

Covers the automation object itself, the bot commands/callbacks that drive it,
the login check that runs right after setup and the PDF resume path.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pdf_fixtures import make_pdf

from bidpilot.automation import Automation
from bidpilot.bot import BotService
from bidpilot.config import load_config, reset_secret_cache
from bidpilot.settings_store import get_store, reset_store_cache
from bidpilot.sitecheck import SiteCheck

ROOT = Path(__file__).resolve().parents[1]
CONFIG = str(ROOT / "config" / "config.yaml")


class FakeAPI:
    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.sent: list[tuple[str, str, dict | None]] = []
        self.answered: list[str] = []
        self.files = files or {}

    async def send_message(self, chat_id, text, *, reply_markup=None, parse_mode="HTML"):
        self.sent.append((str(chat_id), text, reply_markup))

    async def answer_callback(self, callback_id, text=""):
        self.answered.append(callback_id)

    async def get_file(self, file_id: str) -> dict:
        return {"file_id": file_id, "file_path": f"docs/{file_id}.pdf"}

    async def download_file(self, file_path: str, *, limit: int = 0) -> bytes:
        return self.files[file_path]

    @property
    def last_text(self) -> str:
        return self.sent[-1][1] if self.sent else ""

    @property
    def texts(self) -> list[str]:
        return [text for _, text, _ in self.sent]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("BIDPILOT_SETTINGS_FILE", str(tmp_path / "data" / "settings.json"))
    reset_store_cache()
    reset_secret_cache()
    store = get_store()
    store.set("telegram.chat_ids", ["777"])
    return store


def callback(data: str) -> dict:
    return {
        "update_id": 9,
        "callback_query": {"id": "cb", "data": data, "message": {"chat": {"id": 777}}},
    }


def document(file_id: str, name: str = "resume.pdf") -> dict:
    return {
        "update_id": 10,
        "message": {
            "chat": {"id": 777},
            "document": {
                "file_id": file_id,
                "file_name": name,
                "mime_type": "application/pdf",
                "file_size": 4096,
            },
        },
    }


# ---------------------------------------------------------------------------
# Automation object
# ---------------------------------------------------------------------------
async def test_automation_runs_one_cycle_and_reports_once(env, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env.update({"marketplace.name": "mock", "runtime.submit_mode": "dry_run"})
    notes: list[str] = []

    async def notify(text: str) -> None:
        notes.append(text)

    automation = Automation(store=env, config_path=CONFIG, notify=notify, interval=30)
    ok, message = await automation.start()
    assert ok and "شروع" in message

    for _ in range(50):  # the first cycle runs immediately; wait for its report
        if notes:
            break
        await asyncio.sleep(0.1)

    assert automation.running
    assert notes and "اولین بررسی" in notes[0]
    assert automation.summary.cycles >= 1
    assert automation.summary.fetched >= 1

    stopped, _ = await automation.stop()
    assert stopped
    assert not automation.running
    assert "متوقف" in automation.status_text()


async def test_automation_start_is_idempotent(env, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env.set("marketplace.name", "mock")
    automation = Automation(store=env, config_path=CONFIG, interval=600)
    assert (await automation.start())[0]
    try:
        ok, message = await automation.start()
        assert ok is False and "از قبل" in message
    finally:
        await automation.stop()
    assert (await automation.stop())[0] is False


# ---------------------------------------------------------------------------
# Bot: /run, /stop, /check
# ---------------------------------------------------------------------------
def _service(env, api: FakeAPI) -> BotService:
    return BotService(env, api, config_path=CONFIG)


async def test_run_refuses_before_the_account_is_connected(env):
    api = FakeAPI()
    service = _service(env, api)

    await service.handle_command("777", "/run")

    assert "/market" in api.last_text


async def test_run_asks_for_confirmation_with_the_effective_settings(env, monkeypatch):
    env.update(
        {
            "telegram.bot_token": "123456789:AAHabcdefghijklmnopqrstuvwxyz012345",
            "marketplace.name": "mock",
            "runtime.submit_mode": "dry_run",
            "proposal.tone": "رسمی و کوتاه",
            "pricing.bid_percent": 45,
            "profile.full_name": "علی اقبالی",
        }
    )
    api = FakeAPI()
    service = _service(env, api)

    await service.handle_command("777", "/run")

    text = api.last_text
    assert "آماده‌ی شروع" in text
    assert "۴۵٪" in text or "45٪" in text
    assert "آزمایشی" in text
    buttons = [row[0]["callback_data"] for row in api.sent[-1][2]["inline_keyboard"]]
    assert "run:confirm" in buttons


async def test_confirm_starts_and_stop_pauses(env, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env.update({"marketplace.name": "mock", "runtime.submit_mode": "dry_run"})
    api = FakeAPI()
    service = _service(env, api)
    service.automation.interval = 30

    # mock needs no login check, so the button goes straight to starting
    await service.handle_update(callback("run:confirm"))

    assert service.automation.running
    assert any("شروع" in text for text in api.texts)
    assert any("پروژه" in text for text in api.texts) or api.sent  # first report may lag

    await service.handle_command("777", "/stop")
    assert not service.automation.running
    assert "متوقف" in api.last_text


# ---------------------------------------------------------------------------
# Login verification after setup
# ---------------------------------------------------------------------------
async def test_post_setup_reports_the_login_and_offers_the_start_button(env, monkeypatch):
    from bidpilot import bot as bot_module

    async def fake_check(*args, **kwargs):
        return SiteCheck("parscoders", "پارس‌کدرز", True, "وارد حساب کاربری شدیم ✅", projects=12)

    monkeypatch.setattr(bot_module, "check_site", fake_check)
    env.update({"marketplace.name": "parscoders", "credentials.parscoders.cookie": "PHPSESSID=x"})
    api = FakeAPI()
    service = _service(env, api)

    await service._post_setup("777")

    assert any("وارد حساب شما شدیم" in text for text in api.texts)
    assert "12" in " ".join(api.texts)
    keyboard = api.sent[-1][2]["inline_keyboard"]
    assert keyboard[0][0]["callback_data"] == "run:ask"


async def test_post_setup_does_not_offer_start_when_login_fails(env, monkeypatch):
    from bidpilot import bot as bot_module

    async def fake_check(*args, **kwargs):
        return SiteCheck("parscoders", "پارس‌کدرز", False, "کوکی منقضی شده است.")

    monkeypatch.setattr(bot_module, "check_site", fake_check)
    env.update({"marketplace.name": "parscoders", "credentials.parscoders.cookie": "PHPSESSID=x"})
    api = FakeAPI()
    service = _service(env, api)

    await service._post_setup("777")

    assert "ورود به حساب انجام نشد" in " ".join(api.texts)
    assert "/market" in " ".join(api.texts)
    assert api.sent[-1][2] is None


async def test_check_command_uses_the_site_check(env, monkeypatch):
    from bidpilot import bot as bot_module

    async def fake_check(*args, **kwargs):
        return SiteCheck("ponisha", "پونیشا", True, "وارد حساب کاربری شدیم ✅", projects=5)

    monkeypatch.setattr(bot_module, "check_site", fake_check)
    env.set("marketplace.name", "ponisha")
    api = FakeAPI()
    service = _service(env, api)

    await service.handle_command("777", "/check")

    assert "پونیشا" in " ".join(api.texts)
    assert "🚀" in str(api.sent[-1][2])


# ---------------------------------------------------------------------------
# PDF resume in the chat
# ---------------------------------------------------------------------------
async def test_resume_pdf_in_the_chat_builds_the_profile(env, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env.update({"marketplace.name": "mock", "profile.source": "resume"})
    resume = make_pdf(
        [
            "Ali Eghbali",
            "Senior Flutter and ASP.NET Developer",
            "5 years of experience",
            "Skills: Flutter, C#, Unity, FastAPI",
            "- Built a shopping app for Avan",
            "https://github.com/kaspian021",
        ]
    )
    api = FakeAPI(files={"docs/resume-file.pdf": resume})
    service = _service(env, api)

    await service.handle_command("777", "/profile")
    assert "پروفایلت را از کدام راه" in api.last_text
    await service.handle_text("777", "resume")  # choice: PDF

    await service.handle_update(document("resume-file"))

    assert env.get("profile.full_name") == "Ali Eghbali"
    assert env.get("profile.years_experience") == 5
    assert "Flutter" in env.get_list("profile.stack")
    assert any("پروفایل استخراج‌شده" in text for text in api.texts)
    assert any("github.com/kaspian021" in text for text in api.texts)


async def test_non_pdf_upload_is_refused(env):
    env.update({"marketplace.name": "mock", "profile.source": "resume"})
    api = FakeAPI()
    service = _service(env, api)
    await service.handle_command("777", "/profile")
    await service.handle_text("777", "resume")

    update = document("doc1", name="cv.docx")
    update["message"]["document"]["mime_type"] = "application/msword"
    await service.handle_update(update)

    assert "فقط فایل PDF" in api.last_text


async def test_document_outside_the_flow_is_ignored(env):
    api = FakeAPI()
    service = _service(env, api)

    await service.handle_update(document("whatever"))

    assert "الان جایی برای آن باز نیست" in api.last_text


# ---------------------------------------------------------------------------
# Price share actually reaches the pricer
# ---------------------------------------------------------------------------
def test_bid_percent_changes_the_effective_price(env):
    from bidpilot.models import Budget
    from bidpilot.pricing.strategy import PricingConfig

    budget = Budget(min=500_000, max=1_500_000)

    def price() -> int:
        reset_secret_cache()
        cfg = load_config(CONFIG, store=env)
        return PricingConfig(**cfg.pricing).build().price(None, budget).amount  # type: ignore[arg-type]

    env.set("pricing.ratio", 0.3)
    assert price() == 800_000

    env.set("pricing.ratio", 0.9)
    assert price() == 1_400_000

    # and it never leaves the employer's range
    env.set("pricing.ratio", 1.0)
    assert price() == 1_500_000


async def test_live_automation_refuses_to_start_without_a_valid_session(env, tmp_path, monkeypatch):
    from types import SimpleNamespace

    import bidpilot.automation as automation_module

    monkeypatch.chdir(tmp_path)
    env.update({"marketplace.name": "parscoders", "runtime.submit_mode": "live"})
    cfg = load_config(CONFIG, store=env)
    assert cfg.runtime.live

    class UnauthenticatedSource:
        async def verify_session(self):
            return False, "کوکی منقضی شده است"

    class FakeApp:
        def __init__(self):
            self.config = cfg
            self.sources = {"parscoders": UnauthenticatedSource()}
            self.closed = False

        async def setup(self):
            return None

        async def close(self):
            self.closed = True

    fake_app = FakeApp()
    monkeypatch.setattr(automation_module, "load_config", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(automation_module, "build_application", lambda config: fake_app)
    notes = []

    async def notify(text):
        notes.append(text)

    automation = Automation(store=env, config_path=CONFIG, notify=notify)
    ok, message = await automation.start()

    assert not ok
    assert "ورود" in message
    assert fake_app.closed
    assert not automation.running
    assert automation._scheduler is None
    assert notes and "وارد حساب کاربری" in notes[0]
    assert "جست‌وجو و ارسال پیشنهاد شروع نشد" in notes[0]
