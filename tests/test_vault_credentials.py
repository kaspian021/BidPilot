"""Encrypted credential vault + verified-login persistence."""

import json
import os
import stat

import pytest

from bidpilot.credentials import (
    discard_unverified_password,
    mark_credentials_verified,
    save_site_credentials,
    save_verified_site_credentials,
    site_credentials,
)
from bidpilot.settings_store import SettingsStore


def make_store(tmp_path) -> SettingsStore:
    store = SettingsStore(tmp_path / "data" / "settings.json")
    store.set("telegram.chat_ids", ["100", "200"])
    return store


def test_password_is_encrypted_at_rest_but_plain_in_memory(tmp_path):
    store = make_store(tmp_path)
    save_site_credentials(store, "parscoders", method="credentials", username="ali", password="S3cret!pw")

    raw = (tmp_path / "data" / "settings.json").read_text(encoding="utf-8")
    assert "S3cret!pw" not in raw
    assert "enc:v1:" in raw
    assert (tmp_path / "data" / ".vault.key").exists()

    reopened = SettingsStore(tmp_path / "data" / "settings.json")
    creds = site_credentials("parscoders", reopened)
    assert creds.password == "S3cret!pw"
    assert creds.username == "ali"


def test_legacy_plaintext_store_is_migrated_on_first_read(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"values": {"credentials": {"ponisha": {"method": "credentials", "username": "sara", "password": "old-plain"}}}}),
        encoding="utf-8",
    )
    store = SettingsStore(path)
    assert store.get("credentials.ponisha.password") == "old-plain"
    assert "old-plain" not in path.read_text(encoding="utf-8")


def test_verified_login_is_remembered_and_reused(tmp_path):
    store = make_store(tmp_path)
    save_verified_site_credentials(
        store, "ponisha", chat_id="200", method="credentials", username="sara", password="pw-12345"
    )
    creds = site_credentials("ponisha", store, chat_id="200")
    assert creds.verified_ready is True
    assert creds.verified_method == "credentials"
    assert creds.password == "pw-12345"  # decrypted transparently for the runner
    assert "ورود تأییدشده" in creds.summary
    # and it is still ciphertext on disk
    assert "pw-12345" not in (tmp_path / "data" / "settings.json").read_text(encoding="utf-8")


def test_changing_the_password_invalidates_the_verification(tmp_path):
    store = make_store(tmp_path)
    save_verified_site_credentials(store, "parscoders", method="credentials", username="ali", password="pw1")
    assert site_credentials("parscoders", store).verified_ready

    save_site_credentials(store, "parscoders", password="pw2-different")
    creds = site_credentials("parscoders", store)
    assert creds.password == "pw2-different"
    assert creds.verified is False and not creds.verified_ready


def test_failed_login_password_is_discarded_not_kept(tmp_path):
    store = make_store(tmp_path)
    save_site_credentials(store, "karlancer", method="credentials", username="ali", password="wrong-pw")
    assert discard_unverified_password(store, "karlancer") is True
    creds = site_credentials("karlancer", store)
    assert creds.password is None
    assert creds.username == "ali"  # kept so the retry only re-asks the password
    assert creds.ready is False


def test_verified_password_survives_the_discard(tmp_path):
    store = make_store(tmp_path)
    save_verified_site_credentials(store, "karlancer", method="credentials", username="ali", password="good-pw")
    assert discard_unverified_password(store, "karlancer") is False
    assert site_credentials("karlancer", store).password == "good-pw"


def test_mark_verified_for_a_per_user_account(tmp_path):
    store = make_store(tmp_path)
    store.update(
        {
            "users.200.credentials.ponisha.method": "credentials",
            "users.200.credentials.ponisha.username": "sara",
            "users.200.credentials.ponisha.password": "pw",
        }
    )
    assert not site_credentials("ponisha", store, chat_id="200").verified
    mark_credentials_verified(store, "ponisha", chat_id="200", method="credentials")
    creds = site_credentials("ponisha", store, chat_id="200")
    assert creds.verified_ready and creds.verified_at


def test_env_vault_key_wins_and_no_key_file_is_created(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("BIDPILOT_VAULT_KEY", Fernet.generate_key().decode())
    store = make_store(tmp_path)
    save_site_credentials(store, "parscoders", method="credentials", username="ali", password="pw-env")

    assert not (tmp_path / "data" / ".vault.key").exists()
    assert site_credentials("parscoders", SettingsStore(tmp_path / "data" / "settings.json")).password == "pw-env"


def test_wrong_vault_key_fails_loudly_instead_of_using_ciphertext(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    from bidpilot.settings_store import SettingsError

    store = make_store(tmp_path)
    save_site_credentials(store, "parscoders", method="credentials", username="ali", password="pw-x")

    monkeypatch.setenv("BIDPILOT_VAULT_KEY", Fernet.generate_key().decode())
    with pytest.raises(SettingsError):
        SettingsStore(tmp_path / "data" / "settings.json").get("credentials.parscoders.password")


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_vault_key_file_is_private(tmp_path):
    store = make_store(tmp_path)
    save_site_credentials(store, "parscoders", method="credentials", username="ali", password="pw-mode")
    key_path = tmp_path / "data" / ".vault.key"
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
