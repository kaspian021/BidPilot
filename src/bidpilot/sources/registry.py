"""Source registry — the extension point for new marketplaces."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..config import AppConfig, Secrets
from .base import Source

_REGISTRY: dict[str, type[Source]] = {}
_BUILTINS_LOADED = False


def register_source(name: str) -> Callable[[type[Source]], type[Source]]:
    def decorator(cls: type[Source]) -> type[Source]:
        cls.name = name
        _REGISTRY[name] = cls
        return cls

    return decorator


def available_sources() -> list[str]:
    _load_builtin()
    return sorted(_REGISTRY)


def get_source_class(name: str) -> type[Source]:
    _load_builtin()
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise KeyError(f"Unknown source {name!r}. Available: {', '.join(sorted(_REGISTRY))}") from exc


def create_source(
    name: str,
    settings: dict[str, Any],
    config: AppConfig,
    secrets: Secrets,
    client: Any | None = None,
) -> Source:
    return get_source_class(name)(settings=settings, config=config, secrets=secrets, client=client)


def _load_builtin() -> None:
    """Import built-in adapters once so their decorators run."""
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return
    _BUILTINS_LOADED = True
    from . import mock, parscoders  # noqa: F401


__all__ = [
    "register_source",
    "available_sources",
    "get_source_class",
    "create_source",
]
