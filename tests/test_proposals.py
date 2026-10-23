import httpx
import pytest
import respx

from bidpilot.config import AppConfig, Profile, ProposalConfig
from bidpilot.models import Budget, Project
from bidpilot.proposals.generator import ProposalError, ProposalGenerator, validate_proposal
from bidpilot.proposals.llm_client import LLMClient, LLMError

VALID_LLM_TEXT = (
    "سلام و وقت بخیر 🌱\n\n"
    "من محمد رضایی هستم، برنامه‌نویس فول‌استک با پنج سال تجربه در پایتون و FastAPI. "
    "ربات‌های تلگرامی فروشگاهی با درگاه پرداخت را قبلاً اجرا کرده‌ام و ساختار همین پروژه برایم آشناست.\n\n"
    "درباره‌ی پروژه: سه نکته‌ی اصلی می‌بینم. اول، جریان مکالمه و مدیریت وضعیت کاربر در ربات که اگر از ابتدا "
    "ماژولار طراحی نشود، اضافه‌کردن پنل مدیریت سخت می‌شود؛ راه‌حل: هندلرهای جدا و state در دیتابیس. "
    "دوم، اتصال به درگاه پرداخت و مدیریت تراکنش‌های نیمه‌کاره که با ثبت idempotent و لاگ کامل انجام می‌شود. "
    "سوم، همگام‌سازی محصولات ووکامرس با ربات که با REST API و کش زمان‌بندی‌شده بدون کندی سایت انجام می‌دهم.\n\n"
    "قیمت پیشنهادی من ۱,۰۰۰,۰۰۰ تومان و زمان تحویل ۱۴ روز است؛ در بازه‌ی اعلامی شما می‌مانم. "
    "اگر جزئیات را در چت روشن کنیم، همین هفته استارت می‌زنم و در پایان هر مرحله خروجی قابل بررسی تحویل می‌دهم.\n\n"
    "با احترام،\nمحمد رضایی"
)


def make_project() -> Project:
    return Project(
        source="mock",
        external_id="1",
        title="ساخت ربات تلگرام فروشگاهی",
        description="ربات تلگرام با کاتالوگ، سبد خرید و درگاه زرین‌پال، متصل به ووکامرس",
        budget=Budget(min=500_000, max=1_500_000),
        skills=["Python", "Telegram Bot API", "WooCommerce"],
    )


def make_config(**overrides) -> AppConfig:
    return AppConfig(
        profile=Profile(
            first_name="محمد",
            full_name="محمد رضایی",
            stack=["Python", "FastAPI", "React"],
            years_experience=5,
        ),
        proposal=ProposalConfig(**overrides),
        llm={"enabled": True, "base_url": "https://llm.test/v1", "model": "test-model"},
    )


def test_validation_flags_common_problems():
    config = ProposalConfig()
    assert validate_proposal("کوتاه", config, 1_000_000).ok is False
    assert validate_proposal(VALID_LLM_TEXT, config, 1_000_000).ok is True
    assert not validate_proposal("سلام " + "x" * 400, config).ok  # not Persian
    assert not validate_proposal(VALID_LLM_TEXT.replace("۱,۰۰۰,۰۰۰", "۹۹۹"), config, 1_000_000).ok
    assert not validate_proposal("**سلام** " + VALID_LLM_TEXT[10:], config, 1_000_000).ok


def test_template_generation_is_valid_and_project_aware():
    generator = ProposalGenerator(make_config())
    proposal = generator.generate_from_template(make_project(), 1_000_000, 14)

    assert proposal.generated_by == "template"
    assert proposal.text.startswith("سلام")
    assert "۱,۰۰۰,۰۰۰" in proposal.text or "1,000,000" in proposal.text
    assert "تومان" in proposal.text
    assert "ربات" in proposal.text
    assert "۱۴ روز" in proposal.text
    assert validate_proposal(proposal.text, ProposalConfig(), 1_000_000).ok


def test_template_mentions_detected_challenges():
    generator = ProposalGenerator(make_config())
    text = generator.generate_from_template(make_project(), 1_000_000, 14).text
    assert "درگاه پرداخت" in text  # payment playbook entry
    assert "ووکامرس" in text or "ووکامرس" in text.replace(" ", "")


@respx.mock
async def test_llm_generation_success():
    respx.post("https://llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "test-model-2026",
                "choices": [{"message": {"content": VALID_LLM_TEXT}}],
                "usage": {"total_tokens": 900},
            },
        )
    )
    llm = LLMClient("https://llm.test/v1", "key", "test-model")
    generator = ProposalGenerator(make_config(), llm=llm)
    proposal = await generator.generate(make_project(), 1_000_000, 14)

    assert proposal.generated_by == "llm:test-model-2026"
    assert proposal.usage["total_tokens"] == 900
    await generator.aclose()


@respx.mock
async def test_llm_invalid_then_repair():
    bad = "سلام، من برنامه‌نویس هستم. " * 5  # no price, too short
    route = respx.post("https://llm.test/v1/chat/completions")
    route.side_effect = [
        httpx.Response(200, json={"model": "m", "choices": [{"message": {"content": bad}}]}),
        httpx.Response(200, json={"model": "m", "choices": [{"message": {"content": VALID_LLM_TEXT}}]}),
    ]
    llm = LLMClient("https://llm.test/v1", "key", "test-model")
    generator = ProposalGenerator(make_config(), llm=llm)
    proposal = await generator.generate(make_project(), 1_000_000, 14)

    assert route.call_count == 2
    assert proposal.text == VALID_LLM_TEXT
    await generator.aclose()


@respx.mock
async def test_llm_failure_falls_back_to_template_when_configured():
    respx.post("https://llm.test/v1/chat/completions").mock(return_value=httpx.Response(500, text="boom"))
    llm = LLMClient("https://llm.test/v1", "key", "m", max_retries=0)
    generator = ProposalGenerator(make_config(on_generation_failure="template", max_chars=2_000), llm=llm)
    proposal = await generator.generate(make_project(), 1_000_000, 14)
    assert proposal.generated_by == "template"
    await generator.aclose()


@respx.mock
async def test_llm_failure_raises_when_skip_configured():
    respx.post("https://llm.test/v1/chat/completions").mock(return_value=httpx.Response(500, text="boom"))
    llm = LLMClient("https://llm.test/v1", "key", "m", max_retries=0)
    generator = ProposalGenerator(make_config(on_generation_failure="skip"), llm=llm)
    with pytest.raises(ProposalError):
        await generator.generate(make_project(), 1_000_000, 14)
    await generator.aclose()


async def test_llm_client_raises_without_key_and_endpoint():
    client = LLMClient("http://127.0.0.1:9/v1", None, "m", max_retries=0, timeout=0.2)
    with pytest.raises(LLMError):
        await client.complete("sys", "user")
    await client.aclose()
