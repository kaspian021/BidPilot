from bidpilot.error_analyzer import analyze_error
from bidpilot.models import BidPlan, Project, SubmissionResult, SubmissionStatus
from bidpilot.notifications.formatting import _bid_block


def test_new_failure_messages_are_recognised():
    no_request = analyze_error("پیشنهاد ثبت نشد. سایت پس از کلیک پیام داد: «مدت کوتاه است».")
    assert no_request["category"] == "form_rejected"
    assert analyze_error("درخواست ثبت پیشنهاد فرستاده شد ولی سایت پاسخی نداد (تایم‌اوت شبکه).")["category"] == "timeout"
    assert analyze_error("sqlite3.OperationalError: table bids has no column named user_chat_id")["category"] == "local_db"


def test_failed_bid_message_in_telegram_carries_cause_and_action():
    project = Project(source="parscoders", external_id="1", title="پروژه")
    plan = BidPlan(
        project=project,
        amount=500_000,
        result=SubmissionResult(
            status=SubmissionStatus.FAILED,
            message="پس از کلیک هیچ درخواستی به سایت فرستاده نشد.",
        ),
    )
    block = _bid_block(plan)
    assert "🔍 علت:" in block and "🛠 اقدام:" in block


def test_ai_analysis_stored_on_the_result_is_preferred():
    project = Project(source="parscoders", external_id="1", title="پروژه")
    result = SubmissionResult(
        status=SubmissionStatus.FAILED,
        message="چیزی عجیب",
        payload_debug={"analysis": {"category": "llm_analyzed", "title": "سایت فرم را نپذیرفت", "action": "دستی بفرست"}},
    )
    block = _bid_block(BidPlan(project=project, amount=1, result=result))
    assert "سایت فرم را نپذیرفت" in block and "دستی بفرست" in block


async def test_unknown_failures_are_explained_once_by_the_llm_and_cached():
    from types import SimpleNamespace

    from bidpilot import pipeline as pipeline_module

    pipeline_module._AI_ANALYSIS_CACHE.clear()
    calls = {"n": 0}

    class FakeClient:
        async def complete(self, system, prompt, max_tokens=200):
            calls["n"] += 1
            return SimpleNamespace(text='```json\n{"title": "علت فارسی", "action": "اقدام فارسی"}\n```')

    fake_self = SimpleNamespace(generator=SimpleNamespace(_clients_ready=lambda: [(None, FakeClient())]))
    first = SubmissionResult(status=SubmissionStatus.FAILED, message="خطای عجیب شماره 123")
    second = SubmissionResult(status=SubmissionStatus.FAILED, message="خطای عجیب شماره 456")
    await pipeline_module.BidPipeline._attach_ai_analysis(fake_self, first)
    await pipeline_module.BidPipeline._attach_ai_analysis(fake_self, second)

    assert first.payload_debug["analysis"]["title"] == "علت فارسی"
    assert second.payload_debug["analysis"]["action"] == "اقدام فارسی"
    assert calls["n"] == 1  # same error signature -> one LLM call
    pipeline_module._AI_ANALYSIS_CACHE.clear()


async def test_known_failures_never_call_the_llm():
    from types import SimpleNamespace

    from bidpilot import pipeline as pipeline_module

    class Boom:
        async def complete(self, *args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("LLM must not be used for known errors")

    fake_self = SimpleNamespace(generator=SimpleNamespace(_clients_ready=lambda: [(None, Boom())]))
    result = SubmissionResult(status=SubmissionStatus.FAILED, message="اشتراک شما به پایان رسیده است")
    await pipeline_module.BidPipeline._attach_ai_analysis(fake_self, result)
    assert "analysis" not in result.payload_debug
