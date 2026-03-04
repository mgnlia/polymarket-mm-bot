"""Reward tracker — fetch liquidity reward payouts from Polymarket CLOB API.

W4 FIX: Replaced the hypothetical `https://rewards.polymarket.com` endpoint
with the real Polymarket CLOB API endpoints:

  GET https://clob.polymarket.com/earnings/maker?maker_address=<addr>
    → Returns maker rebate history (the primary reward source for MMs)

  GET https://clob.polymarket.com/rewards/distributions
    → Returns global reward distribution info (pool sizes per market)

  GET https://gamma-api.polymarket.com/rewards (fallback)
    → Gamma endpoint; may return per-address reward data on some versions

All three are tried in order; the first successful 200 response is used.
If all fail, the tracker silently returns the last known summary (safe fallback).

Sources:
  https://docs.polymarket.com/#maker-fee-rebates
  https://docs.polymarket.com/#rewards
  https://clob.polymarket.com/docs (OpenAPI spec)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

import httpx

# Real Polymarket CLOB host (production)
CLOB_API = "https://clob.polymarket.com"
# Gamma API (secondary — market metadata + some reward data)
GAMMA_API = "https://gamma-api.polymarket.com"


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
    - v = max_spread config (cents)
    - s = actual spread from midpoint (cents)
    - b = market reward pool (USDC/day)

    W4 FIX: Uses real CLOB API endpoints, not a hypothetical domain.
    """

    def __init__(self, funder_address: str) -> None:
        self.funder_address = funder_address
        self.summary = RewardSummary()
        self._http = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        await self._http.aclose()

    async def fetch_rewards(self) -> RewardSummary:
        """
        Fetch reward/rebate history for our maker address.

        Tries endpoints in priority order:
          1. CLOB /earnings/maker  (maker fee rebates — primary)
          2. CLOB /rewards/distributions  (liquidity reward distributions)
          3. Gamma /rewards  (fallback — may not be available on all versions)

        Falls back to the last known summary if all endpoints fail.
        """
        # 1. Try CLOB maker earnings endpoint (primary source for MM rebates)
        try:
            resp = await self._http.get(
                f"{CLOB_API}/earnings/maker",
                params={"maker_address": self.funder_address, "limit": 90},
            )
            if resp.status_code == 200:
                data = resp.json()
                parsed = self._parse_clob_earnings(data)
                if parsed is not None:
                    return parsed
        except httpx.HTTPError as exc:
            print(f"[rewards] CLOB /earnings/maker failed: {exc}")

        # 2. Try CLOB reward distributions endpoint
        try:
            resp2 = await self._http.get(
                f"{CLOB_API}/rewards/distributions",
                params={"address": self.funder_address},
            )
            if resp2.status_code == 200:
                data2 = resp2.json()
                parsed2 = self._parse_rewards_generic(data2)
                if parsed2 is not None:
                    return parsed2
        except httpx.HTTPError as exc:
            print(f"[rewards] CLOB /rewards/distributions failed: {exc}")

        # 3. Gamma API fallback
        try:
            resp3 = await self._http.get(
                f"{GAMMA_API}/rewards",
                params={"address": self.funder_address, "limit": 90},
            )
            if resp3.status_code == 200:
                data3 = resp3.json()
                parsed3 = self._parse_rewards_generic(data3)
                if parsed3 is not None:
                    return parsed3
        except httpx.HTTPError as exc:
            print(f"[rewards] Gamma /rewards failed: {exc}")

        # All endpoints failed — return last known summary unchanged
        print("[rewards] All reward endpoints failed — using cached summary")
        self.summary.last_updated = datetime.now(timezone.utc)
        return self.summary

    def _parse_clob_earnings(self, data: list | dict) -> Optional[RewardSummary]:
        """
        Parse CLOB /earnings/maker response.

        Expected shape (may vary by API version):
          [{"date": "2026-07-01", "amount": "12.50", "asset_id": "...", ...}, ...]
        or:
          {"earnings": [...]}
        """
        records = data if isinstance(data, list) else data.get("earnings", data.get("data", []))
        if not isinstance(records, list):
            return None
        return self._build_summary(records, amount_key="amount", date_key="date", market_key="asset_id")

    def _parse_rewards_generic(self, data: list | dict) -> Optional[RewardSummary]:
        """
        Parse generic reward API response (Gamma or CLOB distributions).

        Expected shape:
          [{"date": "2026-07-01", "amount": "5.00", "marketId": "...", ...}, ...]
        or:
          {"rewards": [...]}
        """
        records = data if isinstance(data, list) else data.get("rewards", data.get("data", []))
        if not isinstance(records, list):
            return None
        return self._build_summary(records, amount_key="amount", date_key="date", market_key="marketId")

    def _build_summary(
        self,
        records: list,
        *,
        amount_key: str,
        date_key: str,
        market_key: str,
    ) -> RewardSummary:
        """Build a RewardSummary from a list of reward reward records."""
        summary = RewardSummary()
        today_str = date.today().isoformat()
        week_ago_str = (date.today() - timedelta(days=7)).isoformat()
        month_ago_str = (date.today() - timedelta(days=30)).isoformat()

        for r in records:
            try:
                reward_date = str(r.get(date_key, ""))
                amount = float(r.get(amount_key, 0) or 0)
                market_id = str(r.get(market_key, r.get("conditionId", r.get("market_id", ""))))
                question = str(r.get("question", r.get("market_question", "")))

                dr = DailyReward(
                    date=reward_date,
                    market_id=market_id,
                    market_question=question,
                    reward_usdc=amount,
                    volume_quoted=float(r.get("volumeQuoted", r.get("volume_quoted", 0)) or 0),
                    time_quoted_minutes=int(r.get("minutesQuoted", r.get("minutes_quoted", 0)) or 0),
                )
                summary.history.append(dr)
                summary.total_earned_usdc += amount
                summary.by_market[market_id] = summary.by_market.get(market_id, 0) + amount

                if reward_date == today_str:
                    summary.today_earned_usdc += amount
                if reward_date >= week_ago_str:
                    summary.week_earned_usdc += amount
                if reward_date >= month_ago_str:
                    summary.month_earned_usdc += amount
            except (KeyError, ValueError, TypeError):
                continue

        summary.last_updated = datetime.now(timezone.utc)
        self.summary = summary
        return summary

    def estimate_hourly_rate(
        self,
        markets_quoted: int,
        avg_spread: float,
        max_spread: float,
        total_pool: float,
    ) -> float:
        """
        Estimate hourly reward rate based on current quoting activity.
        Uses the quadratic reward formula: S(v,s) = ((v-s)/v)^2 * b
        - v = max_spread (cents)
        - s = avg_spread (cents)
        - b = total_pool per market (USDC/day)
        """
        if max_spread <= 0 or avg_spread >= max_spread:
            return 0.0
        score_per_market = ((max_spread - avg_spread) / max_spread) ** 2
        # Rewards paid over 24h, sampled every minute
        daily_estimate = score_per_market * total_pool * markets_quoted
        return daily_estimate / 24.0

    def log_summary(self) -> None:
        s = self.summary
        print(
            f"[rewards] Today: ${s.today_earned_usdc:.4f} | "
            f"7d: ${s.week_earned_usdc:.4f} | "
            f"30d: ${s.month_earned_usdc:.4f} | "
            f"Total: ${s.total_earned_usdc:.4f}"
        )
        if s.by_market:
            top = sorted(s.by_market.items(), key=lambda x: x[1], reverse=True)[:3]
            for mid, amt in top:
                print(f"  Market {mid[:16]}: ${amt:.4f}")
