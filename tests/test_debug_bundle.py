from __future__ import annotations

import json
import zipfile
from pathlib import Path

from bidpilot.debug_bundle import (
    _safe_html,
    _safe_json,
    browser_debug_stats,
    build_browser_debug_archive,
    build_browser_debug_archives,
)


def test_safe_html_redacts_form_values_editors_scripts_and_query_strings():
    raw = """
    <html><head><meta name="csrf-token" content="csrf-secret"></head><body>
      <input type="hidden" name="_token" value="csrf-secret">
      <input name="email" value="person@example.com">
      <textarea>this proposal must not leak</textarea>
      <div contenteditable="true">typed proposal content</div>
      <a href="/project/123?token=private&tab=bid">project</a>
      <script>window.secret = 'do-not-export';</script>
    </body></html>
    """
    safe = _safe_html(raw)
    for secret in ("csrf-secret", "person@example.com", "this proposal must not leak",
                   "typed proposal content", "do-not-export", "token=private", "tab=bid"):
        assert secret not in safe
    assert "[REDACTED]" in safe
    assert "/project/123" in safe
    assert "<script>/* omitted */</script>" in safe


def test_safe_json_redacts_sensitive_keys_recursively():
    raw = json.dumps({
        "project_id": "123",
        "cookies": {"PHPSESSID": "session-secret"},
        "forms": [{"name": "bid-form", "csrf_token": "token-secret"}],
        "password": "password-secret",
    })
    safe = json.loads(_safe_json(raw))
    assert safe["project_id"] == "123"
    assert safe["cookies"] == "[REDACTED]"
    assert safe["forms"][0]["csrf_token"] == "[REDACTED]"
    assert safe["password"] == "[REDACTED]"


def test_archive_contains_sanitized_html_dom_json_and_images(tmp_path: Path):
    root = tmp_path / "browser_debug"
    root.mkdir()
    (root / "failure.html").write_text(
        '<form id="init_message_form"><input name="_token" value="csrf-secret">'
        '<textarea>private proposal</textarea><button id="msg_submit_btn">ارسال</button>'
        '<script>secret script content</script></form>',
        encoding="utf-8",
    )
    (root / "failure.json").write_text(
        json.dumps({"project_id": "123", "cookie": "session-secret", "controls": [{"id": "msg_submit_btn"}]}),
        encoding="utf-8",
    )
    (root / "failure.png").write_bytes(b"fake-png-bytes")

    stats = browser_debug_stats(root)
    assert stats["count"] == 3
    archive_path, result = build_browser_debug_archive(root)
    assert archive_path is not None
    try:
        assert result["included"] == 3
        with zipfile.ZipFile(archive_path) as archive:
            names = set(archive.namelist())
            assert {"failure.html", "failure.json", "failure.png", "manifest.json"} <= names
            html = archive.read("failure.html").decode("utf-8")
            data = json.loads(archive.read("failure.json").decode("utf-8"))
            assert "csrf-secret" not in html
            assert "private proposal" not in html
            assert "secret script content" not in html
            assert data["cookie"] == "[REDACTED]"
            assert "session-secret" not in json.dumps(data)
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            assert "failure.png" in manifest["files"]
    finally:
        archive_path.unlink(missing_ok=True)


def test_empty_archive_returns_no_file(tmp_path: Path):
    archive, stats = build_browser_debug_archive(tmp_path / "empty")
    assert archive is None
    assert stats["count"] == 0


def test_all_debug_files_are_split_into_multiple_archives_when_needed(tmp_path: Path):
    root = tmp_path / "many_debug_files"
    root.mkdir()
    expected = set()
    for index in range(5):
        name = f"failure-{index}.json"
        (root / name).write_text('{"id": 1}', encoding="utf-8")
        expected.add(name)

    archives, stats = build_browser_debug_archives(root, max_files=2, max_total_bytes=20)
    assert len(archives) == 3
    assert stats["parts"] == 3
    try:
        received = set()
        for archive_path, part_stats in archives:
            assert archive_path.exists()
            assert part_stats["total_parts"] == 3
            with zipfile.ZipFile(archive_path) as archive:
                manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
                received.update(manifest["files"])
        assert received == expected
    finally:
        for archive_path, _part_stats in archives:
            archive_path.unlink(missing_ok=True)
