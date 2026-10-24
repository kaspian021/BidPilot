from pathlib import Path

from typer.testing import CliRunner

from bidpilot.cli import app

ROOT = Path(__file__).resolve().parents[1]


def test_selftest_offline_runs_the_whole_pipeline_without_network(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["selftest", "--offline", "-c", str(ROOT / "config" / "config.yaml")])

    assert result.exit_code == 0, result.output
    assert "mock" in result.output
    assert "تولید پیشنهاد" in result.output
    assert "template" in result.output
    assert "خود برنامه سالم است" in result.output


def test_python_dash_m_entrypoint_works():
    """`python -m bidpilot` must be usable without `pip install -e`."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "bidpilot", "version"],
        capture_output=True,
        text=True,
        env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "src")},
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert "BidPilot" in result.stdout


def test_settings_sites_and_use_switch_the_active_site(tmp_path, monkeypatch):
    from bidpilot.credentials import save_site_credentials, site_credentials
    from bidpilot.settings_store import get_store, reset_store_cache

    monkeypatch.setenv("BIDPILOT_SETTINGS_FILE", str(tmp_path / "settings.json"))
    reset_store_cache()
    store = get_store()
    save_site_credentials(store, "parscoders", method="cookie", cookie="PHPSESSID=pars")

    runner = CliRunner()
    listed = runner.invoke(app, ["settings", "sites"])
    assert listed.exit_code == 0, listed.output
    assert "پارس‌کدرز" in listed.output and "پونیشا" in listed.output

    switched = runner.invoke(app, ["settings", "use", "ponisha"])
    assert switched.exit_code == 0, switched.output
    assert store.get("marketplace.name") == "ponisha"
    # the previous site keeps its own account
    assert site_credentials("parscoders", store).cookies == {"PHPSESSID": "pars"}

    unknown = runner.invoke(app, ["settings", "use", "nope"])
    assert unknown.exit_code != 0
    assert store.get("marketplace.name") == "ponisha"


def test_selftest_rejects_an_unregistered_site(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        app, ["selftest", "--offline", "-s", "nope", "-c", str(ROOT / "config" / "config.yaml")]
    )
    assert result.exit_code != 0
    assert "ثبت نشده" in result.output
