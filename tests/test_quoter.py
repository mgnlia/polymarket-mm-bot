"""Tests for quoter — bid/ask calculation and reward scoring."""
import pytest
from quoter import Quoter
from scanner import ScoredMarket
from config import Config


def _make_market(yes_price: float = 0.50, is_extreme: bool = False) -> ScoredMarket:
    return ScoredMarket(
        condition_id="test-cid-001",
        question="Test market?",
        yes_token_id="yes-tok",
        no_token_id="no-tok",
        yes_price=yes_price,
        no_price=1.0 - yes_price,
        volume_24h=50000.0,
        liquidity=10000.0,
        rewards_min_size=20.0,
        rewards_max_spread=2.0,
        category="politics",
        is_extreme=is_extreme,
        reward_score=0.72,
        end_date=None,
    )


def test_bid_below_ask():
    """Bid must always be below ask."""
    config = Config()
    quoter = Quoter(config)
    market = _make_market(0.50)
    quotes = quoter.build_quotes(market)
    assert quotes.yes_bid.price < quotes.yes_ask.price


def test_spread_within_max():
    """Actual spread must not exceed max_spread_pct."""
    config = Config()
    quoter = Quoter(config)
    market = _make_market(0.50)
    quotes = quoter.build_quotes(market)
    spread = quotes.yes_ask.price - quotes.yes_bid.price
    assert spread <= config.max_spread_pct + 0.001  # small float tolerance


def test_no_prices_complement_yes():
    """NO bid/ask should be complement of YES ask/bid."""
    config = Config()
    quoter = Quoter(config)
    market = _make_market(0.60)
    quotes = quoter.build_quotes(market)
    # NO bid = 1 - YES ask
    assert abs(quotes.no_bid.price - (1.0 - quotes.yes_ask.price)) < 0.001
    # NO ask = 1 - YES bid
    assert abs(quotes.no_ask.price - (1.0 - quotes.yes_bid.price)) < 0.001


def test_extreme_market_tighter_spread():
    """Extreme markets should use tighter (or equal) spread."""
    config = Config()
    quoter = Quoter(config)
    normal = _make_market(0.50, is_extreme=False)
    extreme = _make_market(0.05, is_extreme=True)
    q_normal = quoter.build_quotes(normal)
    q_extreme = quoter.build_quotes(extreme)
    spread_normal = q_normal.yes_ask.price - q_normal.yes_bid.price
    spread_extreme = q_extreme.yes_ask.price - q_extreme.yes_bid.price
    assert spread_extreme <= spread_normal + 0.001


def test_net_long_skews_ask_size_up():
    """When net long YES, ask size should be >= bid size (to reduce position)."""
    config = Config()
    quoter = Quoter(config)
    market = _make_market(0.50)
    quotes = quoter.build_quotes(market, net_position=config.max_position_per_market_usdc * 0.8)
    assert quotes.yes_ask.size >= quotes.yes_bid.size


def test_net_short_skews_bid_size_up():
    """When net short YES, bid size should be >= ask size (to reduce position)."""
    config = Config()
    quoter = Quoter(config)
    market = _make_market(0.50)
    quotes = quoter.build_quotes(market, net_position=-config.max_position_per_market_usdc * 0.8)
    assert quotes.yes_bid.size >= quotes.yes_ask.size


def test_reward_score_at_target_spread():
    """Reward score at target spread should be positive and < 1."""
    config = Config()
    quoter = Quoter(config)
    score = quoter.compute_reward_score(config.target_spread_pct)
    assert 0.0 < score < 1.0


def test_reward_score_at_max_spread_is_zero():
    """Reward score at max spread = 0."""
    config = Config()
    quoter = Quoter(config)
    score = quoter.compute_reward_score(config.max_spread_pct)
    assert score == 0.0
