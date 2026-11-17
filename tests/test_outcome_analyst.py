from types import SimpleNamespace

from bidpilot.outcome_analyst import (
    analyze_outcome,
    classify_rules,
    evidence_from_html,
    seconds_until_site_reset,
)
from bidpilot.sources.browser_submit import limit_result


class FakeLLM:
    def __init__(self, text):
        self.text, self.calls = text, 0

    async def complete(self, system, prompt, max_tokens=200):
        self.calls += 1
        return SimpleNamespace(text=self.text)


def test_site_reset_is_between_half_an_hour_and_a_day():
    assert 30 * 60 <= seconds_until_site_reset() <= 25 * 3600


def test_daily_quota_pauses_the_site_but_expiry_does_not_resume_by_itself():
    daily = classify_rules("", {"notices": ["شما امروز فقط یک پیشنهاد می‌توانید ارسال کنید - محدودیت روزانه"]})
    assert daily.kind == "daily_limit" and daily.pauses_site and daily.wait_seconds >= 1800
    expired = classify_rules("اشتراک شما به پایان رسیده است")
    assert expired.kind == "subscription" and not expired.pauses_site and expired.retry == "never"


def test_limit_result_is_worded_for_the_site_and_never_claims_a_free_account():
    daily = limit_result("ponisha", "محدودیت روزانه", True, 200, {})
    assert daily.retry_after_seconds > 0 and not daily.subscription_required
    assert "پونیشا" in daily.message and "رایگان" not in daily.message
    expired = limit_result("karlancer", "اشتراک تمام شد", False, 200, {})
    assert expired.subscription_required and expired.retry_after_seconds == 0
    assert "کارلنسر" in expired.message


async def test_ai_reads_the_page_evidence_for_unknown_messages_and_is_cached():
    llm = FakeLLM(
        '{"kind": "daily_limit", "title": "سقف امروز پر شد", "explanation": "سایت فقط ۳ پیشنهاد در روز می‌پذیرد",'
        ' "action": "فردا ادامه می‌دهد", "retry": "later", "wait_minutes": 600}'
    )
    evidence = {"notices": ["ظرفیت شما برای این بازهٔ زمانی تکمیل است"], "excerpt": ""}
    out = await analyze_outcome("پاسخ نامشخص ۱۲۳", site="ponisha", evidence=evidence, llm_client=llm)
    assert out.source == "ai" and out.kind == "daily_limit" and out.wait_seconds == 600 * 60
    await analyze_outcome("پاسخ نامشخص ۴۵۶", site="ponisha", evidence=evidence, llm_client=llm)
    assert llm.calls == 1


async def test_ai_cannot_invent_a_pause_without_evidence():
    llm = FakeLLM('{"kind": "daily_limit", "title": "سقف", "retry": "later", "wait_minutes": 600}')
    out = await analyze_outcome("خطای عجیب بدون هیچ شاهدی xyz", site="x", evidence=None, llm_client=llm)
    assert out is None or not out.pauses_site


def test_evidence_extracts_clean_text():
    ev = evidence_from_html("<html><body><p>سلام   دنیا</p><script>x()</script></body></html>")
    assert "سلام دنیا" in ev["excerpt"]
