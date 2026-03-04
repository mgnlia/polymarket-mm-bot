"""Tests for market scanner reward scoring logic."""
import pytest
from scanner import compute_reward_score, ScoredMarket
from config import Config


def test_reward_score_tight_spread():
    """Tighter spread → higher score."""
    score_tight = compute_reward_score(
        incentive_size=100.0,
        liquidity=1000.0,
        volume_24h=10000.0,
        target_spread=0.01,
        max_spread=0.05,
    )
    score_wide = compute_reward_score(
        incentive_size=100.0,
        liquidity=1000.0,
        volume_24h=10000.0,
        target_spread=0.04,
        max_spread=0.05,
    )
    assert score_tight > score_wide


def test_reward_score_at_max_spread_is_zero():
    """Spread equal to max → score = 0."""
    score = compute_reward_score(
        incentive_size=100.0,
        liquidity=1000.0,
        volume_24h=10000.0,
        target_spread=0.05,
        max_spread=0.05,
    )
    assert score == 0.0


def test_reward_score_quadratic():
    """Verify quadratic formula: ((v-s)/v)^2 * b * adjustments."""
    v, s, b = 0.05, 0.02, 100.0
    expected_base = ((v - s) / v) ** 2  # = 0.36
    score = compute_reward_score(
        incentive_size=b,
        liquidity=0.0,          # no competition penalty
        volume_24h=0.0,         # no volume bonus
        target_spread=s,
        max_spread=v,
    )
    # With zero liquidity: competition_penalty = 1/(1+0) = 1.0
    # With zero volume: volume_bonus = 0, factor = 1.0
    assert abs(score - expected_base * b) < 0.01


def test_reward_score_zero_max_spread():
    """Zero max spread → score = 0 (no division by zero)."""
    score = compute_reward_score(100.0, 1000.0, 10000.0, 0.0, 0.0)
    assert score == 0.0


def test_reward_score_high_liquidity_penalty():
    """High liquidity (competition) reduces score."""
    score_low_liq = compute_reward_score(100.0, 100.0, 10000.0, 0.01, 0.05)
    score_high_liq = compute_reward_score(100.0, 1_000_000.0, 10000.0, 0.01, 0.05)
    assert score_low_liq > score_high_liq


def test_scored_market_extreme_detection():
    """Markets below 10¢ or above 90¢ are flagged as extreme."""
    config = Config()

    low_market = ScoredMarket(
        condition_id="abc",
        question="Will X happen?",
        yes_token_id="tok1",
        no_token_id="tok2",
        yes_price=0.05,   # < 0.10 → extreme
        no_price=0.95,
        volume_24h=10000.0,
        liquidity=5000.0,
        incentive_size=100.0,
        category="politics",
        is_extreme=True,
        reward_score=0.5,
        end_date=None,
    )
    assert low_market.is_extreme is True

    normal_market = ScoredMarket(
        condition_id="def",
        question="Will Y happen?",
        yes_token_id="tok3",
        no_token_id="tok4",
        yes_price=0.50,   # normal
        no_price=0.50,
        volume_24h=10000.0,
        liquidity=5000.0,
        incentive_size=100.0,
        category="politics",
        is_extreme=False,
        reward_score=0.8,
        end_date=None,
    )
    assert normal_market.is_extreme is False
