from __future__ import annotations

import logging
from pathlib import Path

from bidpilot.app import configure_logging


def test_configure_logging_writes_rotating_file_and_suppresses_sql_noise(tmp_path, monkeypatch):
    root = logging.getLogger()
    original_root_level = root.level
    db_logger = logging.getLogger("aiosqlite")
    original_db_level = db_logger.level
    monkeypatch.chdir(tmp_path)

    try:
        configure_logging("DEBUG")
        assert db_logger.level >= logging.WARNING

        logging.getLogger("bidpilot.test.logging").debug("pipeline-diagnostic-marker")
        db_logger.debug("sqlite-noise-marker")

        for handler in root.handlers:
            handler.flush()

        log_path = Path("data/logs/bidpilot.log")
        assert log_path.is_file()
        contents = log_path.read_text(encoding="utf-8")
        assert "pipeline-diagnostic-marker" in contents
        assert "sqlite-noise-marker" not in contents
    finally:
        for handler in list(root.handlers):
            if getattr(handler, "_bidpilot_kind", "") == "console" or getattr(handler, "_bidpilot_log_path", None):
                root.removeHandler(handler)
                handler.close()
        root.setLevel(original_root_level)
        db_logger.setLevel(original_db_level)
