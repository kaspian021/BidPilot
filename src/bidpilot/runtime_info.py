"""Which BidPilot is actually running? — the question behind half the weird bugs.

A user can have three copies of this project on one machine:

* the **repository** they just ``git pull``ed (what they *think* they run),
* an old **PyPI/editable install** in the virtualenv,
* and whatever ``python`` happens to be on ``PATH``.

When ``python -m bidpilot`` resolves to the stale copy, the terminal shows the
new version while the bot keeps answering with old wording — and nothing in the
output says so. This module answers "what am I running, and is it the folder
next to my terminal?" in one place, so ``/status``, ``bidpilot version``,
``bidpilot doctor`` and the bot's start log can all tell the truth.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

from . import __version__

_VERSION_RE = re.compile(r'^\s*version\s*=\s*"([^"]+)"', re.M)


def _package_file() -> Path:
    return Path(__file__).resolve()


def _has_pyproject(path: Path) -> bool:
    pyproject = path / "pyproject.toml"
    if not pyproject.is_file():
        return False
    try:
        text = pyproject.read_text(encoding="utf-8", errors="replace")
    except OSError:  # pragma: no cover - unreadable file
        return False
    return bool(re.search(r'^\s*name\s*=\s*"bidpilot"', text, re.M))


def find_project_root(start: Path | None = None) -> Path | None:
    """Nearest parent of ``start`` that looks like the BidPilot repository."""
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if _has_pyproject(candidate):
            return candidate
    return None


def repo_version(root: Path | None) -> str | None:
    if root is None:
        return None
    try:
        text = (root / "pyproject.toml").read_text(encoding="utf-8", errors="replace")
    except OSError:  # pragma: no cover
        return None
    match = _VERSION_RE.search(text)
    return match.group(1) if match else None


@dataclass(frozen=True)
class InstallInfo:
    version: str
    package_file: Path
    project_root: Path | None
    repo_version: str | None

    @property
    def is_checkout(self) -> bool:
        """True when this package file lives inside the repository we can see."""
        root = self.project_root
        return bool(root) and root in self.package_file.parents

    @property
    def stale(self) -> bool:
        """The repository on disk is newer than the code that is running."""
        return bool(self.repo_version and self.repo_version != self.version)

    @property
    def is_site_packages(self) -> bool:
        parts = self.package_file.parts
        return "site-packages" in parts or "dist-packages" in parts

    def fix_command(self) -> str:
        """Install *this repository* into the interpreter that is running.

        A ``uv``-created venv has no pip, so the command is built from what is
        actually available (``uv pip install --python …`` or ``ensurepip`` first).
        """
        from .deps import editable_install_command, has_pip, install_command

        if self.project_root is not None:
            return editable_install_command(self.project_root)
        if not has_pip():
            exe = sys.executable or "python"
            return f'"{exe}" -m ensurepip --upgrade && "{exe}" -m pip install --upgrade bidpilot'
        return install_command("bidpilot")

    def summary(self) -> str:
        where = str(self.package_file.parent)
        if self.is_checkout:
            return f"{self.version} — {where}"
        if self.project_root is not None:
            return f"{self.version} — نسخه‌ی نصب‌شده در {where} (نه پوشه‌ی پروژه)"
        return f"{self.version} — {where}"

    def status_line(self) -> str:
        """One Persian line for /status — short, and honest about staleness."""
        if self.stale:
            return (
                f"🧩 نسخه: {self.version} — اما پوشه‌ی پروژه نسخه‌ی {self.repo_version} است. "
                "کد قدیمی اجرا می‌شود."
            )
        return f"🧩 نسخه: {self.version}"

    def warning(self) -> str:
        """Empty when everything is consistent."""
        if self.stale:
            return (
                f"نسخه‌ی در حال اجرا {self.version} است ولی پوشه‌ی پروژه نسخه‌ی "
                f"{self.repo_version} را دارد؛ پس تغییرها (مثل جست‌وجوی هوشمند) اعمال نشده‌اند."
            )
        if self.project_root is not None and not self.is_checkout:
            return (
                "BidPilot از یک نسخه‌ی نصب‌شده (site-packages) اجرا می‌شود، نه از همین پوشه. "
                "اگر از داخل پروژه اجرا می‌کنی، نسخه‌ی نصب‌شده قدیمی است."
            )
        return ""


def install_info(start: Path | None = None) -> InstallInfo:
    root = find_project_root(start)
    return InstallInfo(
        version=__version__,
        package_file=_package_file(),
        project_root=root,
        repo_version=repo_version(root) if root else None,
    )


def python_version() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"


def environment_line() -> str:
    """Version + interpreter + path, for the bot's start log."""
    info = install_info()
    return (
        f"BidPilot {info.version} | python {python_version()} | {sys.executable} | "
        f"package {info.package_file.parent}"
    )


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


__all__ = [
    "InstallInfo",
    "environment_line",
    "find_project_root",
    "install_info",
    "is_frozen",
    "python_version",
    "repo_version",
]
