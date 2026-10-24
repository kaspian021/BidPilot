import pytest

from bidpilot.app import Application
from bidpilot.config import AppConfig, FiltersConfig, Profile, RuntimeConfig, Secrets, SourceConfig
from bidpilot.models import Budget, Event, EventKind, Project
from bidpilot.notifications.base import Notifier
from bidpilot.repositories import Repository

PROJECTS = [
    {
        "external_id": "1",
        "title": "ساخت ربات تلگرام فروشگاهی",
        "description": "ربات تلگرام با کالای فروشگاهی، سبد خرید و درگاه پرداخت زرین‌پال",
        "budget": {"min": 500_000, "max": 1_500_000},
        "skills": ["Python", "Telegram Bot API"],
    },
    {
        "external_id": "2",
        "title": "سایت شرکتی وردپرس",
        "description": "سایت شش صفحه‌ای با قالب ریسپانسیو",
        "budget": {"min": 300_000, "max": 700_000},
        "skills": ["WordPress"],
    },
    {
        "external_id": "3",
        "title": "هک اکانت اینستاگرام",
        "description": "بازیابی اکانت",
        "budget": {"min": 100_000, "max": 200_000},
        "skills": [],
    },
]


class MemoryNotifier(Notifier):
    name = "memory"

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def send(self, event: Event) -> bool:
        self.events.append(event)
        return True

    def kinds(self, kind: EventKind) -> list[Event]:
        return [e for e in self.events if e.kind == kind]


def make_config(tmp_path, **runtime_overrides) -> AppConfig:
    runtime = dict(
        submit_mode="live",
        max_bids_per_hour=50,
        max_bids_per_day=100,
        min_seconds_between_bids=0,
        notify_on_skip=False,
        poll_interval_seconds=1,
        jitter_seconds=0,
    )
    runtime.update(runtime_overrides)
    return AppConfig(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.sqlite3'}",
        profile=Profile(first_name="محمد", full_name="محمد رضایی"),
        runtime=RuntimeConfig(**runtime),
        # the pipeline tests exercise pacing/submission, not the new relevance rules
        filters=FiltersConfig(
            exclude_keywords=["هک", "کپی سایت"],
            require_budget=False,
            require_skill_match=False,
        ),
        sources=[SourceConfig(type="mock", settings={"projects": PROJECTS, "latency": 0})],
        llm={"enabled": False},
    )


async def build_app(config: AppConfig) -> tuple[Application, MemoryNotifier]:
    application = Application.build(config, Secrets())
    notifier = MemoryNotifier()
    application.notifiers.notifiers = [notifier]
    await application.setup()
    return application, notifier


async def test_full_cycle_submits_bids_and_skips_blacklisted(tmp_path):
    application, notifier = await build_app(make_config(tmp_path))
    reports = await application.cycle()

    assert len(reports) == 1
    report = reports[0]
    assert report.fetched == 3
    assert report.submitted == 2
    assert report.skipped == 1  # the "هک" project

    submitted = notifier.kinds(EventKind.BID_SUBMITTED)
    assert len(submitted) == 2
    amounts = {event.bid.amount for event in submitted}
    assert amounts == {1_000_000, 500_000}

    await application.close()


async def test_second_cycle_does_not_rebid(tmp_path):
    application, _ = await build_app(make_config(tmp_path))
    first = (await application.cycle())[0]
    second = (await application.cycle())[0]

    assert first.submitted == 2
    assert second.submitted == 0
    assert second.new == 0
    await application.close()


async def test_dry_run_mode_sends_nothing(tmp_path):
    application, notifier = await build_app(make_config(tmp_path, submit_mode="dry_run"))
    report = (await application.cycle())[0]

    assert report.dry_run == 2
    assert report.submitted == 0
    assert len(notifier.kinds(EventKind.BID_DRY_RUN)) == 2
    await application.close()


async def test_rate_limit_caps_bids(tmp_path):
    application, _ = await build_app(make_config(tmp_path, max_bids_per_hour=1))
    report = (await application.cycle())[0]

    assert report.submitted == 1
    assert report.skipped == 2  # second project hits the hourly cap, third is filtered
    await application.close()


