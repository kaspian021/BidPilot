import shutil
from pathlib import Path

from bidpilot.bot import BotService
from bidpilot.config import reset_secret_cache
from bidpilot.onboarding import TONE_CHOICES
from bidpilot.settings_store import SettingsStore, get_store, reset_store_cache

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "123456789:AAHabcdefghijklmnopqrstuvwxyz012345"


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


def message(chat_id: int, text: str) -> dict:
    return {"update_id": 1, "message": {"chat": {"id": chat_id}, "text": text}}


def make_service(tmp_path, monkeypatch) -> tuple[BotService, FakeAPI, SettingsStore]:
    monkeypatch.setenv("BIDPILOT_SETTINGS_FILE", str(tmp_path / "data" / "settings.json"))
    reset_store_cache()
    reset_secret_cache()
    store = get_store()
    api = FakeAPI()
    service = BotService(store, api, config_path=str(ROOT / "config" / "config.yaml"))
    return service, api, store


async def test_first_chat_claims_ownership_and_setup_starts(tmp_path, monkeypatch):
    service, api, store = make_service(tmp_path, monkeypatch)

    await service.handle_update(message(777, "/start"))

    assert store.get_list("telegram.chat_ids") == ["777"]
    assert "خوش آمدی" in api.sent[0][1]
    assert "توکن ربات تلگرام" in api.last_text
    assert api.sent[-1][2] is not None  # cancel keyboard


async def test_other_chats_are_rejected(tmp_path, monkeypatch):
    service, api, store = make_service(tmp_path, monkeypatch)
    store.set("telegram.chat_ids", ["777"])

    await service.handle_update(message(999, "/status"))

    assert "مجاز نیست" in api.last_text
    assert "999" in api.last_text


async def test_setup_answers_are_saved_and_flow_advances(tmp_path, monkeypatch):
    service, api, store = make_service(tmp_path, monkeypatch)
    await service.handle_update(message(777, "/start"))
    assert "توکن ربات تلگرام" in api.last_text

    await service.handle_text("777", TOKEN)
    assert store.get("telegram.bot_token") == TOKEN
    assert "هوش مصنوعی" in api.last_text  # moved to the LLM questions

    await service.handle_text("777", "2")  # gemini
    assert store.get("llm.provider") == "gemini"
    from bidpilot.llm_providers import PRESETS

    assert store.get("llm.model") == PRESETS["gemini"].model
    assert "کلید API" in api.last_text

    await service.handle_text("777", "not-a-real-key-needed")
    assert store.get("llm.api_key") == "not-a-real-key-needed"
    assert store.get("llm.keys.gemini") == "not-a-real-key-needed"


async def test_invalid_answer_is_reported_and_question_repeats(tmp_path, monkeypatch):
    service, api, store = make_service(tmp_path, monkeypatch)
    await service.handle_update(message(777, "/start"))

    await service.handle_text("777", "bad-token")

    assert "فرمت توکن درست نیست" in api.last_text
    assert store.get("telegram.bot_token") is None
    assert "توکن ربات تلگرام" in api.sent[-2][1]


async def test_style_flow_sets_tone_and_instructions(tmp_path, monkeypatch):
    service, api, store = make_service(tmp_path, monkeypatch)
    store.set("telegram.chat_ids", ["777"])

    await service.handle_command("777", "/style")
    assert "لحنی" in api.last_text
    assert api.sent[-1][2]["inline_keyboard"]

    await service.handle_text("777", "3")  # خودمانی و گرم
    assert store.get("proposal.tone") == TONE_CHOICES[2].value
    assert "دستورهای سبک" in api.last_text

    await service.handle_text("777", "همیشه روی تحویل مرحله‌ای تأکید کن")
    assert store.get("proposal.style_instructions") == "همیشه روی تحویل مرحله‌ای تأکید کن"


async def test_choice_callback_sets_live_mode(tmp_path, monkeypatch):
    service, api, store = make_service(tmp_path, monkeypatch)
    store.set("telegram.chat_ids", ["777"])
    await service.handle_command("777", "/mode")

    await service.handle_update(
        {
            "update_id": 2,
            "callback_query": {
                "id": "cb-1",
                "data": "ch:0:1",
                "message": {"chat": {"id": 777}},
            },
        }
    )

    assert "cb-1" in api.answered
    assert store.get("runtime.submit_mode") == "live"


async def test_mode_question_writes_into_effective_config(tmp_path, monkeypatch):
    service, api, store = make_service(tmp_path, monkeypatch)
    store.set("telegram.chat_ids", ["777"])
    await service.handle_command("777", "/mode")
    await service.handle_text("777", "1")

    from bidpilot.config import load_config

    config = load_config(ROOT / "config" / "config.yaml")
    assert config.runtime.live is False


