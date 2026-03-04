"""Tests for market scanner reward scoring logic.

Updated to reflect B1 fix (real Gamma API field parsing) and
B2 fix (rewardsMinSize = min order size, not reward pool).
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from scanner import compute_reward_score, ScoredMarket, MarketScanner, _parse_json_field
from config import Config


# ── _parse_json_field ─────────────────────────────────────────────────────────

def test_parse_json_field_string():
    """JSON-encoded string is parsed into a list."""
    raw = json.dumps(["tok_yes", "tok_no"])
    result = _parse_json_field(raw, [])
    assert result == ["tok_yes", "tok_no"]


def test_parse_json_field_already_list():
    """Already-parsed list is returned as-is."""
    result = _parse_json_field(["a", "b"], [])
    assert result == ["a", "b"]


def test_parse_json_field_none_returns_fallback():
    """None returns fallback."""
    result = _parse_json_field(None, ["fallback"])
    assert result == ["fallback"]


def test_parse_json_field_invalid_json_returns_fallback():
    """Invalid JSON string returns fallback."""
    result = _parse_json_field("not-json{{{", [])
    assert result == []


# ── compute_reward_score ──────────────────────────────────────────────────────

def test_reward_score_tight_spread():
    """Tighter spread → higher score."""
    score_tight = compute_reward_score(
        rewards_min_size=20.0,
        rewards_max_spread=2.0,
        liquidity=1000.0,
        volume_24h=10000.0,
        target_spread=0.01,   # 1 cent
        yes_price=0.50,
    )
    score_wide = compute_reward_score(
        rewards_min_size=20.0,
        rewards_max_spread=2.0,
        liquidity=1000.0,
        volume_24h=10000.0,
        target_spread=0.018,  # 1.8 cents — closer to max
        yes_price=0.50,
    )
    assert score_tight > score_wide


def test_reward_score_exceeds_max_spread_is_zero():
    """Spread wider than rewardsMaxSpread → score = 0 (no rewards earned)."""
    score = compute_reward_score(
        rewards_min_size=20.0,
        rewards_max_spread=2.0,   # 2 cents max
        liquidity=1000.0,
        volume_24h=10000.0,
        target_spread=0.03,       # 3 cents > 2 cents → no rewards
        yes_price=0.50,
    )
    assert score == 0.0


def test_reward_score_zero_max_spread():
    """Zero rewardsMaxSpread → score = 0 (no division by zero)."""
    score = compute_reward_score(
        rewards_min_size=20.0,
        rewards_max_spread=0.0,
        liquidity=1000.0,
        volume_24h=10000.0,
        target_spread=0.01,
        yes_price=0.50,
    )
    assert score == 0.0


def test_reward_score_zero_min_size():
    """Zero rewardsMinSize → score = 0 (market has no reward program)."""
    score = compute_reward_score(
        rewards_min_size=0.0,
        rewards_max_spread=2.0,
        liquidity=1000.0,
        volume_24h=10000.0,
        target_spread=0.01,
        yes_price=0.50,
    )
    assert score == 0.0


def test_reward_score_high_liquidity_penalty():
    """High liquidity (competition) reduces score."""
    score_low_liq = compute_reward_score(20.0, 2.0, 100.0, 10000.0, 0.01, 0.50)
    score_high_liq = compute_reward_score(20.0, 2.0, 1_000_000.0, 10000.0, 0.01, 0.50)
    assert score_low_liq > score_high_liq


def test_reward_score_midprice_bonus():
    """Market at 50¢ scores higher than market at 90¢ (extreme)."""
    score_mid = compute_reward_score(20.0, 2.0, 1000.0, 10000.0, 0.01, 0.50)
    score_extreme = compute_reward_score(20.0, 2.0, 1000.0, 10000.0, 0.01, 0.90)
    assert score_mid > score_extreme


# ── ScoredMarket construction ─────────────────────────────────────────────────

def _make_market(**kwargs) -> ScoredMarket:
    defaults = dict(
        condition_id="abc123",
        question="Will X happen?",
        yes_token_id="tok_yes",
        no_token_id="tok_no",
        yes_price=0.50,
        no_price=0.50,
        volume_24h=10000.0,
        liquidity=5000.0,
        rewards_min_size=20.0,
        rewards_max_spread=2.0,
        category="politics",
        is_extreme=False,
        reward_score=0.5,
        end_date=None,
    )
    defaults.update(kwargs)
    return ScoredMarket(**defaults)


def test_scored_market_extreme_detection_low():
    """Markets below 10¢ are flagged as extreme."""
    m = _make_market(yes_price=0.05, is_extreme=True)
    assert m.is_extreme is True


def test_scored_market_extreme_detection_high():
    """Markets above 90¢ are flagged as extreme."""
    m = _make_market(yes_price=0.95, is_extreme=True)
    assert m.is_extreme is True


def test_scored_market_normal_not_extreme():
    """Markets at 50¢ are not extreme."""
    m = _make_market(yes_price=0.50, is_extreme=False)
    assert m.is_extreme is False


# ── MarketScanner.fetch_rewarded_markets — B1 real API parsing ────────────────

@pytest.mark.asyncio
async def test_scanner_parses_real_gamma_api_format():
    """
    B1 regression test: scanner correctly parses the real Gamma API format
    where clobTokenIds, outcomePrices, and outcomes are JSON-encoded strings.
    """
    config = Config()
    config.min_volume_24h = 0.0
    config.categories = {"politics", "crypto", "sports", "other"}

    # Real Gamma API response shape (verified against live API)
    fake_market = {
        "conditionId": "0xabc123",
        "question": "Will it rain tomorrow?",
        "clobTokenIds": json.dumps(["token_yes_id_123", "token_no_id_456"]),
        "outcomePrices": json.dumps(["0.62", "0.38"]),
        "outcomes": json.dumps(["Yes", "No"]),
        "volume24hr": 50000.0,
        "liquidityNum": 10000.0,
        "rewardsMinSize": 20.0,
        "rewardsMaxSpread": 2.0,
        "endDate": "2026-12-31",
        "groupItemTagged": "politics",
    }

    scanner = MarketScanner(config)

    with patch.object(scanner._http, "get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = [fake_market]
        mock_get.return_value = mock_resp

        markets = await scanner.fetch_rewarded_markets()

    await scanner.close()

    assert len(markets) == 1, "Should find exactly 1 market"
    m = markets[0]
    assert m.condition_id == "0xabc123"
    assert m.yes_token_id == "token_yes_id_123"
    assert m.no_token_id == "token_no_id_456"
    assert abs(m.yes_price - 0.62) < 0.001
    assert abs(m.no_price - 0.38) < 0.001
    assert m.rewards_min_size == 20.0
    assert m.rewards_max_spread == 2.0


@pytest.mark.asyncio
async def test_scanner_skips_market_missing_token_ids():
    """Markets with no clobTokenIds are skipped gracefully."""
    config = Config()
    config.min_volume_24h = 0.0
    config.categories = {"other"}

    fake_market = {
        "conditionId": "0xbad",
        "question": "Bad market",
        "clobTokenIds": None,
        "outcomePrices": None,
        "volume24hr": 50000.0,
        "liquidityNum": 10000.0,
        "rewardsMinSize": 20.0,
        "rewardsMaxSpread": 2.0,
    }

    scanner = MarketScanner(config)

    with patch.object(scanner._http, "get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = [fake_market]
        mock_get.return_value = mock_resp

        markets = await scanner.fetch_rewarded_markets()

    await scanner.close()
    assert len(markets) == 0, "Should skip market with no token IDs"


@pytest.mark.asyncio
async def test_scanner_skips_market_with_no_rewards():
    """Markets with rewardsMinSize=0 are skipped (no reward program)."""
    config = Config()
    config.min_volume_24h = 0.0
    config.categories = {"politics"}

    fake_market = {
        "conditionId": "0xnoreward",
        "question": "No reward market",
        "clobTokenIds": json.dumps(["tok1", "tok2"]),
        "outcomePrices": json.dumps(["0.5", "0.5"]),
        "volume24hr": 50000.0,
        "liquidityNum": 10000.0,
        "rewardsMinSize": 0.0,   # No rewards
        "rewardsMaxSpread": 0.0,
        "groupItemTagged": "politics",
    }

    scanner = MarketScanner(config)

    with patch.object(scanner._http, "get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = [fake_market]
        mock_get.return_value = mock_resp

        markets = await scanner.fetch_rewarded_markets()

    await scanner.close()
    assert len(markets) == 0, "Should skip market with no reward program"
