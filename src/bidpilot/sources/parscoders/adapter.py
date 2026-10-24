"""Parscoders source adapter (built on the shared config-driven HTML adapter)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

from ...config import AppConfig, Secrets
from ...credentials import parse_cookie_header
from ..html_market import HtmlMarketSource, load_yaml_settings
from ..registry import register_source

log = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).with_name("config.yaml")


def load_default_settings() -> dict[str, Any]:
    return load_yaml_settings(_CONFIG_PATH)


@register_source("parscoders")
class ParscodersSource(HtmlMarketSource):
    name = "parscoders"
    config_file = _CONFIG_PATH
    default_base_url = "https://parscoders.com"

    def __init__(
        self,
        settings: dict[str, Any],
        config: AppConfig,
        secrets: Secrets,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(settings, config, secrets, client)
        # legacy single-site configuration (PARSOCODERS_COOKIE / config cookie)
        cookie = self.secrets.parscoders_cookie or self.settings.get("cookie", "")
        self._session_cookies: dict[str, str] = parse_cookie_header(cookie)

    # ------------------------------------------------------------------ http
    def cookies(self) -> dict[str, str]:
        cookies = super().cookies()
        cookies.update(getattr(self, "_session_cookies", {}) or {})
        return cookies

    # ------------------------------------------------------------------ auth
    def credentials_present(self) -> bool:
        return bool(self._session_cookies) or super().credentials_present()


__all__ = ["ParscodersSource", "load_default_settings", "parse_cookie_header"]
