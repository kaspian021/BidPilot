from bidpilot.config import FiltersConfig, Profile
from bidpilot.filters import ProjectFilter, derive_skill_keywords, normalise_keyword
from bidpilot.models import Budget, Project

PROFILE = Profile(
    first_name="علی",
    full_name="علی اقبالی",
    role="برنامه‌نویس موبایل و بک‌اند",
    stack=["Flutter", "Dart", "ASP.NET Core", "C#", "SQL Server", "FastAPI"],
)


def make_project(title: str, description: str = "", *, budget: Budget | None = None) -> Project:
    return Project(
        source="mock",
        external_id="1",
        title=title,
        description=description,
        budget=budget,
        skills=[],
    )


def filtered() -> ProjectFilter:
    return ProjectFilter(
        FiltersConfig(),
        profile_skills=list(PROFILE.stack),
        profile_role=PROFILE.role,
    )


def test_keywords_come_from_the_profile():
    words = derive_skill_keywords(PROFILE.stack, PROFILE.role)

    assert "flutter" in words and "fastapi" in words
    # role words are split on separators but the generic ones are dropped
    assert "برنامه‌نویس" not in words
    assert normalise_keyword("ASP.NET  Core") == "asp.net core"


def test_a_project_without_a_budget_is_skipped_by_default():
    decision = filtered().evaluate(make_project("ساخت اپلیکیشن فلاتر"))

    assert not decision.allowed
    assert "بودجه" in decision.reason


def test_unknown_budget_is_allowed_when_the_rule_is_off():
    flt = ProjectFilter(
        FiltersConfig(require_budget=False),
        profile_skills=list(PROFILE.stack),
    )
    assert flt.evaluate(make_project("ساخت اپلیکیشن فلاتر")).allowed


def test_unrelated_projects_are_rejected():
    dec = filtered().evaluate(
        make_project("طراحی لوگو فروشگاه", "طراحی لوگو و بنر تبلیغاتی", budget=Budget(min=500_000))
    )

    assert not dec.allowed
    assert "مهارت" in dec.reason


def test_a_project_matching_an_alias_is_accepted():
    dec = filtered().evaluate(
        make_project("توسعه اپلیکیشن با فلاتر", "اپ موبایل", budget=Budget(min=2_000_000, max=4_000_000))
    )

    assert dec.allowed, dec.reason


def test_budget_range_and_excluded_words_still_work():
    flt = ProjectFilter(
        FiltersConfig(min_budget=1_000_000, max_budget=10_000_000, exclude_keywords=["هک"]),
        profile_skills=list(PROFILE.stack),
    )
    assert not flt.evaluate(make_project("ساخت اپ با Flutter", budget=Budget(min=200_000))).allowed
    assert not flt.evaluate(make_project("هک اکانت", "Flutter", budget=Budget(min=2_000_000))).allowed
    assert flt.evaluate(make_project("ساخت اپ با Flutter", budget=Budget(min=2_000_000))).allowed


def test_empty_profile_blocks_everything_until_it_is_filled():
    dec = ProjectFilter(FiltersConfig()).evaluate(make_project("ساخت اپ", budget=Budget(min=2_000_000)))

    assert not dec.allowed
    assert "/profile" in dec.reason


def test_explicit_keywords_override_the_profile():
    flt = ProjectFilter(
        FiltersConfig(skill_keywords=["وردپرس"]),
        profile_skills=list(PROFILE.stack),
    )

    assert flt.skill_keywords == ["وردپرس"]
    assert flt.describe_keywords() == "وردپرس"
