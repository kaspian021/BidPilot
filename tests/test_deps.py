"""Optional packages: never tell the user «pip install pypdf» and hope."""

from __future__ import annotations

import subprocess
import sys

from bidpilot import deps


def test_install_command_targets_the_running_interpreter():
    command = deps.install_command("pypdf")

    assert sys.executable in command
    assert "-m pip install" in command
    assert "pypdf" in command


def test_missing_reports_only_uninstalled_packages(monkeypatch):
    monkeypatch.setattr(deps, "has", lambda module: module == "pypdf")
    assert deps.missing() == []

    monkeypatch.setattr(deps, "has", lambda module: False)
    assert [dep.module for dep in deps.missing()] == ["pypdf"]


def test_install_reports_success(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):  # noqa: ANN001
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "Successfully installed pypdf", "")

    monkeypatch.setattr(deps.subprocess, "run", fake_run)
    monkeypatch.setattr(deps, "uv_available", lambda: False)

    ok, detail = deps.install("pypdf")

    assert ok is True
    assert "Successfully installed" in detail
    assert calls[0][:4] == [sys.executable, "-m", "pip", "install"]


def test_install_falls_back_to_ensurepip_then_retries(monkeypatch):
    """A uv-created venv has no pip; ensurepip creates it, then pip works."""
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):  # noqa: ANN001
        commands.append(command)
        if "ensurepip" in command:
            return subprocess.CompletedProcess(command, 0, "pip installed", "")
        if command[1:3] == ["-m", "pip"] and not any("ensurepip" in c for c in commands[:-1]):
            return subprocess.CompletedProcess(command, 1, "", "No module named pip")
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr(deps.subprocess, "run", fake_run)
    monkeypatch.setattr(deps, "uv_available", lambda: False)

    ok, _ = deps.install("pypdf")

    assert ok is True
    assert any("ensurepip" in command for command in commands)


def test_install_failure_never_raises(monkeypatch):
    def fake_run(command, **kwargs):  # noqa: ANN001
        return subprocess.CompletedProcess(command, 1, "", "network is unreachable")

    monkeypatch.setattr(deps.subprocess, "run", fake_run)
    monkeypatch.setattr(deps, "uv_available", lambda: False)

    ok, detail = deps.install("pypdf")

    assert ok is False
    assert "network" in detail


def test_install_handles_a_missing_interpreter(monkeypatch):
    def fake_run(command, **kwargs):  # noqa: ANN001
        raise FileNotFoundError(command[0])

    monkeypatch.setattr(deps.subprocess, "run", fake_run)
    monkeypatch.setattr(deps, "uv_available", lambda: False)

    ok, detail = deps.install("pypdf")

    assert ok is False
    assert "پیدا نشد" in detail


def test_ensure_is_a_no_op_when_the_module_exists(monkeypatch):
    monkeypatch.setattr(deps, "has", lambda module: True)
    called: list[str] = []
    monkeypatch.setattr(deps, "install", lambda package, **kw: called.append(package) or (True, ""))

    ok, detail = deps.ensure("pypdf")

    assert ok is True and detail == ""
    assert called == []


# ---------------------------------------------------------------------------
# A uv-created venv has *no pip*: «python -m pip install -e .» cannot work
# ---------------------------------------------------------------------------
def test_the_suggested_command_matches_what_is_installed(monkeypatch):
    from bidpilot import deps

    monkeypatch.setattr(deps, "has_pip", lambda: False)
    monkeypatch.setattr(deps.shutil, "which", lambda name: r"C:\tools\uv.exe" if name == "uv" else None)

    assert deps.install_command("pypdf").startswith("uv pip install --python")


def test_editable_commands_fall_back_to_uv_then_ensurepip(monkeypatch, tmp_path):
    from bidpilot import deps

    monkeypatch.setattr(deps, "has_pip", lambda: False)
    monkeypatch.setattr(deps.shutil, "which", lambda name: "uv" if name == "uv" else None)

    commands = deps.editable_commands(tmp_path)
    rendered = [" ".join(command) for command in commands]

    assert any("uv pip install" in line and "-e" in line for line in rendered)
    # last resort: give the venv a pip first
    assert any("ensurepip" in line for line in rendered)
    assert not any("python -m pip install -e" in line for line in rendered if "ensurepip" not in line)


def test_editable_install_reports_success(monkeypatch, tmp_path):
    from bidpilot import deps

    calls: list[list[str]] = []

    class Done:
        returncode = 0
        stdout = "Successfully installed bidpilot"
        stderr = ""

    def fake_run(command, **_kwargs):
        calls.append(command)
        return Done()

    monkeypatch.setattr(deps.subprocess, "run", fake_run)
    monkeypatch.setattr(deps, "has_pip", lambda: True)

    ok, detail = deps.install_editable(tmp_path)

    assert ok and "Successfully" in detail
    assert calls and "-e" in calls[0]


def test_a_stale_install_suggests_a_command_that_works_without_pip(monkeypatch, tmp_path):
    from bidpilot import deps, runtime_info

    monkeypatch.setattr(deps, "has_pip", lambda: False)
    monkeypatch.setattr(deps.shutil, "which", lambda name: "uv" if name == "uv" else None)

    info = runtime_info.InstallInfo(
        version="0.1.0",
        package_file=tmp_path / "site-packages" / "bidpilot" / "__init__.py",
        project_root=tmp_path,
        repo_version="9.9.9",
    )

    assert "uv pip install" in info.fix_command()
