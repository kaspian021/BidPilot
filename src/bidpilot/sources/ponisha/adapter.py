"""Ponisha (ponisha.ir) source adapter."""

from __future__ import annotations

from pathlib import Path

from ..html_market import HtmlMarketSource, load_yaml_settings
from ..registry import register_source

_CONFIG_PATH = Path(__file__).with_name("config.yaml")


def load_default_settings() -> dict:
    return load_yaml_settings(_CONFIG_PATH)


@register_source("ponisha")
class PonishaSource(HtmlMarketSource):
    name = "ponisha"
    config_file = _CONFIG_PATH
    default_base_url = "https://ponisha.ir"


__all__ = ["PonishaSource", "load_default_settings"]
