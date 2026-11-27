"""Bot flow: username/password is kept only after a *successful* login test."""

from pathlib import Path

from bidpilot import bot as bot_module
from bidpilot.bot import BotService, Flow
from bidpilot.config import reset_secret_cache
from bidpilot.credentials import save_site_credentials, site_credentials
from bidpilot.settings_store import get_store, reset_store_cache
from bidpilot.sitecheck import SiteCheck

ROOT = Path(__file__).resolve().parents[1]


class FakeAPI:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict | None]] = []

    async def send_message(self, chat_id, text, *, reply_markup=None, parse_mode="HTML"):
        self.sent.append((str(chat_id), text, reply_markup))

    @property
    def texts(self) -> list[str]:
        return [text for _chat, text, _kb in self.sent]


def make_service(tmp_path, monkeypatch):
    monkeypatch.setenv("BIDPILOT_SETTINGS_FILE", str(tmp_path / "data" / "settings.json"))
    reset_store_cache()
    reset_secret_cache()
    store = get_store()
    store.set("telegram.chat_ids", ["777"])
    api = FakeAPI()
    return BotService(store, api, config_path=str(ROOT / "config" / "config.yaml")), api, store


async def test_successful_password_login_is_verified_and_kept_encrypted(tmp_path, monkeypatch):
    service, api, store = make_service(tmp_path, monkeypatch)
    store.set("marketplace.name", "parscoders")
    save_site_credentials(store, "parscoders", method="credentials", username="ali", password="pw-secret")

    async def fake_check(*args, **kwargs):
        return SiteCheck("parscoders", "پارس‌کدرز", True, "وارد حساب کاربری شدیم ✅", method="credentials", projects=3)

    monkeypatch.setattr(bot_module, "check_site", fake_check)
    await service._post_setup("777")

    creds = site_credentials("parscoders", store)
    assert creds.verified_ready is True
    assert creds.password == "pw-secret"  # runner can reuse it without asking again
    assert any("رمزنگاری‌شده" in text for text in api.texts)
    raw = (tmp_path / "data" / "settings.json").read_text(encoding="utf-8")
    assert "pw-secret" not in raw


async def test_failed_password_login_is_not_kept(tmp_path, monkeypatch):
    service, api, store = make_service(tmp_path, monkeypatch)
    store.set("marketplace.name", "parscoders")
    save_site_credentials(store, "parscoders", method="credentials", username="ali", password="wrong-pw")

    async def fake_check(*args, **kwargs):
        return SiteCheck("parscoders", "پارس‌کدرز", False, "رمز اشتباه است.", method="credentials")

    monkeypatch.setattr(bot_module, "check_site", fake_check)
    await service._post_setup("777")

    creds = site_credentials("parscoders", store)
    assert creds.password is None
    assert creds.username == "ali"
    assert any("نگه داشته نشد" in text for text in api.texts)


async def test_step3_onboarding_finish_also_tests_the_login(tmp_path, monkeypatch):
    """The main per-user onboarding path must not skip the login test."""
    service, api, store = make_service(tmp_path, monkeypatch)
    store.set("marketplace.name", "parscoders")
    save_site_credentials(store, "parscoders", method="credentials", username="ali", password="pw-secret")
    calls = []

    async def fake_check(*args, **kwargs):
        calls.append(kwargs)
        return SiteCheck("parscoders", "پارس‌کدرز", True, "وارد حساب کاربری شدیم ✅", method="credentials", projects=3)

    monkeypatch.setattr(bot_module, "check_site", fake_check)
    await service._finish_flow("777", Flow(name="user_market_style_pricing"))

    assert calls, "step-3 finish never tested the marketplace login"
    assert site_credentials("parscoders", store).verified_ready is True
    assert any("رمزنگاری‌شده" in text for text in api.texts)
