import pytest

from bidpilot.models import SubmissionStatus
from bidpilot.sources.browser_submit import submit_js_managed_form


def browser_args(**overrides):
    values = {
        "source_name": "parscoders",
        "base_url": "https://parscoders.com",
        "page_url": "https://parscoders.com/project/12345/sample",
        "project_id": "12345",
        "cookies": {},
        "form_id": "init_message_form",
        "form_name": "init_message",
        "submitter_id": "msg_submit_btn",
        "endpoint_attribute": "data-href",
        "endpoint_url": "https://parscoders.com/conversation/ajax/bid-send/12345",
        "field_mapping": {"amount": "bidAmount", "days": "deadline", "description": "message"},
        "proposal_text": "متن آزمایشی",
        "amount": 500_000,
        "delivery_days": 14,
        "bid_cfg": {},
        "auth_cfg": {},
    }
    values.update(overrides)
    return values


@pytest.mark.asyncio
async def test_browser_submit_rejects_cross_origin_endpoint_without_request():
    result = await submit_js_managed_form(
        **browser_args(endpoint_url="https://example.invalid/conversation/ajax/bid-send/12345")
    )

    assert result.status is SubmissionStatus.FAILED
    assert result.payload_debug["request_sent"] is False
    assert "دامنه دیگری" in result.message


@pytest.mark.asyncio
async def test_browser_submit_rejects_endpoint_for_a_different_project():
    result = await submit_js_managed_form(
        **browser_args(endpoint_url="https://parscoders.com/conversation/ajax/bid-send/99999")
    )

    assert result.status is SubmissionStatus.FAILED
    assert result.payload_debug["request_sent"] is False
    assert "همین پروژه" in result.message
