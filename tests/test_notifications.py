from bidpilot.models import (
    BidPlan,
    Budget,
    Event,
    EventKind,
    Project,
    Proposal,
    SubmissionResult,
    SubmissionStatus,
)
from bidpilot.notifications.formatting import render_event
from bidpilot.notifications.telegram import TelegramNotifier, split_message


def make_event(kind=EventKind.BID_SUBMITTED) -> Event:
    project = Project(
        source="parscoders",
        external_id="42",
        title="ربات تلگرام <فروشگاهی>",
        url="https://parscoders.com/project/42",
        budget=Budget(min=500_000, max=1_500_000),
        skills=["Python"],
    )
    bid = BidPlan(
        project=project,
        amount=1_000_000,
        delivery_days=14,
        rationale="وسط بازه کارفرما",
        proposal=Proposal(text="سلام و وقت بخیر ..."),
        result=SubmissionResult(status=SubmissionStatus.SUBMITTED, message="ثبت شد"),
    )
    return Event(kind=kind, title=project.title, project=project, bid=bid)


def test_render_event_escapes_html_and_includes_key_fields():
    html = render_event(make_event())
    assert "&lt;فروشگاهی&gt;" in html
    assert "1 میلیون تومان" in html
    assert "ثبت شد" in html
    assert "متن پیشنهاد" in html


def test_render_event_without_proposal_body():
    event = make_event()
    html = render_event(event, full_proposal=False)
    assert "متن پیشنهاد" not in html
    assert "پیشنهاد ارسال شد" in html


def test_split_message_respects_limit():
    text = "\n".join(f"خط شماره {i} " + "x" * 100 for i in range(100))
    chunks = split_message(text, 500)
    assert len(chunks) > 1
    assert all(len(chunk) <= 500 for chunk in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_notifier_disabled_without_token():
    assert TelegramNotifier(None, ["123"]).enabled is False
    assert TelegramNotifier("token", []).enabled is False
    assert TelegramNotifier("token", ["123"]).enabled is True
