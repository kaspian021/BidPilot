"""Per-site credentials — every marketplace keeps its own account.

BidPilot supports several freelance sites at once (only one is *active* at a
time, switchable from Telegram with ``/site``). Each site therefore needs its
own login material, stored under a stable, dotted key:

    credentials.<site>.method     "cookie" | "credentials" | "none"
    credentials.<site>.cookie     raw cookie header, e.g. "PHPSESSID=abc"
    credentials.<site>.username
    credentials.<site>.password
    credentials.<site>.headers    extra HTTP headers (dict)
    credentials.<site>.verified   login was live-tested successfully
    credentials.<site>.verified_at / .verified_method

All secret values are encrypted at rest by the settings store
(:mod:`bidpilot.vault`); this module only ever sees plain values in memory.
A stored password is only *kept* as a verified credential after a real login
test succeeds — see :func:`mark_credentials_verified` and the bot flow that
discards an unverified password when the test fails.  Once verified, the
runner reuses the credential and never asks for the login method again.

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
    #: a real login test (verify_session / check_site) proved these work
    verified: bool = False
    verified_at: str | None = None
    verified_method: str | None = None

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
    def verified_ready(self) -> bool:
        """Stored *and* proven by a live login test — safe to start without asking."""
        return self.ready and self.verified

    @property
    def summary(self) -> str:
        """Short, secret-free description for /sites and /status."""
        if self.method == "none":
            return "بدون نیاز به ورود"
        mark = " (ورود تأییدشده 🔐)" if self.verified else ""
        if self.method == "credentials":
            if self.username and self.password:
                return f"نام کاربری: {self.username} ✅{mark}"
            return "نام کاربری/رمز ثبت نشده ❌"
        if self.cookie:
            return f"کوکی ثبت شده ✅{mark}"
        return "کوکی ثبت نشده ❌"

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "site": self.site,
            "method": self.method,
            "ready": self.ready,
            "username": self.username,
            "has_cookie": bool(self.cookie),
            "verified": self.verified,
        }


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def is_owner_chat(store: SettingsStore, chat_id: str | int | None) -> bool:
    """The operator's own chat: the first allowed chat (or a configured admin).

    Only the owner may fall back to the machine-wide ``credentials.<site>.*`` keys.
    Everybody else must use the account they connected themselves — otherwise a
    second user without a cookie would silently bid from the owner's marketplace
    account.
    """
    if chat_id is None or not str(chat_id).strip():
        return True
    cid = str(chat_id).strip()
    try:
        allowed = [str(c) for c in store.get_list("telegram.chat_ids")]
    except Exception:  # noqa: BLE001
        allowed = []
    if not allowed or allowed[0] == cid:
        return True
    try:
        from .admin import is_admin

        return bool(is_admin(cid, store))
    except Exception:  # noqa: BLE001
        return False


def site_credentials(
    site: str,
    store: SettingsStore | None = None,
    *,
    legacy: Mapping[str, Any] | None = None,
    method_default: str = "cookie",
    chat_id: str | int | None = None,
) -> SiteCredentials:
    """Resolve credentials for ``site`` from the store (+ optional legacy values).

    ``chat_id`` (or, failing that, the ambient per-user context) selects the user's
    own account stored under ``users.<chat>.credentials.<site>.*``.
    """
    store = store or get_store()
    site = str(site).strip().lower()
    chat = str(chat_id).strip() if chat_id is not None and str(chat_id).strip() else None
    if chat is None:
        try:
            from .per_user import current_chat_id as _ctx_chat

            chat = _ctx_chat.get()
        except Exception:  # noqa: BLE001
            chat = None
    if chat:
        per_prefix = f"users.{chat}.credentials.{site}"
        own_method = _clean(store.get(f"{per_prefix}.method"))
        own_cookie = _clean(store.get(f"{per_prefix}.cookie"))
        own_user = _clean(store.get(f"{per_prefix}.username"))
        own_password = _clean(store.get(f"{per_prefix}.password"))
        if own_method or own_cookie or own_user:
            method = own_method or method_default
            headers = store.get(f"{per_prefix}.headers") or {}
            cookie_name = _clean(store.get(f"{per_prefix}.cookie_name")) or DEFAULT_COOKIE_NAME
            if method not in METHODS:
                method = method_default
            if method == "cookie" and not own_cookie and own_user and own_password:
                method = "credentials"
            return SiteCredentials(
                site=site,
                method=method,
                cookie=own_cookie,
                username=own_user,
                password=own_password,
                headers=dict(headers) if isinstance(headers, Mapping) else {},
                cookie_name=cookie_name,
                verified=bool(store.get(f"{per_prefix}.verified")),
                verified_at=_clean(store.get(f"{per_prefix}.verified_at")),
                verified_method=_clean(store.get(f"{per_prefix}.verified_method")),
            )
        if not is_owner_chat(store, chat):
            # never borrow somebody else's login: report "no account yet"
            return SiteCredentials(site=site, method=method_default if method_default in METHODS else "cookie")
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
        verified=bool(store.get(f"{prefix}.verified")),
        verified_at=_clean(store.get(f"{prefix}.verified_at")),
        verified_method=_clean(store.get(f"{prefix}.verified_method")),
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
        # any change to login material invalidates the previous live test
        for stale in ("verified", "verified_at", "verified_method"):
            updates[f"credentials.{site}.{stale}"] = None
        store.update(updates)


def clear_site_credentials(store: SettingsStore, site: str, fields: tuple[str, ...] = ("cookie",)) -> None:
    for key in fields:
        store.delete(f"credentials.{site}.{key}")
    for stale in ("verified", "verified_at", "verified_method"):
        store.delete(f"credentials.{site}.{stale}")


# --------------------------------------------------------------- verification
def _verified_prefixes(store: SettingsStore, site: str, chat_id: str | int | None) -> list[str]:
    """Store prefixes that may hold this chat's account for ``site`` (own first)."""
    site = str(site).strip().lower()
    prefixes: list[str] = []
    if chat_id is not None and str(chat_id).strip():
        per = f"users.{str(chat_id).strip()}.credentials.{site}"
        if any(store.get(f"{per}.{f}") for f in ("method", "cookie", "username", "password")):
            prefixes.append(per)
        if not is_owner_chat(store, chat_id):
            return prefixes
    prefixes.append(f"credentials.{site}")
    return prefixes


