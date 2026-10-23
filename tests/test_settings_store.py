import json
import os
import stat

import pytest

from bidpilot.settings_store import (
    SettingsError,
    SettingsStore,
    dot_get,
    get_store,
    is_secret_key,
    mask_value,
    parse_chat_ids,
    parse_list_answer,
    reset_store_cache,
)


def test_dotted_get_nested():
    data = {"telegram": {"bot_token": "x"}, "profile": {"stack": ["Python"]}}
    assert dot_get(data, "telegram.bot_token") == "x"
    assert dot_get(data, "profile.stack") == ["Python"]
    assert dot_get(data, "missing.key", "fallback") == "fallback"


def test_roundtrip_and_persistence(tmp_path):
    path = tmp_path / "nested" / "settings.json"
    store = SettingsStore(path)
    store.set("telegram.bot_token", "123:abc")
    store.update({"proposal.tone": "رسمی", "profile.stack": ["Python", "Django"]})

    assert store.get("proposal.tone") == "رسمی"
    assert store.get("profile.stack") == ["Python", "Django"]
    assert store.get_int("profile.years_experience") is None
    assert store.get_list("profile.stack") == ["Python", "Django"]

    reopened = SettingsStore(path)
    assert reopened.get("telegram.bot_token") == "123:abc"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["values"]["telegram"]["bot_token"] == "123:abc"


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_secret_file_is_private(tmp_path):
    path = tmp_path / "data" / "settings.json"
    store = SettingsStore(path)
    store.set("llm.api_key", "secret-value")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_delete_and_empty_values(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.update({"a.b": "1", "a.c": "2"})
    assert store.delete("a.b") is True
    assert store.delete("a.b") is False
    store.set("a.c", "")
    assert store.get("a.c") is None
    assert store.values() == {"a": {}} or store.values() == {}


def test_secret_detection_and_masking():
    assert is_secret_key("telegram.bot_token")
    assert is_secret_key("llm.api_key")
    assert is_secret_key("credentials.parscoders.password")
    assert is_secret_key("llm.keys.groq")
    assert not is_secret_key("proposal.tone")

    assert mask_value("1234567890abcdef") == "1234…cdef"
    assert mask_value("short") == "••••"
    assert mask_value("") == "—"


def test_masked_items_never_reveal_secrets(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.update({"telegram.bot_token": "123456:ABCDEFGHIJKLMNOP", "proposal.tone": "رسمی"})
    rows = dict(store.masked_items())
    assert "ABCDEFGHIJKLMNOP" not in rows["telegram.bot_token"]
    assert rows["proposal.tone"] == "رسمی"


def test_invalid_key_is_rejected(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    with pytest.raises(SettingsError):
        store.set("bad key!", "x")


def test_get_store_caches_by_path(tmp_path, monkeypatch):
    monkeypatch.setenv("BIDPILOT_SETTINGS_FILE", str(tmp_path / "settings.json"))
    reset_store_cache()
    first = get_store()
    assert first is get_store()
    first.set("profile.first_name", "سارا")
    assert get_store().get("profile.first_name") == "سارا"
    reset_store_cache()


def test_parse_helpers():
    assert parse_list_answer("Python، Django, React;FastAPI") == [
        "Python",
        "Django",
        "React",
        "FastAPI",
    ]
    assert parse_chat_ids("۱۲۳۴۵, -100987; abc") == ["12345", "-100987"]
