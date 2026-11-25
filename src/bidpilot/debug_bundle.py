"""Safe browser diagnostic archive for the Telegram admin panel.

The archive includes HTML/DOM snapshots and screenshots written by the Playwright
submitter. Form values, editor text, inline scripts, query strings and token-like
attributes are redacted before a file is added to the ZIP.
"""

from __future__ import annotations

import json
import re
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

_DEFAULT_ROOT = Path("data/browser_debug")
_ALLOWED_SUFFIXES = {".html", ".htm", ".json", ".png", ".jpg", ".jpeg", ".webp"}
_SENSITIVE_KEY = re.compile(r"(?:token|csrf|password|passwd|secret|cookie|api.?key|authorization)", re.I)
_MAX_FILES = 100
_MAX_TOTAL_BYTES = 35 * 1024 * 1024
_MAX_SINGLE_FILE_BYTES = 10 * 1024 * 1024


def _safe_html(source: str) -> str:
    """Redact values from old as well as newly-created browser snapshots."""
    text = str(source or "")
    text = re.sub(r"(?is)<script\\b[^>]*>.*?</script\\s*>", "<script>/* omitted */</script>", text)
    text = re.sub(r"(?is)<noscript\\b[^>]*>.*?</noscript\\s*>", "", text)
    text = re.sub(
        r"(?is)(<textarea\\b[^>]*>).*?(</textarea\\s*>)",
        r"\\1[REDACTED]\\2",
        text,
    )

    def redact_input(match: re.Match[str]) -> str:
        tag = match.group(0)
        tag = re.sub(
            r"""(?i)(\\bvalue\\s*=\\s*)(?:"[^"]*"|'[^']*'|[^\\s>]+)""",
            r'\\1"[REDACTED]"',
            tag,
        )
        return tag

    text = re.sub(r"(?is)<input\\b[^>]*>", redact_input, text)
    text = re.sub(
        r"""(?i)(<meta\\b[^>]*(?:csrf|token)[^>]*\\bcontent\\s*=\\s*)(["']).*?\\2""",
        r'\\1"[REDACTED]"',
        text,
    )

    def strip_url(match: re.Match[str]) -> str:
        prefix, quote, raw = match.groups()
        try:
            parsed = urlsplit(raw)
            safe = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        except Exception:
            safe = raw.split("?", 1)[0].split("#", 1)[0]
        return prefix + quote + safe + quote

    text = re.sub(
        r"""(?i)(\\b(?:href|action|data-href|data-url|data-action)\\s*=\\s*)(["'])(.*?)\\2""",
        strip_url,
        text,
        flags=re.DOTALL,
    )
    return text[:500_000]


def _safe_json(source: str) -> str:
    try:
        payload = json.loads(source)
    except (ValueError, TypeError):
        return json.dumps({"warning": "invalid JSON diagnostic omitted"}, ensure_ascii=False)

    def walk(value: Any, key: str = "") -> Any:
        if _SENSITIVE_KEY.search(key):
            return "[REDACTED]"
        if isinstance(value, dict):
            return {str(k): walk(v, str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(item, key) for item in value]
        return value

    return json.dumps(walk(payload), ensure_ascii=False, indent=2)


def browser_debug_stats(root: str | Path = _DEFAULT_ROOT) -> dict[str, Any]:
    directory = Path(root)
    if not directory.exists() or not directory.is_dir():
        return {"count": 0, "size_bytes": 0, "size_mb": 0.0, "latest": "", "files": {}}
    files: list[Path] = []
    total = 0
    by_type: dict[str, int] = {}
    for item in directory.iterdir():
        try:
            if item.is_symlink() or not item.is_file() or item.suffix.lower() not in _ALLOWED_SUFFIXES:
                continue
            size = item.stat().st_size
        except OSError:
            continue
        files.append(item)
        total += size
        suffix = item.suffix.lower()
        by_type[suffix] = by_type.get(suffix, 0) + 1
    files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    return {
        "count": len(files),
        "size_bytes": total,
        "size_mb": round(total / (1024 * 1024), 2),
        "latest": files[0].name if files else "",
        "files": by_type,
    }


def build_browser_debug_archive(
    root: str | Path = _DEFAULT_ROOT,
    *,
    max_files: int = _MAX_FILES,
    max_total_bytes: int = _MAX_TOTAL_BYTES,
) -> tuple[Path | None, dict[str, Any]]:
    """Build a bounded ZIP of all available HTML, JSON and screenshot artifacts."""
    directory = Path(root)
    stats = browser_debug_stats(directory)
    if stats["count"] == 0:
        return None, stats

    candidates: list[Path] = []
    for item in directory.iterdir():
        try:
            if item.is_symlink() or not item.is_file() or item.suffix.lower() not in _ALLOWED_SUFFIXES:
                continue
            if item.stat().st_size > _MAX_SINGLE_FILE_BYTES:
                continue
            candidates.append(item)
        except OSError:
            continue
    candidates.sort(key=lambda item: item.stat().st_mtime, reverse=True)

    handle = tempfile.NamedTemporaryFile(prefix="bidpilot-browser-debug-", suffix=".zip", delete=False)
    archive_path = Path(handle.name)
    handle.close()
    included: list[str] = []
    included_bytes = 0
    skipped = 0
    try:
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for item in candidates[:max(1, min(int(max_files), 250))]:
                try:
                    size = item.stat().st_size
                    if included_bytes + size > max_total_bytes:
                        skipped += 1
                        continue
                    suffix = item.suffix.lower()
                    if suffix in {".html", ".htm"}:
                        data = _safe_html(item.read_text(encoding="utf-8", errors="replace")).encode("utf-8")
                        archive.writestr(item.name, data)
                    elif suffix == ".json":
                        data = _safe_json(item.read_text(encoding="utf-8", errors="replace")).encode("utf-8")
                        archive.writestr(item.name, data)
                    else:
                        archive.write(item, arcname=item.name)
                    included.append(item.name)
                    included_bytes += size
                except OSError:
                    skipped += 1
            manifest = {
                "product": "BidPilot",
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "redaction": "HTML values, textarea/editor contents, scripts, query strings and secret-like JSON keys are redacted.",
                "files": included,
                "included_source_bytes": included_bytes,
                "skipped_files": skipped + max(0, len(candidates) - len(included) - skipped),
            }
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        if not included:
            archive_path.unlink(missing_ok=True)
            return None, stats
        result = {
            **stats,
            "included": len(included),
            "included_bytes": included_bytes,
            "skipped": skipped,
            "archive_bytes": archive_path.stat().st_size,
            "archive": str(archive_path),
        }
        return archive_path, result
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise


__all__ = ["browser_debug_stats", "build_browser_debug_archive"]