def mark_credentials_verified(
    store: SettingsStore, site: str, *, chat_id: str | int | None = None, method: str | None = None
) -> None:
    """Record that a live login test just succeeded for the stored credential.

    From this point the runner reuses the (encrypted) credential and the user
    is never asked for the login method / password again, until the material
    itself changes (which invalidates the mark) or the session really dies.
    """
    from datetime import datetime, timezone

    prefixes = _verified_prefixes(store, site, chat_id)
    if not prefixes:
        return
    prefix = prefixes[0]
    creds = site_credentials(site, store, chat_id=chat_id)
    updates = {
        f"{prefix}.verified": True,
        f"{prefix}.verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        f"{prefix}.verified_method": method or creds.method,
    }
    store.update(updates)


def invalidate_credentials_verification(
    store: SettingsStore, site: str, *, chat_id: str | int | None = None
) -> None:
    for prefix in _verified_prefixes(store, site, chat_id):
        store.update(
            {
                f"{prefix}.verified": None,
                f"{prefix}.verified_at": None,
                f"{prefix}.verified_method": None,
            }
        )


def discard_unverified_password(
    store: SettingsStore, site: str, *, chat_id: str | int | None = None
) -> bool:
    """Drop a password whose login test failed (username is kept for a retry).

    Implements "we only *store* the password once we have proved it works":
    an unproven password never stays on disk after its failed test.
    """
    removed = False
    for prefix in _verified_prefixes(store, site, chat_id):
        if store.get(f"{prefix}.verified"):
            continue
        if store.get(f"{prefix}.password"):
            store.delete(f"{prefix}.password")
            removed = True
    return removed


def save_verified_site_credentials(
    store: SettingsStore,
    site: str,
    *,
    chat_id: str | int | None = None,
    method: str | None = None,
    **values: Any,
) -> None:
    """Persist login material *together with* its successful live-test mark.

    Values land encrypted at rest through the settings store; callers use this
    only after ``verify_session``/``check_site`` has proved the login works.
    """
    from datetime import datetime, timezone

    site = str(site).strip().lower()
    if chat_id is not None and str(chat_id).strip() and not is_owner_chat(store, chat_id):
        prefix = f"users.{str(chat_id).strip()}.credentials.{site}"
    elif chat_id is not None and str(chat_id).strip():
        # owner: keep the per-user copy when one exists, else the global account
        per = f"users.{str(chat_id).strip()}.credentials.{site}"
        prefix = per if any(store.get(f"{per}.{f}") for f in ("method", "cookie", "username")) else f"credentials.{site}"
    else:
        prefix = f"credentials.{site}"
    updates: dict[str, Any] = {}
    if method:
        updates[f"{prefix}.method"] = method
    for key in ("cookie", "username", "password", "headers", "cookie_name"):
        if key in values:
            updates[f"{prefix}.{key}"] = values[key]
    updates[f"{prefix}.verified"] = True
    updates[f"{prefix}.verified_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    updates[f"{prefix}.verified_method"] = method or values.get("method") or store.get(f"{prefix}.method") or "credentials"
    store.update(updates)


__all__ = [
    "DEFAULT_COOKIE_NAME",
    "METHODS",
    "SiteCredentials",
    "clear_site_credentials",
    "discard_unverified_password",
    "invalidate_credentials_verification",
    "is_owner_chat",
    "mark_credentials_verified",
    "parse_cookie_header",
    "save_site_credentials",
    "save_verified_site_credentials",
    "site_credentials",
]
