"""Tests for builder_rewards.py — volume tracking & reward parsing."""
from __future__ import annotations

import time

import pytest
import httpx
import pytest_asyncio

from builder_rewards import BuilderRewardsTracker, BuilderOrderRecord


class TestBuilderRewardsTracker:
    def setup_method(self):
        self.tracker = BuilderRewardsTracker(builder_key="0xTestBuilder123")

    def teardown_method(self):
        import asyncio
        asyncio.get_event_loop().run_until_complete(self.tracker.close())

    def test_record_order_increments_totals(self):
        self.tracker.record_attributed_order(
            order_id="order-001",
            condition_id="0xABC",
            token_id="0xTOK",
            side="BUY",
            price=0.55,
            size_usdc=50.0,
        )
        assert self.tracker.stats.total_orders == 1
        assert self.tracker.stats.total_volume_usdc == 50.0
        assert self.tracker.stats.current_week_volume_usdc == 50.0

    def test_record_multiple_orders_accumulates(self):
        for i in range(5):
            self.tracker.record_attributed_order(
                order_id=f"order-{i:03d}",
                condition_id="0xABC",
                token_id="0xTOK",
                side="BUY",
                price=0.50,
                size_usdc=20.0,
            )
        assert self.tracker.stats.total_orders == 5
        assert abs(self.tracker.stats.total_volume_usdc - 100.0) < 0.01

    def test_volume_by_market_tracked(self):
        self.tracker.record_attributed_order("o1", "0xMKT1", "0xTOK1", "BUY", 0.5, 30.0)
        self.tracker.record_attributed_order("o2", "0xMKT1", "0xTOK1", "SELL", 0.55, 25.0)
        self.tracker.record_attributed_order("o3", "0xMKT2", "0xTOK2", "BUY", 0.3, 40.0)
        assert abs(self.tracker.stats.volume_by_market["0xMKT1"] - 55.0) < 0.01
        assert abs(self.tracker.stats.volume_by_market["0xMKT2"] - 40.0) < 0.01

    def test_summary_returns_expected_keys(self):
        summary = self.tracker.summary()
        required_keys = [
            "builder_key", "tier", "leaderboard_rank",
            "total_volume_usdc", "total_orders_attributed",
            "total_rewards_usdc", "current_week",
            "top_markets_by_volume", "reward_history",
        ]
        for key in required_keys:
            assert key in summary, f"Missing key: {key}"

    def test_summary_builder_key_truncated(self):
        summary = self.tracker.summary()
        assert summary["builder_key"].endswith("...")
        assert len(summary["builder_key"]) < len("0xTestBuilder123")

    def test_summary_no_builder_key(self):
        tracker = BuilderRewardsTracker(builder_key="")
        summary = tracker.summary()
        assert summary["builder_key"] == "not_set"

    def test_parse_leaderboard_finds_rank(self):
        data = {
            "builders": [
                {"address": "0xOther1", "tier": "Gold"},
                {"address": "0xtestbuilder123", "tier": "Silver"},  # case-insensitive
                {"address": "0xOther2", "tier": "Bronze"},
            ]
        }
        self.tracker._parse_leaderboard(data)
        assert self.tracker.stats.leaderboard_rank == 2
        assert self.tracker.stats.tier == "Silver"

    def test_parse_leaderboard_not_found(self):
        data = {
            "builders": [
                {"address": "0xSomeoneElse", "tier": "Gold"},
            ]
        }
        self.tracker._parse_leaderboard(data)
        assert self.tracker.stats.leaderboard_rank is None

    def test_parse_builder_stats_rewards(self):
        data = {
            "total_rewards_usdc": "125.50",
            "tier": "Gold",
            "reward_history": [
                {
                    "week_start": "2026-06-01",
                    "week_end": "2026-06-07",
                    "volume_usdc": "50000",
                    "reward_usdc": "45.00",
                    "rank": 5,
                    "tier": "Silver",
                }
            ],
        }
        self.tracker._parse_builder_stats(data)
        assert abs(self.tracker.stats.total_rewards_usdc - 125.50) < 0.01
        assert self.tracker.stats.tier == "Gold"
        assert len(self.tracker.stats.reward_history) == 1
        assert self.tracker.stats.reward_history[0].rank == 5

    def test_current_week_start_is_monday(self):
        ts = BuilderRewardsTracker._current_week_start()
        from datetime import datetime, timezone
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        assert dt.weekday() == 0  # 0 = Monday
        assert dt.hour == 0
        assert dt.minute == 0


@pytest.mark.asyncio
async def test_fetch_builder_stats_no_key():
    """Without a builder key, fetch_builder_stats returns current stats without API call."""
    tracker = BuilderRewardsTracker(builder_key="")
    stats = await tracker.fetch_builder_stats()
    assert stats.total_orders == 0
    await tracker.close()
