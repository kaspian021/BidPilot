import asyncio
from types import SimpleNamespace

from bidpilot.scheduler import Scheduler
from bidpilot.settings_store import SettingsStore


class FakeApp:
    def __init__(self, tag: int = 0) -> None:
        self.tag = tag
        self.closed = False
        self.setup_called = False
        self.config = SimpleNamespace(
            runtime=SimpleNamespace(poll_interval_seconds=60, jitter_seconds=0, live=False)
        )

    async def setup(self) -> None:
        self.setup_called = True

    async def close(self) -> None:
        self.closed = True


async def test_scheduler_reloads_only_when_settings_change(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    initial = FakeApp(tag=-1)
    created: list[FakeApp] = []

    def factory() -> FakeApp:
        app = FakeApp(tag=len(created))
        created.append(app)
        return app

    scheduler = Scheduler(initial, interval=60, jitter=0, app_factory=factory, store=store)
    assert await scheduler.reload_if_settings_changed() is False

    store.set("proposal.tone", "رسمی و کوتاه")
    assert await scheduler.reload_if_settings_changed() is True
    assert scheduler.app is created[-1]
    assert created[-1].setup_called and initial.closed

    assert await scheduler.reload_if_settings_changed() is False


async def test_scheduler_without_factory_never_reloads(tmp_path):
    app = FakeApp()
    scheduler = Scheduler(app, interval=60, jitter=0, store=SettingsStore(tmp_path / "s.json"))
    assert await scheduler.reload_if_settings_changed() is False
    assert scheduler.app is app


async def test_scheduler_stops_after_a_report_marks_authentication_failed(tmp_path):
    class AuthReport:
        source = "parscoders"
        authentication_failed = True
        errors = ["ورود به حساب تأیید نشد"]

        def summary(self):
            return "authentication failed"

    class AuthApp(FakeApp):
        def __init__(self):
            super().__init__()
            self.sources = {"parscoders": object()}
            self.cycles = 0

        async def cycle(self, sources=None):
            self.cycles += 1
            return [AuthReport()]

    app = AuthApp()
    callback_reports = []
    scheduler = Scheduler(
        app,
        interval=600,
        jitter=0,
        store=SettingsStore(tmp_path / "settings.json"),
        on_report=lambda reports: callback_reports.extend(reports),
    )

    await asyncio.wait_for(scheduler.run_forever(), timeout=2)

    assert app.cycles == 1
    assert len(callback_reports) == 1
    assert scheduler._stop.is_set()
