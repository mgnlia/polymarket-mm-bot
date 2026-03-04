"""
builder_rewards.py — Builder Program volume & rewards tracker.

Tracks:
- Orders attributed to our builder profile
- Weekly USDC rewards from the Builder Program
- Volume breakdown by market
- Leaderboard rank (scraped from builders.polymarket.com API)
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx


@dataclass
class BuilderOrderRecord:
    """A single order attributed to the builder."""
    order_id: str
    market_condition_id: str
    token_id: str
    side: str          # BUY | SELL
    price: float
    size_usdc: float
    timestamp: float = field(default_factory=time.time)
    attributed: bool = True


@dataclass
class BuilderRewardPeriod:
    """Weekly reward period summary."""
    week_start: str        # ISO date
    week_end: str
    volume_usdc: float
    reward_usdc: float
    rank: Optional[int] = None
    tier: str = "Unverified"


@dataclass
class BuilderStats:
    """Aggregate builder program statistics."""
    total_volume_usdc: float = 0.0
    total_orders: int = 0
    total_rewards_usdc: float = 0.0
    current_week_volume_usdc: float = 0.0
    current_week_orders: int = 0
    leaderboard_rank: Optional[int] = None
    tier: str = "Unverified"
    volume_by_market: Dict[str, float] = field(default_factory=dict)
    reward_history: List[BuilderRewardPeriod] = field(default_factory=list)
    last_updated: Optional[datetime] = None


class BuilderRewardsTracker:
    """
    Tracks builder program volume attribution and reward payouts.

    Revenue streams tracked:
      1. Spread capture (existing)
      2. Liquidity rewards (existing rewards.py)
      3. Builder Program rewards (this module) ← NEW
    """

    LEADERBOARD_API = "https://builders.polymarket.com/api/leaderboard"
    BUILDER_STATS_API = "https://builders.polymarket.com/api/builder"

    def __init__(self, builder_key: str = "", http_client: Optional[httpx.AsyncClient] = None):
        self.builder_key = builder_key
        self._http = http_client or httpx.AsyncClient(timeout=30.0)
        self._own_http = http_client is None

        self.stats = BuilderStats()
        self._orders: List[BuilderOrderRecord] = []
        self._week_start_ts = self._current_week_start()

    # ── Order attribution tracking ────────────────────────────────────────────

    def record_attributed_order(
        self,
        order_id: str,
        condition_id: str,
        token_id: str,
        side: str,
        price: float,
        size_usdc: float,
    ) -> BuilderOrderRecord:
        """Record an order that was submitted with builder attribution."""
        record = BuilderOrderRecord(
            order_id=order_id,
            market_condition_id=condition_id,
            token_id=token_id,
            side=side,
            price=price,
            size_usdc=size_usdc,
        )
        self._orders.append(record)

        # Update aggregate stats
        self.stats.total_orders += 1
        self.stats.total_volume_usdc += size_usdc

        # Reset week counter if new week started
        if time.time() > self._week_start_ts + 7 * 86400:
            self._week_start_ts = self._current_week_start()
            self.stats.current_week_volume_usdc = 0.0
            self.stats.current_week_orders = 0

        self.stats.current_week_volume_usdc += size_usdc
        self.stats.current_week_orders += 1

        # Volume by market
        prev = self.stats.volume_by_market.get(condition_id, 0.0)
        self.stats.volume_by_market[condition_id] = prev + size_usdc

        return record

    # ── Remote data fetching ──────────────────────────────────────────────────

    async def fetch_builder_stats(self) -> BuilderStats:
        """
        Fetch builder stats from Polymarket's builder leaderboard API.
        Falls back gracefully if API is unavailable or key not set.
        """
        if not self.builder_key:
            return self.stats

        try:
            # Fetch leaderboard position
            resp = await self._http.get(
                self.LEADERBOARD_API,
                params={"limit": 100},
                timeout=10.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                self._parse_leaderboard(data)

            # Fetch per-builder stats
            resp2 = await self._http.get(
                f"{self.BUILDER_STATS_API}/{self.builder_key}",
                timeout=10.0,
            )
            if resp2.status_code == 200:
                self._parse_builder_stats(resp2.json())

        except httpx.TimeoutException:
            print("[builder_rewards] Leaderboard API timeout — using cached stats")
        except Exception as e:
            print(f"[builder_rewards] Stats fetch error: {e}")

        self.stats.last_updated = datetime.now(timezone.utc)
        return self.stats

    def _parse_leaderboard(self, data: Any) -> None:
        """Parse leaderboard response to find our rank."""
        try:
            entries = data.get("builders", data) if isinstance(data, dict) else data
            for i, entry in enumerate(entries, 1):
                if entry.get("address", "").lower() == self.builder_key.lower():
                    self.stats.leaderboard_rank = i
                    self.stats.tier = entry.get("tier", "Unverified")
                    break
        except Exception as e:
            print(f"[builder_rewards] Leaderboard parse error: {e}")

    def _parse_builder_stats(self, data: dict) -> None:
        """Parse per-builder API response."""
        try:
            self.stats.total_rewards_usdc = float(data.get("total_rewards_usdc", 0))
            self.stats.tier = data.get("tier", self.stats.tier)

            history = data.get("reward_history", [])
            self.stats.reward_history = [
                BuilderRewardPeriod(
                    week_start=r.get("week_start", ""),
                    week_end=r.get("week_end", ""),
                    volume_usdc=float(r.get("volume_usdc", 0)),
                    reward_usdc=float(r.get("reward_usdc", 0)),
                    rank=r.get("rank"),
                    tier=r.get("tier", "Unverified"),
                )
                for r in history
            ]
        except Exception as e:
            print(f"[builder_rewards] Builder stats parse error: {e}")

    # ── Summary / reporting ───────────────────────────────────────────────────

    def summary(self) -> Dict[str, Any]:
        """Return a JSON-serializable summary for the API endpoint."""
        top_markets = sorted(
            self.stats.volume_by_market.items(),
            key=lambda x: x[1],
            reverse=True,
        )[:10]

        return {
            "builder_key": self.builder_key[:8] + "..." if self.builder_key else "not_set",
            "tier": self.stats.tier,
            "leaderboard_rank": self.stats.leaderboard_rank,
            "total_volume_usdc": round(self.stats.total_volume_usdc, 2),
            "total_orders_attributed": self.stats.total_orders,
            "total_rewards_usdc": round(self.stats.total_rewards_usdc, 4),
            "current_week": {
                "volume_usdc": round(self.stats.current_week_volume_usdc, 2),
                "orders": self.stats.current_week_orders,
            },
            "top_markets_by_volume": [
                {"condition_id": cid[:16] + "...", "volume_usdc": round(vol, 2)}
                for cid, vol in top_markets
            ],
            "reward_history": [
                {
                    "week_start": r.week_start,
                    "week_end": r.week_end,
                    "volume_usdc": round(r.volume_usdc, 2),
                    "reward_usdc": round(r.reward_usdc, 4),
                    "rank": r.rank,
                    "tier": r.tier,
                }
                for r in self.stats.reward_history[-8:]  # last 8 weeks
            ],
            "last_updated": (
                self.stats.last_updated.isoformat()
                if self.stats.last_updated
                else None
            ),
        }

    def log_summary(self) -> None:
        s = self.stats
        print(
            f"[builder_rewards] Tier={s.tier} | Rank=#{s.leaderboard_rank} | "
            f"Week vol=${s.current_week_volume_usdc:.2f} | "
            f"Total rewards=${s.total_rewards_usdc:.4f} USDC"
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _current_week_start() -> float:
        """Unix timestamp of the most recent Monday 00:00 UTC."""
        now = datetime.now(timezone.utc)
        days_since_monday = now.weekday()
        monday = now.replace(hour=0, minute=0, second=0, microsecond=0)
        monday = monday.replace(day=now.day - days_since_monday)
        return monday.timestamp()

    async def close(self) -> None:
        if self._own_http:
            await self._http.aclose()
