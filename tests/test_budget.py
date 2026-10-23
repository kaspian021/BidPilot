import pytest

from bidpilot.pricing.budget import format_toman, parse_budget


@pytest.mark.parametrize(
    "text,low,high",
    [
        ("بودجه: ۵۰۰,۰۰۰ تا ۱,۵۰۰,۰۰۰ تومان", 500_000, 1_500_000),
        ("500,000 - 1,500,000 تومان", 500_000, 1_500_000),
        ("۵۰۰ تا ۱۵۰۰ تومن", 500_000, 1_500_000),  # colloquial: thousand toman
        ("بودجه ۳۰۰ تا ۷۰۰ هزار تومان", 300_000, 700_000),
        ("۲ تا ۴ میلیون تومان", 2_000_000, 4_000_000),
        ("حداکثر ۱.۵ میلیون تومان", 1_500_000, 1_500_000),
        ("۵,۰۰۰,۰۰۰ ریال", 500_000, 500_000),
        ("۱۲۰۰۰۰۰ تومان", 1_200_000, 1_200_000),
    ],
)
def test_parse_ranges(text, low, high):
    budget = parse_budget(text)
    assert budget is not None
    assert budget.min == low
    assert budget.max == high
    assert budget.as_text()


def test_negotiable_budget():
    budget = parse_budget("بودجه: توافقی")
    assert budget is not None
    assert not budget.is_known
    assert budget.negotiable


def test_empty_input():
    assert parse_budget(None) is None
    assert parse_budget("") is None


def test_noise_guard():
    # a huge unrelated number should not become a budget
    budget = parse_budget("۱۲۳۴۵۶۷۸۹۰۱۲۳۴ تومان")
    assert budget is not None and not budget.is_known


def test_clamp():
    budget = parse_budget("۵۰۰,۰۰۰ تا ۱,۵۰۰,۰۰۰ تومان")
    assert budget.clamp(100) == 500_000
    assert budget.clamp(9_000_000) == 1_500_000
    assert budget.clamp(1_000_000) == 1_000_000


def test_format_toman():
    assert format_toman(1_000_000) == "1 میلیون تومان"
    assert format_toman(650_000) == "650,000 تومان"
    assert format_toman(None) == "—"
