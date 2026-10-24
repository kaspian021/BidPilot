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


def install_command(package: str) -> str:
    """A copy-pasteable command that targets *this* interpreter."""
    exe = interpreter()
    if " " in exe and not exe.startswith('"'):
        exe = f'"{exe}"'
    return f"{exe} -m pip install --upgrade {package}"


def uv_available() -> bool:
    return shutil.which("uv") is not None


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
    "ensure",
    "has",
    "install",
    "install_command",
    "interpreter",
    "missing",
    "uv_available",
]
