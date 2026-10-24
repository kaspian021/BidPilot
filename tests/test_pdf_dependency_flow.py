"""What happened on Windows: no pypdf → the bot said «pip install pypdf» and then
claimed the profile was saved «از رزومه» even though nothing was read.

These tests pin the fix: install on the spot, keep the queued file, and never
claim success when the import failed.
"""

from __future__ import annotations

from pathlib import Path

from bidpilot import deps
from bidpilot.bot import BotService
from bidpilot.config import reset_secret_cache
from bidpilot.settings_store import get_store, reset_store_cache

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "123456789:AAHabcdefghijklmnopqrstuvwxyz012345"


class FakeAPI:
    """Same shape as the bot's transport; only what these tests touch."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict | None]] = []

    async def send_message(self, chat_id, text, *, reply_markup=None, parse_mode="HTML"):
        self.sent.append((str(chat_id), text, reply_markup))

    async def answer_callback(self, callback_id, text=""):
        return None

    @property
    def last_text(self) -> str:
        return self.sent[-1][1] if self.sent else ""


def make_service(tmp_path, monkeypatch, *, pypdf: bool = False):
    monkeypatch.setenv("BIDPILOT_SETTINGS_FILE", str(tmp_path / "data" / "settings.json"))
    reset_store_cache()
    reset_secret_cache()
    store = get_store()
    api = FakeAPI()
    service = BotService(store, api, config_path=str(ROOT / "config" / "config.yaml"))
    monkeypatch.setattr(deps, "has", lambda module: pypdf if module == "pypdf" else True)
    return service, api, store


def test_deps_command_shows_the_package_and_an_install_button(tmp_path, monkeypatch):
    import asyncio

    service, api, store = make_service(tmp_path, monkeypatch, pypdf=False)

    asyncio.run(service.handle_command("777", "/deps"))

    assert "pypdf" in api.last_text
    buttons = str(api.sent[-1][2])
    assert "dep:pypdf" in buttons


def test_install_button_runs_the_installer(tmp_path, monkeypatch):
    import asyncio

    service, api, store = make_service(tmp_path, monkeypatch, pypdf=False)
    installed: list[str] = []

    def fake_ensure(module, **kwargs):
        installed.append(module)
        return True, ""

    monkeypatch.setattr(deps, "ensure", fake_ensure)

    asyncio.run(service._install_dep("777", "pypdf"))

    assert installed == ["pypdf"]
    assert "✅" in api.sent[-2][1] or "✅" in api.sent[-1][1]


def test_pdf_upload_installs_pypdf_before_reading(tmp_path, monkeypatch):
    import asyncio

    service, api, store = make_service(tmp_path, monkeypatch, pypdf=False)
    monkeypatch.setattr(deps, "ensure", lambda module, **kw: (True, ""))

    ok = asyncio.run(service._ensure_pdf_reader("777"))

    assert ok is True
    texts = "\n".join(text for _, text, _ in api.sent)
    assert "نصبش می‌کنم" in texts
    assert "✅ نصب شد" in texts


def test_failed_install_shows_a_command_that_works_in_this_venv(tmp_path, monkeypatch):
    import asyncio
    import sys

    service, api, store = make_service(tmp_path, monkeypatch, pypdf=False)
    monkeypatch.setattr(deps, "ensure", lambda module, **kw: (False, "no network"))

    ok = asyncio.run(service._ensure_pdf_reader("777"))

    assert ok is False
    assert sys.executable in api.last_text
    assert store.get("profile.import_error")
    assert "ورود دستی" in api.last_text


def test_failed_import_does_not_claim_the_profile_was_saved(tmp_path, monkeypatch):
    """Exactly the reported log: unreadable PDF, then «✅ ذخیره شد … (از رزومه)»."""
    import asyncio

    service, api, store = make_service(tmp_path, monkeypatch, pypdf=True)
    store.update(
        {
            "telegram.chat_ids": ["777"],
            "profile.full_name": "Ali Eghbali",
            "profile.source": "resume",
            "profile.import_pending": {"type": "file", "value": str(tmp_path / "nope.pdf")},
        }
    )

    asyncio.run(service.handle_command("777", "/profile"))
    asyncio.run(service.handle_text("777", "1"))  # رزومه PDF
    bogus = tmp_path / "resume.pdf"
    bogus.write_bytes(b"%PDF-1.4 this is not a pdf")
    asyncio.run(service.handle_text("777", str(bogus)))

    texts = [text for _, text, _ in api.sent]
    assert any("کامل نشد" in text for text in texts), texts[-2:]
    assert not any("ذخیره شد" in text for text in texts)
    assert store.get("profile.import_error")
    # the queue survives so the same file can be retried after fixing the cause
    assert store.get("profile.import_pending")


def test_status_no_longer_says_the_resume_was_read(tmp_path, monkeypatch):

    from bidpilot.onboarding import status_text

    service, api, store = make_service(tmp_path, monkeypatch, pypdf=True)
    store.update(
        {
            "profile.full_name": "Ali Eghbali",
            "profile.source": "resume",
            "profile.import_error": "pypdf نصب نیست",
        }
    )

    text = status_text(store)

    assert "خوانده نشد" in text
    assert "از رزومه" not in text

    # a real import makes it truthful again
    store.update({"profile.imported_at": "2026-10-24T12:00:00+00:00"})
    store.delete("profile.import_error")
    assert "از رزومه" in status_text(store)


def test_help_mentions_the_deps_command(tmp_path, monkeypatch):

    from bidpilot.bot import HELP_TEXT

    assert "/deps" in HELP_TEXT
