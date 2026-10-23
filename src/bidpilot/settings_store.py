"""Durable, user-supplied settings — BidPilot works without any ``.env`` file.

Everything the user is asked for (Telegram bot token, marketplace account,
a free LLM key, profile, tone of voice, submit mode…) is stored in a single
JSON document inside ``data/``:

* the directory is ``0700`` and the file is ``0600``;
* writes are atomic (temp file + ``os.replace``), so a crash never corrupts it;
* ``data/`` is already gitignored, so secrets never reach the repository;
* environment variables remain supported as an *optional* fallback (Docker),
  but they are never required and the store always wins over them.

Access is by dotted key::

    store = get_store()
    store.set("telegram.bot_token", "123:abc")
    store.get("telegram.bot_token")            # -> "123:abc"
    store.update({"proposal.tone": "رسمی", "profile.stack": ["Python"]})
"""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_SETTINGS_PATH = "data/settings.json"
ENV_SETTINGS_PATH = "BIDPILOT_SETTINGS_FILE"
SCHEMA_VERSION = 1

#: A key is treated as secret if any of its path segments looks like a credential.
_SECRET_SEGMENTS = frozenset(
    {
        "token",
        "api_key",
        "apikey",
        "password",
        "passwd",
        "pass",
        "secret",
        "cookie",
        "cookies",
        "keys",
        "credentials",
        "session",
    }
)
_SECRET_SUFFIXES = ("_token", "_key", "_secret", "_password", "_cookie")
_KEY_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
_MISSING = object()


class SettingsError(RuntimeError):
    """Raised when the settings document cannot be read or written."""


def is_secret_key(dotted: str) -> bool:
    parts = [p.lower() for p in dotted.split(".") if p]
    if any(part in _SECRET_SEGMENTS for part in parts):
        return True
    leaf = parts[-1] if parts else ""
    return leaf.endswith(_SECRET_SUFFIXES)


def mask_value(value: Any, *, keep: int = 4) -> str:
    """Make a secret printable: ``1234••••abcd`` (never leaks short values)."""
    if value is None or value == "":
        return "—"
    if isinstance(value, Mapping):
        return f"{{{len(value)} مورد ذخیره‌شده}}"
    text = str(value)
    if len(text) <= keep * 2:
        return "••••"
    return f"{text[:keep]}…{text[-keep:]}"


def _check_key(dotted: str) -> None:
    if not dotted or not _KEY_RE.match(dotted):
        raise SettingsError(f"کلید تنظیمات نامعتبر است: {dotted!r}")


def dot_get(data: Mapping[str, Any], dotted: str, default: Any = None) -> Any:
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return default
        node = node[part]
    return node


def dot_set(data: dict[str, Any], dotted: str, value: Any) -> None:
    _check_key(dotted)
    parts = dotted.split(".")
    node = data
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def dot_delete(data: dict[str, Any], dotted: str) -> bool:
    parts = dotted.split(".")
    node: Any = data
    for part in parts[:-1]:
        node = node.get(part) if isinstance(node, Mapping) else None
        if not isinstance(node, dict):
            return False
    if not isinstance(node, dict) or parts[-1] not in node:
        return False
    del node[parts[-1]]
    return True


def _prune_empty(data: dict[str, Any]) -> dict[str, Any]:
    """Drop empty containers/values so the file stays readable."""
    cleaned: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            nested = _prune_empty(value)
            if nested:
                cleaned[key] = nested
        elif value is not None and value != "" and value != []:
            cleaned[key] = value
    return cleaned


def _resolve_path(path: str | Path | None) -> Path:
    raw = path or os.environ.get(ENV_SETTINGS_PATH) or DEFAULT_SETTINGS_PATH
    return Path(raw).expanduser()


