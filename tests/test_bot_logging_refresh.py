from __future__ import annotations

from bidpilot.bot import BotService
from bidpilot import logging_utils


class _Store:
    def get(self, key: str):
        return {"admin.logging.enabled": True, "admin.logging.level": "DEBUG"}.get(key)


def test_refresh_logging_uses_non_reserved_configured_level(monkeypatch):
    monkeypatch.setattr(logging_utils, "setup_file_logging", lambda store: None)
    service = object.__new__(BotService)
    service.store = _Store()
    captured = {}
    service._log_event = lambda *args, **kwargs: captured.update(args=args, fields=kwargs)

    service._refresh_logging()

    assert captured["args"] == ("INFO", "system", "logging reconfigured")
    assert captured["fields"]["enabled"] == "True"
    assert captured["fields"]["configured_level"] == "DEBUG"
    assert "level" not in captured["fields"]