async def test_projects_and_bids_persisted(tmp_path):
    application, _ = await build_app(make_config(tmp_path))
    await application.cycle()

    session = application.db.session()
    repo = Repository(session)
    stats = await repo.stats()
    rows = await repo.recent_bids(limit=10)

    assert stats["projects"] == 3
    assert stats["submitted"] == 2
    filled = [row for row in rows if row.proposal_text]
    assert filled and filled[0].proposal_text.startswith("سلام")
    assert filled[0].pricing_rationale
    await session.close()
    await application.close()


async def test_source_failure_is_reported_without_crash(tmp_path):
    config = make_config(tmp_path)
    config.sources = [SourceConfig(type="mock", settings={"fixture_path": "/does/not/exist.json"})]
    application, notifier = await build_app(config)

    reports = await application.cycle()
    assert reports[0].errors
    assert notifier.kinds(EventKind.ERROR)
    await application.close()


@pytest.mark.parametrize("live", [True, False])
async def test_live_flag_override(tmp_path, live):
    application, _ = await build_app(make_config(tmp_path, submit_mode="live" if not live else "dry_run"))
    report = (await application.cycle(live=live))[0]
    assert (report.submitted > 0) is live
    await application.close()


async def test_duplicate_projects_are_not_counted_as_skipped(tmp_path):
    application, notifier = await build_app(make_config(tmp_path))
    await application.cycle()
    second = (await application.cycle())[0]

    assert second.duplicates == 3
    assert second.skipped == 0
    assert second.dry_run == 0
    await application.close()


async def test_error_notifications_are_throttled(tmp_path):
    config = make_config(tmp_path)
    config.sources = [SourceConfig(type="mock", settings={"fixture_path": "/nope.json"})]
    application, notifier = await build_app(config)

    await application.cycle()
    await application.cycle()

    # DB logs the error both times, Telegram only once per cooldown window
    assert len(notifier.kinds(EventKind.ERROR)) == 1
    await application.close()


def make_digest_config(tmp_path, **runtime_overrides) -> AppConfig:
    runtime = dict(notify_digest=True, notify_on_skip=True)
    runtime.update(runtime_overrides)
    config = make_config(tmp_path, **runtime)
    # only one project matters for the digest assertions
    config.sources = [SourceConfig(type="mock", settings={"projects": PROJECTS[:1], "latency": 0})]
    return config


async def test_one_digest_per_cycle_instead_of_one_message_per_project(tmp_path):
    application, notifier = await build_app(make_digest_config(tmp_path, submit_mode="dry_run"))
    report = (await application.cycle())[0]

    digests = notifier.kinds(EventKind.DIGEST)
    assert len(digests) == 1
    assert "گزارش mock" in digests[0].body
    assert "پروژه بررسی شد" in digests[0].body
    # no per-project «پروژه جدید» ping any more
    assert not notifier.kinds(EventKind.NEW_PROJECT)
    assert report.dry_run == 1
    await application.close()


async def test_digest_lists_why_projects_were_skipped(tmp_path):
    config = make_digest_config(tmp_path)
    config.sources = [SourceConfig(type="mock", settings={"projects": PROJECTS[2:], "latency": 0})]
    application, notifier = await build_app(config)
    report = (await application.cycle())[0]

    assert report.skipped == 1
    assert report.skip_reasons  # «کلیدواژهی ممنوعه پیدا شد: …»
    digest = notifier.kinds(EventKind.DIGEST)[0]
    assert "⏭" in digest.body
    assert any(reason in digest.body for reason in report.skip_reasons)
    await application.close()


async def test_digest_is_silent_when_nothing_happened(tmp_path):
    application, notifier = await build_app(make_digest_config(tmp_path))
    await application.cycle()
    notifier.events.clear()
    await application.cycle()  # everything is a duplicate now

    assert not notifier.kinds(EventKind.DIGEST)
    await application.close()


