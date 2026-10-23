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