class SettingsStore:
    """A tiny, file-backed key/value store (single process, single machine)."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = _resolve_path(path)
        self._lock = threading.RLock()
        self._cache: dict[str, Any] | None = None
        self._stamp: tuple[int, int] | None = None

    # ------------------------------------------------------------- internals
    def _stat(self) -> tuple[int, int] | None:
        try:
            st = self.path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _read(self) -> dict[str, Any]:
        stamp = self._stat()
        if self._cache is not None and stamp == self._stamp:
            return self._cache
        if stamp is None:
            self._cache, self._stamp = {}, None
            return self._cache
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError) as exc:
            raise SettingsError(f"خواندن فایل تنظیمات ناموفق بود ({self.path}): {exc}") from exc
        if not isinstance(raw, dict):
            raise SettingsError(f"ساختار فایل تنظیمات نامعتبر است: {self.path}")
        values = raw.get("values", raw)
        self._cache = values if isinstance(values, dict) else {}
        self._stamp = stamp
        return self._cache

    def _write(self, values: dict[str, Any]) -> None:
        payload: dict[str, Any] = {
            "version": SCHEMA_VERSION,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "values": _prune_empty(values),
        }
        directory = self.path.parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(directory, 0o700)
            except OSError:  # pragma: no cover - Windows / exotic FS
                pass
            fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=".settings-", suffix=".tmp")
            try:
                try:
                    os.fchmod(fd, 0o600)
                except (AttributeError, OSError):  # pragma: no cover
                    pass
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False, indent=2)
                    fh.write("\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_name, self.path)
            except BaseException:
                Path(tmp_name).unlink(missing_ok=True)
                raise
            try:
                os.chmod(self.path, 0o600)
            except OSError:  # pragma: no cover
                pass
        except OSError as exc:
            raise SettingsError(f"ذخیره تنظیمات ناموفق بود ({self.path}): {exc}") from exc
        self._cache, self._stamp = payload["values"], self._stat()

    # ------------------------------------------------------------------- api
    @property
    def exists(self) -> bool:
        return self.path.exists()

    def stamp(self) -> tuple[int, int] | None:
        """Cheap change detector used by the scheduler to reload settings."""
        return self._stat()

    def reload(self) -> SettingsStore:
        self._cache = self._stamp = None
        return self

    def values(self) -> dict[str, Any]:
        """A deep copy of everything stored (safe to mutate)."""
        with self._lock:
            return copy.deepcopy(self._read())

    def get(self, dotted: str, default: Any = None) -> Any:
        with self._lock:
            value = dot_get(self._read(), dotted, _MISSING)
        return default if value is _MISSING else copy.deepcopy(value)

    def has(self, dotted: str) -> bool:
        return self.get(dotted, _MISSING) is not _MISSING

    def set(self, dotted: str, value: Any) -> None:
        """Store one value; ``None`` / empty string / empty list removes the key."""
        _check_key(dotted)
        with self._lock:
            values = copy.deepcopy(self._read())
            if value is None or value == "" or value == []:
                dot_delete(values, dotted)
            else:
                dot_set(values, dotted, value)
            self._write(values)

    def update(self, mapping: Mapping[str, Any]) -> None:
        """Store several dotted keys in one atomic write."""
        with self._lock:
            values = copy.deepcopy(self._read())
            for dotted, value in mapping.items():
                _check_key(dotted)
                if value is None or value == "" or value == []:
                    dot_delete(values, dotted)
                else:
                    dot_set(values, dotted, value)
            self._write(values)

    def delete(self, dotted: str) -> bool:
        with self._lock:
            values = copy.deepcopy(self._read())
            removed = dot_delete(values, dotted)
            if removed:
                self._write(values)
            return removed

    def clear(self) -> None:
        with self._lock:
            self._write({})

    # ------------------------------------------------------------- helpers
    def get_int(self, dotted: str, default: int | None = None) -> int | None:
        value = self.get(dotted)
        try:
            return int(value) if value not in (None, "") else default
        except (TypeError, ValueError):
            return default

    def get_list(self, dotted: str) -> list[Any]:
        value = self.get(dotted)
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            return list(value)
        return [value]

    def get_bool(self, dotted: str, default: bool = False) -> bool:
        value = self.get(dotted)
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on", "بله"}

    # --------------------------------------------------------- presentation
    def masked_items(self) -> list[tuple[str, str]]:
        """Flattened, printable (key, value) pairs with credentials masked."""
        rows: list[tuple[str, str]] = []

        def walk(node: Mapping[str, Any], prefix: str) -> None:
            for key, value in node.items():
                dotted = f"{prefix}.{key}" if prefix else key
                if isinstance(value, Mapping):
                    walk(value, dotted)
                elif isinstance(value, list):
                    text = "، ".join(str(v) for v in value)
                    rows.append((dotted, mask_value(text) if is_secret_key(dotted) else text))
                else:
                    rows.append((dotted, mask_value(value) if is_secret_key(dotted) else str(value)))

        walk(self.values(), "")
        return sorted(rows)

    def summary(self, *, reveal: bool = False) -> str:
        """A short human-readable summary used by ``bidpilot settings`` and the bot."""
        if not self.exists:
            return "هنوز هیچ تنظیمی ذخیره نشده است."
        lines: list[str] = []
        for key, value in self.masked_items():
            if reveal and is_secret_key(key):
                raw = self.get(key)
                value = str(raw)
            lines.append(f"{key} = {value}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Process-wide accessor (so config, CLI and bot share one cache)
# ---------------------------------------------------------------------------
_STORES: dict[str, SettingsStore] = {}
_STORES_LOCK = threading.Lock()


def get_store(path: str | Path | None = None) -> SettingsStore:
    resolved = _resolve_path(path)
    key = str(resolved.absolute())
    with _STORES_LOCK:
        store = _STORES.get(key)
        if store is None:
            store = SettingsStore(resolved)
            _STORES[key] = store
        return store


def reset_store_cache() -> None:
    """Test helper — forget cached stores so a new path/env takes effect."""
    with _STORES_LOCK:
        _STORES.clear()


def parse_list_answer(raw: str) -> list[str]:
    """Turn ``"Python، Django, React"`` into a clean list."""
    parts = re.split(r"[,\n،;؛]+", raw or "")
    return [part.strip() for part in parts if part.strip()]


def parse_chat_ids(raw: str) -> list[str]:
    """Parse ``"12345, -100987"`` (Persian digits tolerated) into strings."""
    translation = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
    cleaned = (raw or "").translate(translation)
    values: list[str] = []
    for chunk in re.split(r"[,\s;؛]+", cleaned):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            values.append(str(int(chunk)))
        except ValueError:
            continue
    return values


def first_missing(store: SettingsStore, keys: Iterable[str]) -> list[str]:
    return [key for key in keys if not store.get(key)]


__all__ = [
    "DEFAULT_SETTINGS_PATH",
    "ENV_SETTINGS_PATH",
    "SettingsError",
    "SettingsStore",
    "dot_delete",
    "dot_get",
    "dot_set",
    "first_missing",
    "get_store",
    "is_secret_key",
    "mask_value",
    "parse_chat_ids",
    "parse_list_answer",
    "reset_store_cache",
]