async def test_llm_failure_reason_reaches_the_report(tmp_path):
    from bidpilot.proposals.llm_client import LLMClient, LLMError

    class FailingClient(LLMClient):
        async def complete(self, system, user):  # type: ignore[override]
            raise LLMError("429 rate limit")

    config = make_config(tmp_path, submit_mode="dry_run")
    config.proposal.on_generation_failure = "template"
    config.llm.enabled = True
    application = Application.build(config, Secrets())
    notifier = MemoryNotifier()
    application.notifiers.notifiers = [notifier]
    application.generator = application.generator.__class__(
        config, llm=FailingClient("https://llm.test/v1", "k", "m")
    )
    await application.setup()
    report = (await application.cycle())[0]

    assert "429 rate limit" in report.llm_error
    await application.close()


async def test_employer_deadline_shortens_the_offer(tmp_path):
    """If the employer asks for 5 days, we never promise 14."""
    config = make_config(tmp_path, submit_mode="dry_run")
    config.sources = [
        SourceConfig(
            type="mock",
            settings={
                "projects": [
                    {
                        "external_id": "9",
                        "title": "ساخت اپلیکیشن Flutter",
                        "description": "فروشگاه موبایل",
                        "budget": {"min": 2_000_000, "max": 4_000_000},
                        "skills": ["Flutter"],
                        "employer_deadline": "۵ روز",
                    }
                ],
                "latency": 0,
            },
        )
    ]
    config.pricing = {**config.pricing, "delivery_days": 14}
    application, notifier = await build_app(config)
    report = (await application.cycle())[0]

    assert report.dry_run == 1 and report.proposed
    bid = notifier.kinds(EventKind.BID_DRY_RUN)[0].bid
    assert bid is not None and bid.delivery_days == 5
    assert "۵ روز" in bid.proposal.text or "5 روز" in bid.proposal.text
    await application.close()


async def test_digest_reports_ai_discovery_pages(tmp_path):
    config = make_digest_config(tmp_path, submit_mode="dry_run")
    application, notifier = await build_app(config)
    for source in application.sources.values():
        from bidpilot.sources.discovery import DiscoveryResult

        source.last_discovery = DiscoveryResult(pages_read=7, llm_calls=9)

    await application.cycle()

    digest = notifier.kinds(EventKind.DIGEST)[0].body
    assert "جست‌وجوی هوشمند" in digest
    assert "7" in digest
    await application.close()


async def test_live_pipeline_blocks_fetch_when_session_verification_fails(tmp_path):
    application, notifier = await build_app(make_config(tmp_path))
    source = application.sources["mock"]
    fetch_calls = {"count": 0}
    original_fetch = source.fetch_projects

    async def denied_session():
        return False, "کوکی منقضی شده است"

    async def track_fetch():
        fetch_calls["count"] += 1
        return await original_fetch()

    source.verify_session = denied_session
    source.fetch_projects = track_fetch
    report = (await application.cycle())[0]

    assert report.fetched == 0
    assert report.errors and "احراز هویت" in report.errors[0]
    assert fetch_calls["count"] == 0
    assert notifier.kinds(EventKind.ERROR)
    await application.close()


async def test_pipeline_revisits_skipped_project_when_new_skills_are_found(tmp_path):
    config = make_config(tmp_path)
    config.profile.stack = ["C#"]
    config.filters.require_skill_match = True
    config.filters.require_budget = True
    config.sources = [SourceConfig(type="mock", settings={"projects": [], "latency": 0})]
    application, _ = await build_app(config)
    source = application.sources["mock"]
    fetch_round = {"count": 0}

    async def changing_projects():
        fetch_round["count"] += 1
        skills = [] if fetch_round["count"] == 1 else ["برنامه نویسی با C#"]
        return [
            Project(
                source="mock",
                external_id="321",
                title="ابزار برنامه‌نویسی",
                description="ساخت ابزار",
                budget=Budget(min=500_000, max=1_500_000),
                skills=skills,
            )
        ]

    source.fetch_projects = changing_projects
    first = (await application.cycle())[0]
    second = (await application.cycle())[0]

    assert first.skipped == 1
    assert second.submitted == 1
    assert second.new == 0
    assert second.duplicates == 0
    await application.close()
