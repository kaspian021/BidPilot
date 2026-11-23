"""Identity lock: the user's name is copied verbatim, never re-invented by the LLM."""

from bidpilot.config import AppConfig, Profile, ProposalConfig
from bidpilot.identity import (
    canonical_signature,
    enforce_identity,
    identity_issues,
    lock_extracted_name,
)
from bidpilot.models import Budget, Project
from bidpilot.profile_import import clean_fields
from bidpilot.proposals.generator import ProposalGenerator, validate_proposal
from bidpilot.proposals.llm_client import LLMResponse

PROFILE = Profile(
    first_name="محمد",
    full_name="محمد رضایی",
    role="برنامه‌نویس فول‌استک",
    stack=["Python", "FastAPI"],
    years_experience=5,
    highlights=["ربات تلگرامی فروشگاهی"],
)

BODY = (
    "این پروژه را با معماری ماژولار انجام می‌دهم: هندلرهای جدا، وضعیت در دیتابیس و "
    "اتصال امن به درگاه پرداخت با ثبت تراکنش‌های idempotent. تحویل مرحله‌ای است و در پایان "
    "هر مرحله خروجی قابل بررسی می‌دهم تا اگر جایی نظرتان عوض شد همان‌جا اصلاح کنیم."
)


def _text(intro_name: str, signature_name: str) -> str:
    return (
        "سلام و وقت بخیر\n\n"
        f"من {intro_name} هستم، برنامه‌نویس فول‌استک با پنج سال تجربه در Python و FastAPI. {BODY}\n\n"
        "قیمت پیشنهادی من ۱,۰۰۰,۰۰۰ تومان و زمان تحویل ۱۴ روز است.\n\n"
        f"با احترام،\n{signature_name}"
    )


def test_llm_typo_and_transliteration_are_replaced_by_the_exact_name():
    fixed = enforce_identity(_text("Mohammad Rezaei", "Mohamad Rezaee"), PROFILE)
    assert "Mohammad" not in fixed and "Mohamad" not in fixed
    assert "من محمد هستم" in fixed
    assert fixed.rstrip().endswith("محمد رضایی")
    assert identity_issues(fixed, PROFILE) == []


def test_arabic_script_variant_is_snapped_to_the_resume_spelling():
    # ي/ك (Arabic) instead of ی/ک (Persian): same-looking, not the stored name
    fixed = enforce_identity(_text("محمد", "محمد رضايي"), PROFILE)
    assert "رضایی" in fixed
    assert "رضايي" not in fixed


def test_missing_introduction_gets_the_canonical_one():
    text = "سلام و وقت بخیر\n\n" + BODY + "\n\nقیمت پیشنهادی من ۱,۰۰۰,۰۰۰ تومان و زمان تحویل ۱۴ روز است."
    fixed = enforce_identity(text, PROFILE)
    assert "من محمد هستم" in fixed[:400]
    assert fixed.rstrip().endswith("محمد رضایی")


def test_stale_signature_with_another_name_is_rebuilt():
    profile = Profile(
        first_name="سارا",
        full_name="سارا احمدی",
        signature="با احترام،\nمحمد رضایی",  # leftover from an old profile/template
    )
    assert canonical_signature(profile).rstrip().endswith("سارا احمدی")


def test_validation_rejects_a_name_that_is_not_the_stored_one():
    report = validate_proposal(_text("علی", "علی کریمی"), ProposalConfig(), 1_000_000, profile=PROFILE)
    assert not report.ok
    assert any("محمد رضایی" in issue for issue in report.issues)


class _StubLLM:
    def __init__(self, text: str) -> None:
        self._text = text

    async def complete(self, system, user, max_tokens=None):
        return LLMResponse(text=self._text, model="stub")

    async def aclose(self):
        return None


async def test_generator_output_always_carries_the_exact_name():
    project = Project(
        source="mock",
        external_id="1",
        title="ساخت ربات تلگرام فروشگاهی",
        description="ربات تلگرام با درگاه پرداخت",
        budget=Budget(min=500_000, max=1_500_000),
        skills=["Python"],
    )
    config = AppConfig(profile=PROFILE, proposal=ProposalConfig(on_generation_failure="skip"))
    generator = ProposalGenerator(config, llm=_StubLLM(_text("Mohammad Rezaei", "M. Rezaee")))
    proposal = await generator.generate(project, 1_000_000, 14)
    assert "Mohammad" not in proposal.text
    assert "من محمد هستم" in proposal.text
    assert proposal.text.rstrip().endswith("محمد رضایی")
    await generator.aclose()


def test_template_signature_uses_the_exact_full_name():
    generator = ProposalGenerator(AppConfig(profile=PROFILE))
    project = Project(
        source="mock",
        external_id="1",
        title="ساخت ربات تلگرام",
        description="ربات تلگرام با درگاه پرداخت و کاتالوگ",
        budget=Budget(min=500_000, max=1_500_000),
        skills=["Python"],
    )
    text = generator.generate_from_template(project, 1_000_000, 14).text
    assert text.rstrip().endswith("محمد رضایی")


# ------------------------------------------------------------ import locking
def test_extracted_transliteration_is_snapped_to_the_resume_name():
    source = "محمد رضایی\nبرنامه‌نویس فول‌استک\n۵ سال تجربه با Python و Django"
    locked = lock_extracted_name({"full_name": "Mohammad Rezaei", "first_name": "Mohammad"}, source)
    assert locked["full_name"] == "محمد رضایی"
    assert locked["first_name"] == "محمد"


def test_extracted_english_name_stays_english():
    source = "Ali Eghbali\nSenior Flutter Developer\n5 years of experience"
    locked = lock_extracted_name({"full_name": "Ali Eghbali", "first_name": "Ali"}, source)
    assert locked["full_name"] == "Ali Eghbali"


def test_invented_name_is_dropped_when_the_source_has_none():
    locked = lock_extracted_name(
        {"full_name": "Invented Person", "first_name": "Invented", "role": "برنامه‌نویس"},
        "فقط مهارت‌ها: Python و Django و FastAPI برای ساخت بک‌اند",
    )
    assert "full_name" not in locked and "first_name" not in locked


def test_clean_fields_keeps_llm_name_until_the_lock_judges_it():
    fields = clean_fields({"full_name": "Mohammad Rezaei", "first_name": "Mohammad"})
    assert fields["full_name"] == "Mohammad Rezaei"  # locking happens in analyse_text
