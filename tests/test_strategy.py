import pytest

from bidpilot.models import Budget, Project
from bidpilot.pricing.strategy import (
    PricingConfig,
    build_strategy,
    nice_round,
)


def make_project(budget: Budget | None) -> Project:
    return Project(source="mock", external_id="1", title="ربات تلگرام", budget=budget)


def test_user_example_range_bids_one_million():
    """۵۰۰ هزار تا ۱.۵ میلیون -> پیشنهاد ۱ میلیون تومان."""
    budget = Budget(min=500_000, max=1_500_000)
    result = build_strategy("midpoint_round").price(make_project(budget), budget)
    assert result is not None
    assert result.amount == 1_000_000
    assert result.within_range


@pytest.mark.parametrize("ratio", [0.0, 0.25, 0.5, 0.75, 1.0])
def test_always_inside_employer_range(ratio):
    budget = Budget(min=300_000, max=700_000)
    result = build_strategy("midpoint_round", ratio=ratio).price(make_project(budget), budget)
    assert result is not None
    assert 300_000 <= result.amount <= 700_000


def test_no_budget_uses_default():
    result = build_strategy("midpoint_round", default_amount=1_500_000).price(make_project(None), None)
    assert result is not None
    assert result.amount == 1_500_000
    assert not result.within_range


def test_fixed_strategy_is_clamped():
    budget = Budget(min=500_000, max=900_000)
    result = build_strategy("fixed", amount=5_000_000).price(make_project(budget), budget)
    assert result is not None and result.amount == 900_000


def test_single_value_budget():
    budget = Budget(min=750_000, max=750_000)
    result = build_strategy("aggressive_low").price(make_project(budget), budget)
    # Fixed budget → ratio acts as discount (aggressive_low = 15% of fixed)
    assert result is not None and result.amount == 110_000
    assert result.amount <= budget.max  # bidding below client's fixed budget is allowed


def test_single_value_budget_half_discount():
    budget = Budget(min=5_000_000, max=5_000_000)
    result = build_strategy("midpoint_round", ratio=0.5).price(make_project(budget), budget)
    assert result is not None and result.amount == 2_500_000


def test_single_value_budget_full_price():
    budget = Budget(min=500_000, max=500_000)
    result = build_strategy("midpoint_round", ratio=1.0).price(make_project(budget), budget)
    assert result is not None and result.amount == 500_000


def test_nice_round():
    # two significant digits, never below min_step
    assert nice_round(1_234_000) == 1_200_000
    assert nice_round(987_654) == 990_000
    assert nice_round(5_000) == 5_000
    assert nice_round(1_000_000) == 1_000_000


def test_unknown_strategy_raises():
    with pytest.raises(ValueError):
        build_strategy("does-not-exist")


def test_pricing_config_builds():
    config = PricingConfig(strategy="premium_high", ratio=0.85)
    strategy = config.build()
    budget = Budget(min=1_000_000, max=2_000_000)
    result = strategy.price(make_project(budget), budget)
    assert result is not None and result.amount >= 1_700_000
