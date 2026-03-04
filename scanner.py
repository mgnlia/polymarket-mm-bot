"""Market scanner — fetch rewarded markets and score by reward potential."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import List, Optional

import httpx

from config import Config

GAMMA_API = "https://gamma-api.polymarket.com"


@dataclass
class ScoredMarket:
    condition_id: str
    question: str
    yes_token_id: str
    no_token_id: str
    yes_price: float
    no_price: float
    volume_24h: float
    liquidity: float
    rewards_min_size: float   # Minimum order size to qualify for rewards (USDC)
    rewards_max_spread: float # Maximum spread to qualify for rewards (cents)
    category: str
    is_extreme: bool          # Price <10¢ or >90¢
    reward_score: float       # Higher = better opportunity
    end_date: Optional[str]


def _parse_json_field(value, fallback):
    """Parse a field that may be a JSON-encoded string or already parsed."""
    if value is None:
        return fallback
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return fallback
    return fallback


def compute_reward_score(
    rewards_min_size: float,
    rewards_max_spread: float,
    liquidity: float,
    volume_24h: float,
    target_spread: float,
    yes_price: float,
) -> float:
    """
    Score a market by its reward farming potential.

    Reward formula per Polymarket docs:
        S(v, s) = ((v - s) / v)^2 * b
    where v=max_spread (in cents), s=actual_spread (in cents).

    rewards_min_size: minimum order size in USDC to qualify (e.g. 20 USDC)
    rewards_max_spread: max spread in cents to still earn rewards (e.g. 2.0)
    Higher score = better market to quote.
    """
    # No rewards configured → zero score
    if rewards_max_spread <= 0 or rewards_min_size <= 0:
        return 0.0

    target_spread_cents = target_spread * 100  # convert fraction to cents

    # Can we quote within the reward spread window?
    if target_spread_cents > rewards_max_spread:
        return 0.0  # Our spread is too wide to earn rewards

    spread_score = ((rewards_max_spread - target_spread_cents) / rewards_max_spread) ** 2

    # Penalise crowded markets (high liquidity = more competition)
    competition_penalty = 1.0 / (1.0 + liquidity / 10_000)

    # Volume bonus (active markets are better)
    volume_bonus = min(volume_24h / 100_000, 2.0)

    # Mid-price bonus: markets near 50¢ are most two-sided
    midness = 1.0 - abs(yes_price - 0.5) * 2.0  # 1.0 at 50¢, 0.0 at 0¢/100¢
    midness_bonus = max(0.0, midness)

    # Use rewards_min_size as a proxy for "how accessible" the market is
    # Lower min size = easier to qualify = better for small accounts
    accessibility = max(0.1, 1.0 / max(rewards_min_size, 1.0))

    return spread_score * competition_penalty * (1 + volume_bonus) * (1 + midness_bonus) * accessibility


class MarketScanner:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._http = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        await self._http.aclose()

    async def fetch_rewarded_markets(self) -> List[ScoredMarket]:
        """
        Fetch active, rewarded markets from Gamma API.

        Real Gamma API field layout (verified against live API):
          - clobTokenIds:   JSON string → ["tokenId0", "tokenId1"]  (YES=0, NO=1)
          - outcomePrices:  JSON string → ["0.62", "0.38"]          (YES=0, NO=1)
          - outcomes:       JSON string → ["Yes", "No"]
          - rewardsMinSize:   float — minimum order size (USDC) to qualify for rewards
          - rewardsMaxSpread: float — maximum spread (cents) to still earn rewards
          - volume24hr:     float — 24h volume in USDC
          - liquidityNum:   float — total liquidity in USDC
          - conditionId:    str   — market condition ID
          - question:       str   — market question text

        Returns markets sorted by reward_score descending.
        """
        try:
            resp = await self._http.get(
                f"{GAMMA_API}/markets",
                params={
                    "limit": 200,
                    "active": "true",
                    "closed": "false",
                },
            )
            resp.raise_for_status()
            raw = resp.json()
        except httpx.HTTPError as e:
            print(f"[scanner] Gamma API error: {e}")
            return []

        scored: List[ScoredMarket] = []
        for m in raw:
            try:
                # ── B1 FIX: Parse JSON-encoded string fields from real Gamma API ──
                clob_token_ids = _parse_json_field(m.get("clobTokenIds"), [])
                outcome_prices = _parse_json_field(m.get("outcomePrices"), [])
                # outcomes field gives us ["Yes", "No"] ordering
                # outcomes = _parse_json_field(m.get("outcomes"), ["Yes", "No"])

                if len(clob_token_ids) < 2 or len(outcome_prices) < 2:
                    continue

                # Convention: index 0 = YES, index 1 = NO
                yes_token_id = str(clob_token_ids[0])
                no_token_id = str(clob_token_ids[1])
                yes_price = float(outcome_prices[0] or 0.5)
                no_price = float(outcome_prices[1] or 0.5)

                volume_24h = float(m.get("volume24hr", 0) or 0)
                liquidity = float(m.get("liquidityNum", m.get("liquidity", 0)) or 0)

                # ── B2 FIX: Use correct semantics for reward fields ──
                # rewardsMinSize  = min order size in USDC to qualify (e.g. 20 USDC)
                # rewardsMaxSpread = max spread in cents to earn rewards (e.g. 2.0)
                rewards_min_size = float(m.get("rewardsMinSize", 0) or 0)
                rewards_max_spread = float(m.get("rewardsMaxSpread", 0) or 0)

                # Category from event data or fallback
                events = m.get("events") or []
                if events and isinstance(events, list) and len(events) > 0:
                    category = (events[0].get("category") or "other").lower()
                else:
                    category = (m.get("groupItemTagged") or m.get("category") or "other").lower()

                # Apply filters
                if volume_24h < self.config.min_volume_24h:
                    continue
                # Only include markets that have rewards configured
                if rewards_min_size <= 0 or rewards_max_spread <= 0:
                    continue
                if category not in self.config.categories:
                    continue

                is_extreme = (
                    yes_price < self.config.extreme_low_threshold
                    or yes_price > self.config.extreme_high_threshold
                )

                score = compute_reward_score(
                    rewards_min_size=rewards_min_size,
                    rewards_max_spread=rewards_max_spread,
                    liquidity=liquidity,
                    volume_24h=volume_24h,
                    target_spread=self.config.target_spread_pct,
                    yes_price=yes_price,
                )

                scored.append(ScoredMarket(
                    condition_id=m.get("conditionId", ""),
                    question=m.get("question", "Unknown"),
                    yes_token_id=yes_token_id,
                    no_token_id=no_token_id,
                    yes_price=yes_price,
                    no_price=no_price,
                    volume_24h=volume_24h,
                    liquidity=liquidity,
                    rewards_min_size=rewards_min_size,
                    rewards_max_spread=rewards_max_spread,
                    category=category,
                    is_extreme=is_extreme,
                    reward_score=score,
                    end_date=m.get("endDate"),
                ))
            except (KeyError, ValueError, TypeError) as exc:
                print(f"[scanner] Skipping malformed market: {exc}")
                continue

        # Sort by score, take top N
        scored.sort(key=lambda x: x.reward_score, reverse=True)
        top = scored[: self.config.max_markets]
        print(f"[scanner] Found {len(scored)} eligible markets, selected top {len(top)}")
        return top

    async def refresh_prices(self, markets: List[ScoredMarket]) -> List[ScoredMarket]:
        """Refresh live prices from CLOB midpoint endpoint."""
        async def _refresh(m: ScoredMarket) -> ScoredMarket:
            try:
                r = await self._http.get(
                    f"{self.config.clob_host}/midpoint",
                    params={"token_id": m.yes_token_id},
                )
                if r.status_code == 200:
                    m.yes_price = float(r.json().get("mid", m.yes_price))
                r2 = await self._http.get(
                    f"{self.config.clob_host}/midpoint",
                    params={"token_id": m.no_token_id},
                )
                if r2.status_code == 200:
                    m.no_price = float(r2.json().get("mid", m.no_price))
            except (httpx.HTTPError, ValueError):
                pass
            return m

        return list(await asyncio.gather(*[_refresh(m) for m in markets]))
