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


async def test_scheduler_pauses_javascript_source_instead_of_stopping(tmp_path):
    """A JavaScript interstitial is transient: pause the source, keep running.

    Regression test for the production incident where one such response
    killed the whole daemon (``Scheduler stopped.``) until a manual /run.
    """

    class JavaScriptReport:
        source = "parscoders"
        authentication_failed = False
        javascript_required = True
        errors = ["سایت JavaScript مرورگر را لازم دانست"]

        def summary(self):
            return "JavaScript required"

    class JavaScriptApp(FakeApp):
        def __init__(self):
            super().__init__()
            self.sources = {"parscoders": object()}
            self.cycles = 0
            self.seen_sources = []

        async def cycle(self, sources=None):
            self.cycles += 1
            self.seen_sources.append(sources)
            return [JavaScriptReport()]

    app = JavaScriptApp()
    callback_reports = []
    scheduler = Scheduler(
        app,
        interval=5,
        jitter=0,
        store=SettingsStore(tmp_path / "settings.json"),
        on_report=lambda reports: callback_reports.extend(reports),
        javascript_cooldown=1,
    )

    task = asyncio.create_task(scheduler.run_forever())
    try:
        # first cycle runs, the source gets paused, the scheduler stays alive
        for _ in range(100):
            if app.cycles >= 1 and "parscoders" in scheduler._paused_until:
                break
            await asyncio.sleep(0.05)
        assert app.cycles == 1
        assert len(callback_reports) == 1
        assert not scheduler._stop.is_set()
        assert not task.done()

        # after the short cooldown the very same source is polled again
        for _ in range(200):
            if app.cycles >= 2:
                break
            await asyncio.sleep(0.05)
        assert app.cycles >= 2
        # second failure doubles down the streak (exponential pause growth)
        assert scheduler._js_streak["parscoders"] >= 2
    finally:
        scheduler.request_stop()
        await asyncio.wait_for(task, timeout=5)
