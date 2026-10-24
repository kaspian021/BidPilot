"""“Did the login actually work?” — one place both the bot and the CLI use.

After the account is stored we no longer guess: the source is built exactly the
way the scheduler builds it, the session is verified against the real site and
the listing is parsed once. The bot then tells the user «وارد حساب شما شدیم ✅»
before offering the start button.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .config import AppConfig, Secrets, load_config, secrets_from_store
from .credentials import site_credentials
from .onboarding import DEFAULT_SITE, site_label
from .settings_store import SettingsStore
from .sources import create_source

log = logging.getLogger(__name__)


@dataclass
class SiteCheck:
    site: str
    label: str
    ok: bool
    detail: str
    method: str = "cookie"
    projects: int = 0

    @property
    def title(self) -> str:
        head = "✅ وارد حساب شما شدیم" if self.ok else "❌ ورود به حساب انجام نشد"
        return f"{head} — {self.label}"

    def message(self) -> str:
        from html import escape

        lines = [f"<b>{escape(self.title)}</b>", escape(self.detail)]
        if self.ok and self.projects:
            lines.append(f"📋 {self.projects} پروژه در صفحه‌ی اول خوانده شد.")
        if not self.ok:
            lines.append("")
            lines.append("🔑 /market → روش ورود و کوکی/رمز را بررسی کن.")
        return "\n".join(lines)


def _site_env(store: SettingsStore, config_path: str) -> tuple[AppConfig, Secrets]:
    cfg = load_config(config_path, store=store)
    secrets = secrets_from_store(Secrets(), store)
    return cfg, secrets


def _source_settings(cfg: AppConfig, site: str) -> dict[str, Any]:
    entry = cfg.source(site)
    return dict((entry.settings if entry else {}) or {})


async def check_site(
    store: SettingsStore,
    *,
    config_path: str = "config/config.yaml",
    site: str | None = None,
    fetch_projects: bool = True,
) -> SiteCheck:
    """Verify the active (or given) marketplace account, without sending anything."""
    cfg, secrets = _site_env(store, config_path)
    name = str(site or store.get("marketplace.name") or DEFAULT_SITE).strip().lower()
    label = site_label(name)
    creds = site_credentials(name, store)

    try:
        source = create_source(name, _source_settings(cfg, name), cfg, secrets)
    except Exception as exc:  # unknown source / broken config
        return SiteCheck(name, label, False, f"سایت ساخته نشد: {exc}", method=creds.method)

    try:
        if name == "mock":
            ok, detail = await source.healthcheck()
            return SiteCheck(name, label, bool(ok), detail, method="none")

        if not creds.ready:
            return SiteCheck(
                name,
                label,
                False,
                "کوکی/نام کاربری ثبت نشده است — «/market» را بزن.",
                method=creds.method,
            )

        ok, detail = await source.verify_session()
        projects = 0
        if ok and fetch_projects:
            try:
                health_ok, health_detail = await source.healthcheck()
                if health_ok:
                    # only the count line below is shown — appending the raw
                    # healthcheck text used to print the same sentence twice
                    projects = _project_count_from_detail(health_detail)
                else:
                    ok, detail = False, f"{detail}\n⚠️ {health_detail}"
            except Exception as exc:  # network hiccup on the listing page
                ok, detail = False, f"{detail}\n⚠️ لیست پروژه‌ها خوانده نشد: {exc}"
        return SiteCheck(name, label, bool(ok), detail, method=creds.method, projects=projects)
    finally:
        try:
            await source.aclose()
        except Exception:  # pragma: no cover
            pass


def _project_count_from_detail(detail: str) -> int:
    text = str(detail or "")
    for token in text.replace("،", " ").split():
        cleaned = token.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
        if cleaned.isdigit():
            return int(cleaned)
    return 0


def quick_status(store: SettingsStore) -> str:
    """Cheap, offline snapshot for /status (no network)."""
    site = str(store.get("marketplace.name") or DEFAULT_SITE)
    creds = site_credentials(site, store)
    return f"{site_label(site)} — {creds.summary}"


__all__ = ["SiteCheck", "check_site", "quick_status"]
