"""Which marketplaces a user runs — and how many their plan allows at once.

A user picks one or more sites (ParsCoders, Ponisha, Karlancer). The plan's
``max_active_sites`` caps how many of them run *simultaneously*; the rest stay
selected but paused, and start by themselves the moment the plan grows (payment,
referral reward). The selection is stored per chat, the account (cookie / login)
of every site is stored per chat *and* per site, see :mod:`bidpilot.credentials`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .credentials import is_owner_chat, site_credentials
from .per_user import get_user, set_user
from .settings_store import SettingsStore
from .subscriptions.entitlements import Entitlements

DEFAULT_SITE = "parscoders"


def registered_sites(*, include_mock: bool = True) -> list[str]:
    from .sources import available_sources

    names = list(available_sources())
    return names if include_mock else [n for n in names if n != "mock"]


def user_sites(store: SettingsStore, chat_id: str | int) -> list[str]:
    """The user's selected marketplaces, in priority order (never empty)."""
    known = set(registered_sites())
    raw = get_user(store, chat_id, "marketplace.names")
    names = [str(n).strip().lower() for n in raw] if isinstance(raw, (list, tuple)) else []
    if not names:
        single = get_user(store, chat_id, "marketplace.name")
        if single:
            names = [str(single).strip().lower()]
    if not names and is_owner_chat(store, chat_id):
        legacy = store.get("marketplace.name")
        if legacy:
            names = [str(legacy).strip().lower()]
    out: list[str] = []
    for name in names:
        if name in known and name not in out:
            out.append(name)
    return out or [DEFAULT_SITE]


def set_user_sites(store: SettingsStore, chat_id: str | int, names: list[str]) -> list[str]:
    """Persist the selection; the first site is also the user's primary site."""
    known = set(registered_sites())
    clean: list[str] = []
    for name in names:
        n = str(name).strip().lower()
        if n in known and n not in clean:
            clean.append(n)
    if not clean:
        clean = [DEFAULT_SITE]
    set_user(store, chat_id, "marketplace.names", clean)
    set_user(store, chat_id, "marketplace.name", clean[0])
    if is_owner_chat(store, chat_id):
        # the operator's own chat keeps the machine-wide active site in sync (CLI, old flows)
        store.set("marketplace.name", clean[0])
    return clean


def site_ready(store: SettingsStore, chat_id: str | int, site: str) -> bool:
    if site == "mock":
        return True
    return site_credentials(site, store, chat_id=chat_id).ready


@dataclass
class SitePlan:
    """What will run, what waits for a bigger plan, and what still needs an account."""

    running: list[str] = field(default_factory=list)  # selected, within the plan cap, account ready
    paused_by_plan: list[str] = field(default_factory=list)  # selected, beyond the cap
    missing_account: list[str] = field(default_factory=list)  # within the cap but not logged in

    @property
    def any_runnable(self) -> bool:
        return bool(self.running)


def plan_sites(store: SettingsStore, chat_id: str | int, ent: Entitlements) -> SitePlan:
    kept, paused = ent.allowed_sites(user_sites(store, chat_id))
    running = [s for s in kept if site_ready(store, chat_id, s)]
    missing = [s for s in kept if s not in running]
    return SitePlan(running=running, paused_by_plan=paused, missing_account=missing)


@dataclass
class ToggleResult:
    ok: bool
    action: str  # added | removed | switched | kept | refused_limit | refused_last
    sites: list[str]
    site: str
    message: str = ""
    needs_account: bool = False


def toggle_user_site(store: SettingsStore, chat_id: str | int, site: str, ent: Entitlements) -> ToggleResult:
    """Tap on a site in /sites.

    * selected → deselect (at least one must stay),
    * not selected and there is room → add,
    * not selected, plan allows exactly one site → switch to it (what free users expect),
    * not selected, plan full → refuse and say what to do.
    """
    site = str(site).strip().lower()
    current = user_sites(store, chat_id)
    if site in current:
        if len(current) == 1:
            return ToggleResult(False, "refused_last", current, site)
        new = [s for s in current if s != site]
        set_user_sites(store, chat_id, new)
        return ToggleResult(True, "removed", new, site)
    cap = max(1, ent.max_sites)
    if len(current) < cap:
        new = [*current, site]
        set_user_sites(store, chat_id, new)
        return ToggleResult(True, "added", new, site, needs_account=not site_ready(store, chat_id, site))
    if cap == 1:
        set_user_sites(store, chat_id, [site])
        return ToggleResult(True, "switched", [site], site, needs_account=not site_ready(store, chat_id, site))
    return ToggleResult(False, "refused_limit", current, site)


__all__ = [
    "DEFAULT_SITE",
    "SitePlan",
    "ToggleResult",
    "plan_sites",
    "registered_sites",
    "set_user_sites",
    "site_ready",
    "toggle_user_site",
    "user_sites",
]
