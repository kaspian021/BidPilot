"""«Which BidPilot is actually running?» — the stale-install bug.

The reported symptom: the terminal shows 0.6.x and `explore`, but the bot keeps
answering with old wording and an old pypdf message, because `python -m bidpilot`
resolved to a PyPI copy in site-packages instead of the repository.
"""

from __future__ import annotations

import sys

from typer.testing import CliRunner

import bidpilot.runtime_info as runtime_info
from bidpilot.cli import app
from bidpilot.runtime_info import install_info


def test_the_checkout_is_recognised():
    info = install_info()

    assert info.is_checkout
    assert info.repo_version == runtime_info.__version__
    assert not info.stale
    assert info.warning() == ""
    assert "🧩" in info.status_line()


def test_a_stale_site_packages_copy_is_reported(tmp_path, monkeypatch):
    repo = tmp_path / "bidpilot"
    (repo / "src").mkdir(parents=True)
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "bidpilot"\nversion = "9.9.9"\n', encoding="utf-8"
    )
    installed = tmp_path / "venv" / "Lib" / "site-packages" / "bidpilot" / "__init__.py"
    installed.parent.mkdir(parents=True)
    installed.write_text("", encoding="utf-8")

    monkeypatch.chdir(repo)
    monkeypatch.setattr(runtime_info, "_package_file", lambda: installed)

    info = install_info()

    assert info.stale, "an older installed copy next to a newer checkout is the bug"
    assert info.is_checkout is False
    assert info.is_site_packages
    assert "9.9.9" in info.warning()
    assert "site-packages" in info.summary() or "پوشه‌ی پروژه" in info.summary()
    # the fix must target the interpreter that is running, not a bare `pip`
    assert sys.executable in info.fix_command() or "python" in info.fix_command()
    assert "-e" in info.fix_command()
    assert "🧩" in info.status_line() and "9.9.9" in info.status_line()


def test_version_command_shows_where_the_code_runs():
    result = CliRunner().invoke(app, ["version"])

    assert result.exit_code == 0, result.output
    assert "BidPilot" in result.output
    assert "python" in result.output
    assert "bidpilot" in result.output.split("—", 1)[-1]
