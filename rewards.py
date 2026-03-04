"""Reward tracker — fetch daily liquidity reward payouts from Polymarket API."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

import httpx

GAMMA_API = "https://gamma-api.polymarket.com"
REWARDS_API = "https://rewards.polymarket.com"  # hypothetical — adjust per docs


@dataclass
class DailyReward:
    date: str
    market_id: str
    market_question: str
    reward_usdc: float
    volume_quoted: float
    time_quoted_minutes: int


@dataclass
class RewardSummary:
    total_earned_usdc: float = 0.0
    today_earned_usdc: float = 0.0
    week_earned_usdc: float = 0.0
    month_earned_usdc: float = 0.0
    by_market: Dict[str, float] = field(default_factory=dict)
    history: List[DailyReward] = field(default_factory=list)
    last_updated: Optional[datetime] = None


class RewardTracker:
    """
    Tracks liquidity rewards from Polymarket's reward program.

    Rewards are sampled every minute and paid daily at midnight UTC.
    Minimum payout: $1 USDC.

    Reward formula: S(v, s) = ((v - s) / v)^2 * b
    - v = max_spread config
    - s = actual spread from midpoint
    - b = market reward pool
    """

    def __init__(self, funder_address: str) -> None:
        self.funder_address = funder_address
        self.summary = RewardSummary()
        self._http = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        await self._http.aclose()

    async def fetch_rewards(self) -> RewardSummary:
        """
        Fetch reward history for our address.
        Falls back to estimated rewards if API unavailable.
        """
        try:
            # Try Polymarket rewards API
            resp = await self._http.get(
                f"{GAMMA_API}/rewards",
                params={"address": self.funder_address, "limit": 90},
            )
            if resp.status_code == 200:
                return self._parse_rewards(resp.json())
        except httpx.HTTPError:
            pass

        # Fallback: return current summary unchanged
        self.summary.last_updated = datetime.now(timezone.utc)
        return self.summary

    def _parse_rewards(self, data: list | dict) -> RewardSummary:
        """Parse reward API response into summary."""
        records = data if isinstance(data, list) else data.get("rewards", [])
        summary = RewardSummary()

        from datetime import date, timedelta
        today = date.today().isoformat()
        week_ago = (date.today() - timedelta(days=7)).isoformat()
        month_ago = (date.today() - timedelta(days=30)).isoformat()

        for r in records:
            try:
                reward_date = r.get("date", "")
                amount = float(r.get("amount", 0) or 0)
                market_id = r.get("marketId", "")
                question = r.get("question", "")

                dr = DailyReward(
                    date=reward_date,
                    market_id=market_id,
                    market_question=question,
                    reward_usdc=amount,
                    volume_quoted=float(r.get("volumeQuoted", 0) or 0),
                    time_quoted_minutes=int(r.get("minutesQuoted", 0) or 0),
                )
                summary.history.append(dr)
                summary.total_earned_usdc += amount
                summary.by_market[market_id] = summary.by_market.get(market_id, 0) + amount

                if reward_date == today:
                    summary.today_earned_usdc += amount
                if reward_date >= week_ago:
                    summary.week_earned_usdc += amount
                if reward_date >= month_ago:
                    summary.month_earned_usdc += amount
            except (KeyError, ValueError, TypeError):
                continue

        summary.last_updated = datetime.now(timezone.utc)
        self.summary = summary
        return summary

    def estimate_hourly_rate(self, markets_quoted: int, avg_spread: float, max_spread: float, total_pool: float) -> float:
        """
        Estimate hourly reward rate based on current quoting activity.
        Uses the quadratic reward formula.
        """
        if max_spread <= 0 or avg_spread >= max_spread:
            return 0.0
        score_per_market = ((max_spread - avg_spread) / max_spread) ** 2
        # Rough estimate: rewards paid over 24h, sampled every minute
        daily_estimate = score_per_market * total_pool * markets_quoted
        return daily_estimate / 24.0

    def log_summary(self) -> None:
        s = self.summary
        print(f"[rewards] Today: ${s.today_earned_usdc:.4f} | "
              f"7d: ${s.week_earned_usdc:.4f} | "
              f"30d: ${s.month_earned_usdc:.4f} | "
              f"Total: ${s.total_earned_usdc:.4f}")
        if s.by_market:
            top = sorted(s.by_market.items(), key=lambda x: x[1], reverse=True)[:3]
            for mid, amt in top:
                print(f"  Market {mid[:16]}: ${amt:.4f}")
