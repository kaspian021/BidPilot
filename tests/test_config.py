from pathlib import Path

from bidpilot.config import FiltersConfig, Profile, Secrets, deep_merge, load_config, load_yaml
from bidpilot.filters import ProjectFilter
from bidpilot.models import Budget, Project
from bidpilot.sources import available_sources, get_source_class
from bidpilot.sources.registry import create_source

ROOT = Path(__file__).resolve().parents[1]


def test_repo_config_loads():
    config = load_config(ROOT / "config" / "config.yaml")
    assert config.runtime.submit_mode in ("dry_run", "live")
    assert config.pricing["strategy"] == "midpoint_round"
    assert any(source.type == "parscoders" for source in config.sources)
    assert config.profile.full_name


def test_profile_file_loads():
    profile = Profile(**load_yaml(ROOT / "config" / "profile.yaml"))
    assert profile.stack
    assert profile.intro_line()


def test_deep_merge():
    merged = deep_merge({"a": {"b": 1, "c": 2}}, {"a": {"c": 3}})
    assert merged == {"a": {"b": 1, "c": 3}}


def test_registry_has_builtin_sources():
    sources = available_sources()
    assert "parscoders" in sources and "mock" in sources
    assert get_source_class("parscoders").__name__ == "ParscodersSource"


def test_create_source_reads_parscoders_yaml_defaults():
    config = load_config(ROOT / "config" / "config.yaml")
    source = create_source("parscoders", {}, config, Secrets())
    assert source.base_url == "https://parscoders.com"
    assert source.settings["list"]["path"] == "/project"


def make_project(**kwargs) -> Project:
    base = dict(source="mock", external_id="1", title="ساخت ربات تلگرام", description="ربات فروشگاهی")
    base.update(kwargs)
    return Project(**base)


def test_filter_rules():
    flt = ProjectFilter(
        FiltersConfig(
            include_keywords=["ربات"],
            exclude_keywords=["هک"],
            min_budget=400_000,
            max_budget=5_000_000,
        )
    )
    assert flt.evaluate(make_project(budget=Budget(min=500_000, max=1_500_000))).allowed
    assert not flt.evaluate(make_project(title="هک اکانت")).allowed
    assert not flt.evaluate(
        make_project(title="سایت وردپرس", description="طراحی سایت شرکتی")
    ).allowed  # include keyword missing
    assert not flt.evaluate(make_project(budget=Budget(min=100_000, max=200_000))).allowed
    assert not flt.evaluate(make_project(budget=Budget(min=9_000_000, max=10_000_000))).allowed


def test_profile_problems_detects_placeholders():
    from bidpilot.config import Profile

    assert Profile().problems()  # default profile is a placeholder
    good = Profile(
        first_name="سارا",
        full_name="سارا محمدی",
        role="برنامه‌نویس بک‌اند",
        stack=["Python"],
        highlights=["بک‌اند یک فروشگاه‌ساز"],
        years_experience=4,
    )
    assert good.problems() == []


def test_go_live_writes_local_override(tmp_path, monkeypatch):
    """go-live must never touch config.yaml — only the gitignored config.local.yaml."""
    import yaml
    from typer.testing import CliRunner

    from bidpilot.cli import app

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        (ROOT / "config" / "config.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (config_dir / "profile.yaml").write_text(
        (ROOT / "config" / "profile.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(app, ["go-live", "-c", str(config_dir / "config.yaml"), "--off"])
    assert result.exit_code == 0

    local = yaml.safe_load((config_dir / "config.local.yaml").read_text(encoding="utf-8"))
    assert local["runtime"]["submit_mode"] == "dry_run"
