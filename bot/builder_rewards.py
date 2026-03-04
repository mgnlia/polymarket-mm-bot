"""
Builder Program volume & reward tracking.

Tracks orders attributed to the builder profile and queries the
Polymarket builders leaderboard for rank/tier/reward estimates.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import httpx

from builder_auth import get_builder_config, is_builder_enabled

logger = logging.getLogger(__name__)

LEADERBOARD_URL = "https://builders.polymarket.com/api/leaderboard"
REFRESH_INTERVAL_SECONDS = 3600  # refresh leaderboard every hour


@dataclass
class AttributedOrder:
    market_id: str
    side: str          # "BUY" | "SELL"
    size_usdc: float
    price: float
    timestamp: float = field(default_factory=time.time)


@dataclass
class LeaderboardEntry:
    rank: int
    tier: str
    total_volume_usdc: float
    weekly_rewards_usdc: float


class BuilderRewardsTracker:
    """Tracks attributed order volume and fetches leaderboard data."""

    def __init__(self) -> None:
        self._orders: List[AttributedOrder] = []
        self._leaderboard: Optional[LeaderboardEntry] = None
        self._last_refresh: float = 0.0

    # ── Order recording ───────────────────────────────────────────────────────

    def record_order(self, market_id: str, side: str, size_usdc: float, price: float) -> None:
        """Record an order that has been attributed to the builder profile."""
        self._orders.append(
            AttributedOrder(
                market_id=market_id,
                side=side,
                size_usdc=size_usdc,
                price=price,
            )
        )

    # ── Volume stats ──────────────────────────────────────────────────────────

    @property
    def total_orders(self) -> int:
        return len(self._orders)

    @property
    def total_volume_usdc(self) -> float:
        return sum(o.size_usdc for o in self._orders)

    def volume_by_market(self) -> Dict[str, float]:
        vol: Dict[str, float] = {}
        for o in self._orders:
            vol[o.market_id] = vol.get(o.market_id, 0.0) + o.size_usdc
        return vol

    def recent_orders(self, limit: int = 50) -> List[dict]:
        return [
            {
                "market_id": o.market_id,
                "side": o.side,
                "size_usdc": o.size_usdc,
                "price": o.price,
                "timestamp": o.timestamp,
            }
            for o in self._orders[-limit:]
        ]

    # ── Leaderboard ───────────────────────────────────────────────────────────

    async def refresh_leaderboard(self) -> None:
        """Fetch leaderboard data from builders.polymarket.com."""
        config = get_builder_config()
        if config is None:
            return

        now = time.time()
        if now - self._last_refresh < REFRESH_INTERVAL_SECONDS:
            return

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(LEADERBOARD_URL)
                resp.raise_for_status()
                data = resp.json()

            # The leaderboard API returns a list; find our entry by key
            key = getattr(config.local_builder_creds, "key", None) if config.local_builder_creds else None
            entry = None
            if key and isinstance(data, list):
                for item in data:
                    if item.get("address", "").lower() == key.lower():
                        entry = item
                        break

            if entry:
                self._leaderboard = LeaderboardEntry(
                    rank=entry.get("rank", 0),
                    tier=entry.get("tier", "Unknown"),
                    total_volume_usdc=float(entry.get("totalVolume", 0)),
                    weekly_rewards_usdc=float(entry.get("weeklyRewards", 0)),
                )
                logger.info(
                    "Builder leaderboard refreshed — rank=%s tier=%s",
                    self._leaderboard.rank,
                    self._leaderboard.tier,
                )
            else:
                logger.debug("Builder key not found in leaderboard response.")

            self._last_refresh = now

        except Exception as exc:
            logger.warning("Failed to refresh builder leaderboard: %s", exc)

    # ── Summary ───────────────────────────────────────────────────────────────

    def summary(self) -> dict:
        lb = self._leaderboard
        config = get_builder_config()
        key = (
            config.local_builder_creds.key
            if config and config.local_builder_creds
            else "not configured"
        )
        return {
            "builder_enabled": is_builder_enabled(),
            "builder_key": key,
            "total_orders_attributed": self.total_orders,
            "total_volume_usdc": round(self.total_volume_usdc, 2),
            "volume_by_market": self.volume_by_market(),
            "leaderboard_rank": lb.rank if lb else None,
            "tier": lb.tier if lb else None,
            "weekly_rewards_usdc": lb.weekly_rewards_usdc if lb else None,
            "total_rewards_usdc": lb.total_volume_usdc if lb else None,
        }


# ── Background refresh loop ───────────────────────────────────────────────────

async def leaderboard_refresh_loop(tracker: BuilderRewardsTracker) -> None:
    """Run as a background asyncio task — refreshes leaderboard hourly."""
    while True:
        await tracker.refresh_leaderboard()
        await asyncio.sleep(REFRESH_INTERVAL_SECONDS)