async def test_status_and_settings_mask_secrets(tmp_path, monkeypatch):
    service, api, store = make_service(tmp_path, monkeypatch)
    store.update(
        {
            "telegram.chat_ids": ["777"],
            "llm.api_key": "super-secret-key-value",
            "proposal.tone": "رسمی و کوتاه",
        }
    )

    await service.handle_command("777", "/status")
    assert "Groq" in api.last_text
    assert "رسمی و کوتاه" in api.last_text

    await service.handle_command("777", "/settings")
    assert "super-secret-key-value" not in api.last_text
    assert "…" in api.last_text


async def test_help_and_unknown_command(tmp_path, monkeypatch):
    service, api, store = make_service(tmp_path, monkeypatch)
    store.set("telegram.chat_ids", ["777"])

    await service.handle_command("777", "/help")
    assert "/style" in api.last_text

    await service.handle_command("777", "/nope")
    assert "نمی‌شناسم" in api.last_text


async def test_cancel_stops_the_flow(tmp_path, monkeypatch):
    service, api, store = make_service(tmp_path, monkeypatch)
    await service.handle_update(message(777, "/start"))
    await service.handle_command("777", "/cancel")
    assert "لغو شد" in api.last_text

    await service.handle_text("777", TOKEN)
    assert store.get("telegram.bot_token") is None  # no active flow -> ignored


async def test_preview_generates_a_real_proposal_without_network(tmp_path, monkeypatch):
    workdir = tmp_path / "work"
    workdir.mkdir()
    shutil.copytree(ROOT / "config", workdir / "config")
    monkeypatch.chdir(workdir)
    service, api, store = make_service(tmp_path, monkeypatch)
    store.update({"telegram.chat_ids": ["777"], "marketplace.name": "mock"})

    messages = await service._generate_preview(limit=1)

    assert len(messages) == 1
    assert "پیش‌نمایش پیشنهاد" in messages[0]
    assert "تومان" in messages[0]
    assert Path(workdir / "data").exists()


async def test_skip_command_answers_optional_question(tmp_path, monkeypatch):
    service, api, store = make_service(tmp_path, monkeypatch)
    store.set("telegram.chat_ids", ["777"])

    await service.handle_update(message(777, "/style"))
    await service.handle_update(message(777, "/skip"))  # keep the current tone
    assert "دستورهای سبک" in api.last_text

    await service.handle_update(message(777, "/skip"))  # no style instructions
    assert "حداکثر طول" in api.last_text
    assert store.get("proposal.tone") is None


async def test_full_setup_from_telegram_end_to_end(tmp_path, monkeypatch):
    service, api, store = make_service(tmp_path, monkeypatch)
    await service.handle_update(message(777, "/start"))

    # the login check after setup must not hit the network in a unit test
    from bidpilot import bot as bot_module
    from bidpilot.sitecheck import SiteCheck

    async def fake_check(*args, **kwargs):
        return SiteCheck("parscoders", "پارس‌کدرز", True, "وارد حساب شدیم ✅", projects=3)

    monkeypatch.setattr(bot_module, "check_site", fake_check)

    answers = [
        TOKEN,  # telegram bot token
        "1",  # provider: Groq (first free preset)
        "gsk-free-key",  # API key
        "2",  # marketplace: parscoders (choices are sorted: mock, parscoders)
        "1",  # auth method: username + password
        "ali",  # username
        "secret-pass",  # password
        "manual",  # profile source: the old manual questions
        "علی",  # first name
        "علی رضایی",  # full name
        "برنامه‌نویس بک‌اند",  # role
        "6",  # years of experience
        "Python, Django",  # stack
        "ربات فروشگاهی, بک‌اند اپلیکیشن",  # highlights
        "/skip",  # portfolio (optional)
        "/skip",  # signature (optional)
        "2",  # tone: رسمی و کوتاه
        "روی تحویل مرحله‌ای تأکید کن",  # style instructions
        "1200",  # max chars
        "60",  # pricing: 60% of the employer's budget
        "1",  # submit mode: dry run
    ]
    for answer in answers:
        await service.handle_update(message(777, answer))

    assert store.get("setup.completed") is True
    assert store.get("telegram.bot_token") == TOKEN
    assert store.get("llm.provider") == "groq"
    assert store.get("llm.api_key") == "gsk-free-key"
    assert store.get("marketplace.name") == "parscoders"
    assert store.get("credentials.parscoders.username") == "ali"
    assert store.get("profile.full_name") == "علی رضایی"
    assert store.get("proposal.tone") == TONE_CHOICES[1].value
    assert store.get("proposal.style_instructions") == "روی تحویل مرحله‌ای تأکید کن"
    assert store.get("proposal.max_chars") == 1200
    assert store.get("pricing.bid_percent") == 60
    assert store.get("pricing.ratio") == 0.6
    assert store.get("runtime.submit_mode") == "dry_run"
    assert any("تنظیمات ذخیره شد" in text for _, text, _ in api.sent)
    # after setup the bot proves the login and offers the start button
    assert any("وارد حساب" in text for _, text, _ in api.sent)
    assert "شروع فعالیت" in str(api.sent[-1][2])  # the start button is attached
