"""Professional, detailed logging for BidPilot — toggleable via admin panel.

When enabled (admin.logging.enabled = true) every tiny action is recorded:
updates, commands, callbacks, onboarding flows, profile imports, admin changes,
payments, referrals, scheduler cycles, network probes and all exceptions.

Privacy: tokens, API keys, cookies, passwords and merchant IDs are scrubbed
before they ever reach the file.

File: ``data/logs/bidpilot.log`` with rotation (5 MB x 5 = 30 MB max).
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from logging.handlers import RotatingFileHandler

from .settings_store import SettingsStore, get_store

# --------------------------------------------------------------------------- #
# Paths & rotation
# --------------------------------------------------------------------------- #
LOG_DIR = Path("data/logs")
LOG_FILE = LOG_DIR / "bidpilot.log"
LEGACY_LOG_FILE = Path("data/bidpilot.log")

MAX_BYTES = 5 * 1024 * 1024  # 5 MB
BACKUP_COUNT = 5

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
LEVEL_EMOJI = {"DEBUG": "🐛", "INFO": "ℹ️", "WARNING": "⚠️", "ERROR": "❌", "CRITICAL": "💥"}

# --------------------------------------------------------------------------- #
# Sensitive scrubbing — never write secrets to disk
# --------------------------------------------------------------------------- #
# 1) Telegram bot token:  123456789:AAH... (30+ chars)
_TOKEN_RE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{25,60}\b")
# 2) Generic API keys: sk-..., gsk_..., xai-..., groq..., etc.
_API_KEY_RE = re.compile(
    r"\b(?:sk-|gsk_|xai-|groq-|openai_|api_key=|apikey=)([A-Za-z0-9_-]{8,})",
    re.IGNORECASE,
)
# 3) Long hex/base64 secrets (>=20 chars hex/B64 that look random)
_LONG_SECRET_RE = re.compile(r"\b[A-Za-z0-9_-]{32,}\b")
# 4) Cookie / password assignments
_COOKIE_RE = re.compile(r"(cookie|session|password|passwd|pwd|secret|merchant_id)\s*=\s*([^\s&;\"']{4,})", re.IGNORECASE)


def scrub_sensitive(text: str, store: SettingsStore | None = None) -> str:
    """Mask tokens, API keys and cookies in an arbitrary string."""
    if not text:
        return text
    s = str(text)
    # Mask Telegram token
    s = _TOKEN_RE.sub("***BOT_TOKEN***", s)
    # Mask API keys
    s = _API_KEY_RE.sub(r"\1***", s)
    # Mask cookie/password assignments
    s = _COOKIE_RE.sub(r"\1=***", s)
    # Also scrub any value that exactly equals a stored secret
    if store is not None:
        try:
            secrets = []
            for key in (
                "telegram.bot_token",
                "llm.api_key",
                "llm.keys.groq",
                "llm.keys.openai",
                "llm.keys.anthropic",
                "llm.keys.gemini",
                "llm.keys.openrouter",
                "credentials.parscoders.cookie",
                "credentials.parscoders.password",
                "zarinpal.merchant_id",
                "payments.zibal.merchant_id",
                "payments.idpay.merchant_id",
                "payments.nextpay.merchant_id",
                "payments.zarinpal.merchant_id",
            ):
                v = store.get(key)
                if v and isinstance(v, str) and len(v) > 6:
                    secrets.append(v)
            # also any llm.keys.* entry
            all_keys = store.get("llm.keys")
            if isinstance(all_keys, dict):
                for v in all_keys.values():
                    if v and isinstance(v, str) and len(v) > 6:
                        secrets.append(v)
            for sec in secrets:
                if sec in s:
                    masked = sec[:4] + "***" + sec[-4:] if len(sec) > 8 else "***"
                    s = s.replace(sec, masked)
        except Exception:
            pass
    return s


class SensitiveFormatter(logging.Formatter):
    """Formatter that scrubs secrets before writing."""

    def format(self, record: logging.LogRecord) -> str:
        # Scrub the message itself
        try:
            if isinstance(record.msg, str):
                record.msg = scrub_sensitive(record.msg)
            if record.args:
                # args may contain secrets
                if isinstance(record.args, dict):
                    record.args = {k: scrub_sensitive(str(v)) for k, v in record.args.items()}
                elif isinstance(record.args, tuple):
                    record.args = tuple(scrub_sensitive(str(a)) if isinstance(a, str) else a for a in record.args)
        except Exception:
            pass
        s = super().format(record)
        return scrub_sensitive(s)


# --------------------------------------------------------------------------- #
# Store helpers
# --------------------------------------------------------------------------- #
def is_logging_enabled(store: SettingsStore | None = None) -> bool:
    store = store or get_store()
    # New key: admin.logging.enabled ; fallback legacy: admin.debug / admin.logging
    v = store.get("admin.logging.enabled")
    if v is not None:
        return bool(store.get_bool("admin.logging.enabled", False))
    # legacy: admin.debug
    if store.get("admin.debug") is not None:
        return bool(store.get_bool("admin.debug", False))
    # also check admin.logging (bool)
    if store.get("admin.logging") is not None:
        return bool(store.get_bool("admin.logging", False))
    return False


def get_logging_level(store: SettingsStore | None = None) -> str:
    store = store or get_store()
    lvl = store.get("admin.logging.level") or store.get("admin.logging.level") or store.get("admin.log_level")
    if lvl:
        lvl = str(lvl).upper().strip()
        if lvl in LEVELS:
            return lvl
    # legacy: if debug enabled, default DEBUG
    if is_logging_enabled(store):
        return "DEBUG"
    return "INFO"


def set_logging_enabled(store: SettingsStore | None, enabled: bool) -> None:
    store = store or get_store()
    store.set("admin.logging.enabled", bool(enabled))
    # keep legacy in sync
    store.set("admin.debug", bool(enabled))


def set_logging_level(store: SettingsStore | None, level: str) -> None:
    store = store or get_store()
    lvl = str(level).upper().strip()
    if lvl not in LEVELS:
        raise ValueError(f"سطح نامعتبر: {level} — یکی از {', '.join(LEVELS)}")
    store.set("admin.logging.level", lvl)


# --------------------------------------------------------------------------- #
# File handler setup
# --------------------------------------------------------------------------- #
def setup_file_logging(
    store: SettingsStore | None = None,
    level: str | None = None,
    enabled: bool | None = None,
) -> None:
    """Attach or detach the rotating file handler according to store."""
    store = store or get_store()
    if enabled is None:
        enabled = is_logging_enabled(store)
    if level is None:
        level = get_logging_level(store)

    root = logging.getLogger()
    # Remove old BidPilot file handlers
    for h in list(root.handlers):
        if getattr(h, "_bidpilot_file", False):
            try:
                root.removeHandler(h)
                h.close()
            except Exception:
                pass

    # Also migrate legacy file if exists and new dir missing
    try:
        if LEGACY_LOG_FILE.exists() and not LOG_FILE.exists():
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            # don't move, just start fresh; legacy will be read as fallback
            pass
    except Exception:
        pass

    if not enabled:
        # Ensure directory exists even when disabled (for stats)
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            os.chmod(LOG_DIR, 0o700)
        except Exception:
            pass
        return

    # Enabled — create handler
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(LOG_DIR, 0o700)
        except Exception:
            pass

        level_val = getattr(logging, level.upper(), logging.DEBUG)
        handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
        handler._bidpilot_file = True  # type: ignore[attr-defined]
        handler.setLevel(level_val)
        fmt = SensitiveFormatter(
            "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(fmt)
        root.addHandler(handler)
        # Lower root level if needed so DEBUG can flow to file
        if root.level > level_val:
            root.setLevel(level_val)
        # Keep httpx noisy down
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger(__name__).info("File logging enabled at %s -> %s (max %d MB x %d)", level, LOG_FILE, MAX_BYTES // 1024 // 1024, BACKUP_COUNT)
    except Exception as exc:
        logging.getLogger(__name__).warning("Could not setup file logging: %s", exc)


# --------------------------------------------------------------------------- #
# Stats & tail
# --------------------------------------------------------------------------- #
def get_log_stats(store: SettingsStore | None = None) -> dict[str, Any]:
    """Return size, lines, errors, last modified, etc. for UI."""
    store = store or get_store()
    enabled = is_logging_enabled(store)
    level = get_logging_level(store)
    # Prefer new file, fallback to legacy
    target = LOG_FILE if LOG_FILE.exists() else (LEGACY_LOG_FILE if LEGACY_LOG_FILE.exists() else LOG_FILE)
    exists = target.exists()
    size = 0
    lines = 0
    errors = 0
    warns = 0
    last = None
    file_path = str(target)
    try:
        if exists:
            st = target.stat()
            size = st.st_size
            last = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
            # Count lines efficiently
            with target.open("r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    lines += 1
                    if " ERROR " in line or " CRITICAL " in line:
                        errors += 1
                    if " WARNING " in line:
                        warns += 1
            # also check backups size
            total_size = size
            for i in range(1, BACKUP_COUNT + 1):
                p = Path(f"{target}.{i}")
                if p.exists():
                    total_size += p.stat().st_size
        else:
            total_size = 0
    except Exception:
        total_size = size

    return {
        "enabled": enabled,
        "level": level,
        "exists": exists,
        "file": file_path,
        "size": size,
        "total_size": total_size if 'total_size' in locals() else size,
        "size_mb": round(size / 1024 / 1024, 2) if size else 0,
        "total_mb": round(total_size / 1024 / 1024, 2) if 'total_size' in locals() and total_size else 0,
        "lines": lines,
        "errors": errors,
        "warns": warns,
        "last": last or "—",
        "max_mb": MAX_BYTES // 1024 // 1024,
        "backup_count": BACKUP_COUNT,
    }


def tail_logs(n: int = 50, level: str | None = None, grep: str | None = None) -> list[str]:
    """Return last N lines, optionally filtered by level or substring."""
    target = LOG_FILE if LOG_FILE.exists() else LEGACY_LOG_FILE
    if not target.exists():
        return []
    try:
        lines = target.read_text(encoding="utf-8", errors="ignore").splitlines()
        if level:
            level = level.upper()
            lines = [l for l in lines if f" {level} " in l or f"| {level}" in l or level in l]
        if grep:
            g = grep.lower()
            lines = [l for l in lines if g in l.lower()]
        return lines[-n:]
    except Exception:
        return []


def clear_logs(store: SettingsStore | None = None) -> dict[str, Any]:
    """Truncate main log and backups. Returns stats before clear."""
    stats = get_log_stats(store)
    try:
        # Truncate main
        if LOG_FILE.exists():
            LOG_FILE.write_text("", encoding="utf-8")
        # Legacy too
        if LEGACY_LOG_FILE.exists():
            try:
                LEGACY_LOG_FILE.write_text("", encoding="utf-8")
            except Exception:
                pass
        for i in range(1, BACKUP_COUNT + 1):
            for base in (LOG_FILE, LEGACY_LOG_FILE):
                p = Path(f"{base}.{i}")
                if p.exists():
                    try:
                        p.unlink()
                    except Exception:
                        pass
                # also handle compressed? not needed
        logging.getLogger(__name__).info("Logs cleared by admin — previous size %s bytes, %s lines", stats.get("size"), stats.get("lines"))
    except Exception as exc:
        logging.getLogger(__name__).warning("Clear logs failed: %s", exc)
    return stats


# --------------------------------------------------------------------------- #
# Structured helper for bot — logs at right level with scrubbing
# --------------------------------------------------------------------------- #
def log_event(
    logger: logging.Logger,
    level: str,
    category: str,
    message: str,
    store: SettingsStore | None = None,
    **fields: Any,
) -> None:
    """Log with category prefix and key=value fields, scrubbed."""
    try:
        if store is not None and not is_logging_enabled(store):
            # When disabled, only log WARNING+ to file — but since handler
            # is removed, nothing goes to file. We still allow logger to
            # decide if console should see it.
            # To respect "disabled = silent file", we return early for DEBUG/INFO
            lvl = getattr(logging, level.upper(), logging.INFO)
            if lvl < logging.WARNING:
                return
        # Build fields string
        extra = ""
        if fields:
            parts = []
            for k, v in fields.items():
                sv = scrub_sensitive(str(v), store)
                # Truncate very long values
                if len(sv) > 300:
                    sv = sv[:300] + "…"
                parts.append(f"{k}={sv}")
            extra = " | " + " ".join(parts)
        full = f"[{category}] {scrub_sensitive(message, store)}{extra}"
        lvl = getattr(logging, level.upper(), logging.INFO)
        if lvl == logging.DEBUG:
            logger.debug(full)
        elif lvl == logging.INFO:
            logger.info(full)
        elif lvl == logging.WARNING:
            logger.warning(full)
        elif lvl == logging.ERROR:
            logger.error(full)
        elif lvl == logging.CRITICAL:
            logger.critical(full)
        else:
            logger.info(full)
    except Exception:
        # Never break main flow because of logging
        pass
