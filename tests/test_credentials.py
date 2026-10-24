"""Per-site credentials: every marketplace keeps its own account."""

import pytest

from bidpilot.credentials import (
    SiteCredentials,
    clear_site_credentials,
    parse_cookie_header,
    save_site_credentials,
    site_credentials,
)
from bidpilot.settings_store import SettingsStore


@pytest.fixture()
def store(tmp_path) -> SettingsStore:
    return SettingsStore(tmp_path / "data" / "settings.json")


def test_parse_cookie_header_accepts_header_or_bare_session_id():
    assert parse_cookie_header("PHPSESSID=abc") == {"PHPSESSID": "abc"}
    assert parse_cookie_header("a=1; b=2 ; ") == {"a": "1", "b": "2"}
    assert parse_cookie_header("just-the-id") == {"PHPSESSID": "just-the-id"}
    assert parse_cookie_header("  ") == {}
    assert parse_cookie_header(None) == {}
    assert parse_cookie_header("raw", cookie_name="session") == {"session": "raw"}


def test_each_site_has_its_own_cookie(store):
    save_site_credentials(store, "parscoders", method="cookie", cookie="PHPSESSID=pars")
    save_site_credentials(store, "ponisha", method="cookie", cookie="PHPSESSID=poni")

    pars = site_credentials("parscoders", store)
    poni = site_credentials("ponisha", store)
    karl = site_credentials("karlancer", store)

    assert pars.cookies == {"PHPSESSID": "pars"}
    assert poni.cookies == {"PHPSESSID": "poni"}
    # a site without its own account never inherits another site's cookie
    assert karl.cookies == {}
    assert karl.ready is False
    assert pars.ready is True


def test_summary_never_leaks_the_secret(store):
    save_site_credentials(store, "ponisha", method="cookie", cookie="PHPSESSID=top-secret")
    summary = site_credentials("ponisha", store).summary
    assert "top-secret" not in summary
    assert "کوکی" in summary

    save_site_credentials(store, "karlancer", method="credentials", username="ali", password="pw")
    cred_summary = site_credentials("karlancer", store).summary
    assert "ali" in cred_summary
    assert "pw" not in cred_summary


def test_username_password_site_switches_method_automatically(store):
    store.update(
        {
            "credentials.karlancer.method": "cookie",
            "credentials.karlancer.username": "ali",
            "credentials.karlancer.password": "pw",
        }
    )
    creds = site_credentials("karlancer", store)
    assert creds.method == "credentials"
    assert creds.ready is True


def test_legacy_parscoders_values_still_work(store):
    creds = site_credentials("parscoders", store, legacy={"parscoders_cookie": "PHPSESSID=from-env"})
    assert creds.cookies == {"PHPSESSID": "from-env"}

    # an explicitly stored cookie always wins over the legacy value
    save_site_credentials(store, "parscoders", method="cookie", cookie="PHPSESSID=from-ui")
    creds = site_credentials("parscoders", store, legacy={"parscoders_cookie": "PHPSESSID=from-env"})
    assert creds.cookies == {"PHPSESSID": "from-ui"}


def test_environment_fallback_is_per_site(store, monkeypatch):
    monkeypatch.setenv("KARLANCER_COOKIE", "PHPSESSID=env-karl")
    assert site_credentials("karlancer", store).cookies == {"PHPSESSID": "env-karl"}
    # the other sites are unaffected
    assert site_credentials("ponisha", store).cookies == {}


def test_clear_and_save_roundtrip(store):
    save_site_credentials(store, "ponisha", method="cookie", cookie="PHPSESSID=x")
    clear_site_credentials(store, "ponisha")
    assert store.get("credentials.ponisha.cookie") is None
    assert site_credentials("ponisha", store).ready is False


def test_custom_cookie_name_and_headers(store):
    save_site_credentials(
        store,
        "ponisha",
        method="cookie",
        cookie="sessionid=abc",
        cookie_name="sessionid",
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    creds = site_credentials("ponisha", store)
    assert creds.cookies == {"sessionid": "abc"}
    assert creds.headers == {"X-Requested-With": "XMLHttpRequest"}


def test_unknown_method_falls_back_to_the_site_default(store):
    store.set("credentials.ponisha.method", "magic")
    assert site_credentials("ponisha", store, method_default="cookie").method == "cookie"
    assert site_credentials("ponisha", store, method_default="none").method == "none"


def test_method_none_is_ready_without_any_secret(store):
    save_site_credentials(store, "karlancer", method="none")
    creds = site_credentials("karlancer", store)
    assert creds.ready is True
    assert isinstance(creds, SiteCredentials)
