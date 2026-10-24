"""Optional-but-needed packages, and a way to install them from inside the app.

BidPilot ships ``pypdf`` as a normal dependency, but real virtualenvs lie: the
app may have been installed before ``pypdf`` was added, installed with
``--no-deps``, or created by ``uv`` without ``pip``. When a user sends a resume
PDF we therefore *check first* and install the missing package into the very
interpreter that is running — no guessing which ``pip`` is on ``PATH`` and no
"pip install pypdf" instructions that hit the wrong Python.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Dependency:
    """One importable module that needs its own distribution."""

    module: str
    package: str
    why: str


#: packages that are declared as dependencies but are easy to end up without
DEPENDENCIES: tuple[Dependency, ...] = (Dependency("pypdf", "pypdf", "خواندن فایل PDF رزومه"),)


def has(module: str) -> bool:
    """True when ``import module`` would work (without importing it)."""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):  # broken/partial install
        return False


def missing() -> list[Dependency]:
    return [dep for dep in DEPENDENCIES if not has(dep.module)]


def interpreter() -> str:
    return sys.executable or "python"


def _quoted() -> str:
    exe = interpreter()
    if " " in exe and not exe.startswith('"'):
        exe = f'"{exe}"'
    return exe


def uv_available() -> bool:
    return shutil.which("uv") is not None


def has_pip() -> bool:
    """False in a uv-created venv: ``python -m pip`` answers «No module named pip»."""
    try:
        return importlib.util.find_spec("pip") is not None
    except (ImportError, ValueError):  # pragma: no cover - broken install
        return False


def install_command(package: str) -> str:
    """A copy-pasteable command that targets *this* interpreter.

    A venv created by ``uv`` has **no pip at all** — telling the user to run
    ``python -m pip install …`` there fails with «No module named pip», which is
    exactly what happened on a real Windows machine.
    """
    if not has_pip() and uv_available():
        return f"uv pip install --python {_quoted()} --upgrade {package}"
    return f"{_quoted()} -m pip install --upgrade {package}"


def editable_commands(project_root: str | Path) -> list[list[str]]:
    """Install *this repository* into the running interpreter, pip or not."""
    root = str(project_root)
    commands: list[list[str]] = []
    if has_pip():
        commands.append([interpreter(), "-m", "pip", "install", "-e", root])
    uv = shutil.which("uv")
    if uv:
        # the normal case for a uv venv: no pip, but uv is right there
        commands.append([uv, "pip", "install", "--python", interpreter(), "-e", root])
    if not has_pip():
        commands.append([interpreter(), "-m", "ensurepip", "--upgrade"])
        commands.append([interpreter(), "-m", "pip", "install", "-e", root])
    return commands


def editable_install_command(project_root: str | Path) -> str:
    """The one-line version of what ``editable_commands`` would run first."""
    commands = editable_commands(project_root)
    if not commands:  # pragma: no cover - pip and uv both missing
        return f"{_quoted()} -m ensurepip --upgrade"
    return " ".join(commands[0])


def install_editable(project_root: str | Path, *, timeout: float = 300.0) -> tuple[bool, str]:
    """``pip install -e <root>``, falling back to uv / ensurepip like ``install``."""
    last = ""
    for command in editable_commands(project_root):
        try:
            completed = subprocess.run(  # noqa: S603 - fixed, non-shell command
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            last = str(exc)
            continue
        output = ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()
        if completed.returncode == 0 and "ensurepip" not in command:
            return True, output[-500:]
        last = output[-500:] or f"کد خروج {completed.returncode}"
    return False, last


def _pip(package: str) -> list[str]:
    return [
        interpreter(),
        "-m",
        "pip",
        "install",
        "--upgrade",
        "--disable-pip-version-check",
        package,
    ]


def _commands(package: str) -> list[list[str]]:
    """Ways to install into *this* interpreter, most likely first."""
    commands = [_pip(package)]
    if uv_available():
        # a uv-managed venv usually has no pip at all
        commands.append([shutil.which("uv") or "uv", "pip", "install", "--python", interpreter(), package])
    # last resort: create pip inside the venv (works for uv-created ones too)
    commands.append([interpreter(), "-m", "ensurepip", "--upgrade"])
    commands.append(_pip(package))
    return commands


def install(package: str, *, timeout: float = 240.0) -> tuple[bool, str]:
    """Install ``package`` into the running interpreter.

    Returns ``(ok, tail_of_output)`` — never raises: a failed install is a normal
    outcome (offline machine, read-only venv) and the caller shows the manual
    command instead.
    """
    last = ""
    for command in _commands(package):
        try:
            completed = subprocess.run(  # noqa: S603 - fixed, non-shell command
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError:
            last = f"{command[0]} پیدا نشد"
            continue
        except subprocess.TimeoutExpired:
            last = f"نصب بیش از {int(timeout)} ثانیه طول کشید و رها شد."
            continue
        except OSError as exc:  # pragma: no cover - exotic environments
            last = f"اجرای نصب ممکن نشد: {exc}"
            continue
        output = ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()
        if completed.returncode == 0 and "ensurepip" not in command:
            return True, output[-500:]
        last = output[-500:] or f"کد خروج {completed.returncode}"
    return False, last


def ensure(module: str, *, timeout: float = 240.0) -> tuple[bool, str]:
    """Make ``module`` importable; a no-op when it already is."""
    if has(module):
        return True, ""
    dep = next((d for d in DEPENDENCIES if d.module == module), None)
    package = dep.package if dep else module
    ok, detail = install(package, timeout=timeout)
    if ok and has(module):
        return True, detail
    return False, detail


__all__ = [
    "DEPENDENCIES",
    "Dependency",
    "editable_commands",
    "editable_install_command",
    "ensure",
    "has",
    "has_pip",
    "install_editable",
    "install",
    "install_command",
    "interpreter",
    "missing",
    "uv_available",
]
