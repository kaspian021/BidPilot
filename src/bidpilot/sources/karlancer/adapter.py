"""Karlancer (karlancer.com) source adapter."""

from __future__ import annotations

from pathlib import Path

from ..html_market import HtmlMarketSource, load_yaml_settings
from ..registry import register_source

_CONFIG_PATH = Path(__file__).with_name("config.yaml")


def load_default_settings() -> dict:
    return load_yaml_settings(_CONFIG_PATH)


@register_source("karlancer")
class KarlancerSource(HtmlMarketSource):
    name = "karlancer"
    config_file = _CONFIG_PATH
    default_base_url = "https://karlancer.com"


__all__ = ["KarlancerSource", "load_default_settings"]
