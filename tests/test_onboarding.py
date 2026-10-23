import pytest

from bidpilot import onboarding
from bidpilot.settings_store import SettingsStore


def make_store(tmp_path) -> SettingsStore:
    return SettingsStore(tmp_path / "data" / "settings.json")


def question(store, key):
    return next(q for q in onboarding.all_questions(store) if q.key == key)


def test_fresh_catalogue_order_and_credentials_keys(tmp_path):
    store = make_store(tmp_path)
    keys = [q.store_key(store) for q in onboarding.all_questions(store)]
    assert keys[:5] == [
        "telegram.bot_token",
        "telegram.chat_ids",
        "llm.provider",
        "llm.api_key",
        "marketplace.name",
    ]
    assert "credentials.parscoders.username" in keys
    assert "proposal.tone" in keys


def test_parse_choice_by_index_value_and_custom_tone(tmp_path):
    store = make_store(tmp_path)
    tone = question(store, "proposal.tone")

    assert onboarding.parse_answer(tone, "1", store) == onboarding.TONE_CHOICES[0].value
    assert onboarding.parse_answer(tone, onboarding.TONE_CHOICES[2].value, store) == (
        onboarding.TONE_CHOICES[2].value
    )
    custom = "با لحن یک مشاور سخت‌گیر اما مؤدب"
    assert onboarding.parse_answer(tone, custom, store) == custom
    invalid_choice = next(q for q in onboarding.mode_questions(store))
    with pytest.raises(onboarding.QuestionError):
        onboarding.parse_answer(invalid_choice, "گزینه‌ای که وجود ندارد", store)


def test_persian_numbers_and_lists(tmp_path):
    store = make_store(tmp_path)
    years = question(store, "profile.years_experience")
    stack = question(store, "profile.stack")
    assert onboarding.parse_answer(years, "۷", store) == 7
    assert onboarding.parse_answer(stack, "Python، Django, React", store) == [
        "Python",
        "Django",
        "React",
    ]
    with pytest.raises(onboarding.QuestionError):
        onboarding.parse_answer(years, "خیلی زیاد", store)


def test_bot_token_validation(tmp_path):
    store = make_store(tmp_path)
    token = question(store, "telegram.bot_token")
    with pytest.raises(onboarding.QuestionError):
        onboarding.parse_answer(token, "not-a-token", store)
    good = "123456789:AAHabcdefghijklmnopqrstuvwxyz012345"
    assert onboarding.parse_answer(token, good, store) == good


def test_apply_answer_syncs_provider_key_and_model(tmp_path):
    store = make_store(tmp_path)
    provider = question(store, "llm.provider")
    key = question(store, "llm.api_key")

    onboarding.apply_answer(store, provider, "gemini")
    assert store.get("llm.provider") == "gemini"
    from bidpilot.llm_providers import PRESETS

    assert store.get("llm.model") == PRESETS["gemini"].model

    onboarding.apply_answer(store, key, "gemini-key")
    assert store.get("llm.api_key") == "gemini-key"
    assert store.get("llm.keys.gemini") == "gemini-key"

    # switching back must not reuse the Gemini key for Groq
    onboarding.apply_answer(store, provider, "groq")
    assert store.get("llm.api_key") is None
    assert store.get("llm.keys.gemini") == "gemini-key"


def test_apply_answer_route_based_credential_keys(tmp_path):
    store = make_store(tmp_path)
    onboarding.apply_answer(store, question(store, "marketplace.name"), "parscoders")
    onboarding.apply_answer(store, question(store, "credentials.method"), "cookie")
    cookie_q = next(q for q in onboarding.all_questions(store) if q.store_key(store).endswith(".cookie"))
    onboarding.apply_answer(store, cookie_q, "PHPSESSID=xyz")
    assert store.get("credentials.parscoders.cookie") == "PHPSESSID=xyz"


def test_optional_question_can_be_skipped(tmp_path):
    store = make_store(tmp_path)
    style = next(q for q in onboarding.style_questions(store) if q.key == "proposal.style_instructions")
    assert onboarding.parse_answer(style, "/skip", store) is onboarding.KEEP
    assert onboarding.parse_answer(style, "کوتاه و بدون تعارف", store) == "کوتاه و بدون تعارف"


def test_readiness_and_status(tmp_path):
    store = make_store(tmp_path)
    state = onboarding.readiness(store)
    assert state["telegram"] is False
    assert state["llm"] is False
    assert state["marketplace"] == "parscoders"
    assert onboarding.missing_steps(store)

    store.update(
        {
            "telegram.bot_token": "1:x",
            "telegram.chat_ids": ["42"],
            "llm.provider": "groq",
            "llm.api_key": "k",
            "credentials.parscoders.username": "u",
            "credentials.parscoders.password": "p",
            "proposal.tone": "رسمی",
            "setup.completed": True,
        }
    )
    state = onboarding.readiness(store)
    assert state["telegram"] and state["llm"] and state["credentials"]
    assert "Groq" in onboarding.status_text(store)


def test_terminal_wizard_saves_answers(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    answers = iter(
        [
            "123456789:AAHabcdefghijklmnopqrstuvwxyz012345",  # bot token
            "987654",  # chat id
            "2",  # provider -> gemini
            "key-123",  # api key
            "2",  # marketplace -> parscoders (sorted choices: mock, parscoders)
            "1",  # credentials method
            "ali",  # username
            "secret",  # password
            "علی",  # first name
            "علی رضایی",  # full name
            "برنامه‌نویس بک‌اند",  # role
            "7",  # years
            "Python, Django",  # stack
            "فروشگاه‌ساز, ربات",  # highlights
            "/skip",  # portfolio
            "/skip",  # signature
            "1",  # tone
            "روی تحویل مرحله‌ای تأکید کن.",  # style
            "1200",  # max chars
            "1",  # dry-run
        ]
    )
    monkeypatch.setattr("rich.prompt.Prompt.ask", lambda *a, **k: next(answers))

    async def fake_verify(provider, key):
        return True, "کلید سالم است"

    async def fake_send(token, chat_ids):
        return "پیام تست ارسال شد"

    monkeypatch.setattr(onboarding, "_verify_key", fake_verify)
    monkeypatch.setattr(onboarding, "_send_test_message", fake_send)

    from rich.console import Console

    assert onboarding.run_terminal_wizard(store, Console(record=True)) is True
    assert store.get("telegram.bot_token").startswith("123456789:")
    assert store.get("telegram.chat_ids") == ["987654"]
    assert store.get("llm.provider") == "gemini"
    assert store.get("llm.api_key") == "key-123"
    assert store.get("credentials.parscoders.username") == "ali"
    assert store.get("profile.stack") == ["Python", "Django"]
    assert store.get("proposal.tone") == onboarding.TONE_CHOICES[0].value
    assert store.get("runtime.submit_mode") == "dry_run"
    assert store.get("setup.completed") is True


def test_llm_is_recommended_not_required_for_setup_completion(tmp_path):
    store = make_store(tmp_path)
    store.update(
        {
            "telegram.bot_token": "1:x",
            "telegram.chat_ids": ["42"],
            "credentials.parscoders.username": "u",
            "credentials.parscoders.password": "p",
            "profile.full_name": "سارا محمدی",
        }
    )
    assert onboarding.missing_steps(store, include_recommended=False) == []
    assert onboarding.missing_steps(store)  # the optional AI key is still suggested

    onboarding.mark_setup_complete(store)
    assert onboarding.readiness(store)["setup_completed"] is True
