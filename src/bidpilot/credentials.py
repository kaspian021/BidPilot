"""Per-site credentials — every marketplace keeps its own account.

BidPilot supports several freelance sites at once (only one is *active* at a
time, switchable from Telegram with ``/site``). Each site therefore needs its
own login material, stored under a stable, dotted key:

    credentials.<site>.method     "cookie" | "credentials" | "none"
    credentials.<site>.cookie     raw cookie header, e.g. "PHPSESSID=abc"
    credentials.<site>.username
    credentials.<site>.password
    credentials.<site>.headers    extra HTTP headers (dict)

Nothing here is site-specific: adding a marketplace never means touching this
module. Legacy ``.env`` values (``PARSOCODERS_COOKIE`` …) are still honoured as
a fallback so existing installs keep working.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .settings_store import SettingsStore, get_store

DEFAULT_COOKIE_NAME = "PHPSESSID"
METHODS = ("cookie", "credentials", "none")


def parse_cookie_header(raw: str | None, cookie_name: str = DEFAULT_COOKIE_NAME) -> dict[str, str]:
    """Parse a browser cookie header (``"a=1; b=2"``) or a bare session id."""
    if not raw:
        return {}
    raw = str(raw).strip()
    if not raw:
        return {}
    if "=" not in raw:
        return {cookie_name: raw}
    cookies: dict[str, str] = {}
    for chunk in raw.split(";"):
        if "=" in chunk:
            key, _, value = chunk.partition("=")
            key = key.strip()
            if key:
                cookies[key] = value.strip()
    return cookies


@dataclass(frozen=True)
class SiteCredentials:
    """Login material for one marketplace."""

    site: str
    method: str = "cookie"
    cookie: str | None = None
    username: str | None = None
    password: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    cookie_name: str = DEFAULT_COOKIE_NAME

    @property
    def cookies(self) -> dict[str, str]:
        return parse_cookie_header(self.cookie, self.cookie_name)

    @property
    def ready(self) -> bool:
        """True when the robot can authenticate with what we have."""
        if self.method == "none":
            return True
        if self.method == "credentials":
            return bool(self.username and self.password)
        return bool(self.cookie)

    @property
    def summary(self) -> str:
        """Short, secret-free description for /sites and /status."""
        if self.method == "none":
            return "بدون نیاز به ورود"
        if self.method == "credentials":
            if self.username and self.password:
                return f"نام کاربری: {self.username} ✅"
            return "نام کاربری/رمز ثبت نشده ❌"
        if self.cookie:
            return "کوکی ثبت شده ✅"
        return "کوکی ثبت نشده ❌"

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "site": self.site,
            "method": self.method,
            "ready": self.ready,
            "username": self.username,
            "has_cookie": bool(self.cookie),
        }


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def site_credentials(
    site: str,
    store: SettingsStore | None = None,
    *,
    legacy: Mapping[str, Any] | None = None,
    method_default: str = "cookie",
) -> SiteCredentials:
    """Resolve credentials for ``site`` from the store (+ optional legacy values)."""
    store = store or get_store()
    site = str(site).strip().lower()
    prefix = f"credentials.{site}"

    method = _clean(store.get(f"{prefix}.method")) or method_default
    cookie = _clean(store.get(f"{prefix}.cookie"))
    username = _clean(store.get(f"{prefix}.username"))
    password = _clean(store.get(f"{prefix}.password"))
    headers = store.get(f"{prefix}.headers") or {}
    cookie_name = _clean(store.get(f"{prefix}.cookie_name")) or DEFAULT_COOKIE_NAME

    # legacy: .env / old single-site settings
    legacy = legacy or {}
    if site == "parscoders":
        cookie = cookie or _clean(_legacy_value(legacy, "parscoders_cookie"))
        username = username or _clean(_legacy_value(legacy, "parscoders_username"))
        password = password or _clean(_legacy_value(legacy, "parscoders_password"))

    # environment fallback, e.g. PONISHA_COOKIE / KARLANCER_COOKIE
    env_prefix = site.upper().replace("-", "_")
    cookie = cookie or _clean(os.environ.get(f"{env_prefix}_COOKIE"))
    username = username or _clean(os.environ.get(f"{env_prefix}_USERNAME"))
    password = password or _clean(os.environ.get(f"{env_prefix}_PASSWORD"))

    if method not in METHODS:
        method = method_default
    if method == "cookie" and not cookie and username and password:
        method = "credentials"
    return SiteCredentials(
        site=site,
        method=method,
        cookie=cookie,
        username=username,
        password=password,
        headers=dict(headers) if isinstance(headers, Mapping) else {},
        cookie_name=cookie_name,
    )


def _legacy_value(legacy: Mapping[str, Any], key: str) -> Any:
    value = legacy.get(key)
    if value is None:
        return None
    # pydantic BaseModel is also a Mapping-ish; support attribute access
    return value


def save_site_credentials(
    store: SettingsStore, site: str, *, method: str | None = None, **values: Any
) -> None:
    """Persist one or more credential fields for a site in a single atomic write."""
    site = str(site).strip().lower()
    updates: dict[str, Any] = {}
    if method:
        updates[f"credentials.{site}.method"] = method
    for key in ("cookie", "username", "password", "headers", "cookie_name"):
        if key in values:
            updates[f"credentials.{site}.{key}"] = values[key]
    if updates:
        store.update(updates)


def clear_site_credentials(store: SettingsStore, site: str, fields: tuple[str, ...] = ("cookie",)) -> None:
    for key in fields:
        store.delete(f"credentials.{site}.{key}")


__all__ = [
    "DEFAULT_COOKIE_NAME",
    "METHODS",
    "SiteCredentials",
    "clear_site_credentials",
    "parse_cookie_header",
    "save_site_credentials",
    "site_credentials",
]
