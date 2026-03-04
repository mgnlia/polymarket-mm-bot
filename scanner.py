"""Market scanner — fetch rewarded markets and score by reward potential."""
from __future__ import annotations

import asyncio
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
    incentive_size: float   # Daily reward pool (USDC)
    category: str
    is_extreme: bool        # Price <10¢ or >90¢
    reward_score: float     # Higher = better opportunity
    end_date: Optional[str]


def compute_reward_score(
    incentive_size: float,
    liquidity: float,
    volume_24h: float,
    target_spread: float,
    max_spread: float,
) -> float:
    """
    Score a market by its reward potential.

    Reward formula per Polymarket docs:
        S(v, s) = ((v - s) / v)^2 * b
    where v=max_spread, s=actual_spread, b=reward_pool.

    We estimate our score assuming we quote at target_spread.
    Higher score = better market to quote.
    """
    if max_spread <= 0:
        return 0.0
    spread_score = ((max_spread - target_spread) / max_spread) ** 2
    # Penalise crowded markets (high liquidity = more competition)
    competition_penalty = 1.0 / (1.0 + liquidity / 10_000)
    volume_bonus = min(volume_24h / 100_000, 2.0)  # Cap at 2x bonus
    return incentive_size * spread_score * competition_penalty * (1 + volume_bonus)


class MarketScanner:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._http = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        await self._http.aclose()

    async def fetch_rewarded_markets(self) -> List[ScoredMarket]:
        """
        Fetch active, rewarded markets from Gamma API.
        Returns markets sorted by reward_score descending.
        """
        try:
            resp = await self._http.get(
                f"{GAMMA_API}/markets",
                params={
                    "limit": 200,
                    "active": "true",
                    "closed": "false",
                    "liquidity_num_min": self.config.min_incentive_size,
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
                tokens = m.get("tokens", [])
                if len(tokens) < 2:
                    continue

                yes_tok = next((t for t in tokens if t.get("outcome", "").upper() == "YES"), tokens[0])
                no_tok = next((t for t in tokens if t.get("outcome", "").upper() == "NO"), tokens[1])

                yes_price = float(yes_tok.get("price", 0.5) or 0.5)
                no_price = float(no_tok.get("price", 0.5) or 0.5)
                volume_24h = float(m.get("volume24hr", 0) or 0)
                liquidity = float(m.get("liquidity", 0) or 0)
                incentive = float(m.get("rewardsMinSize", 0) or 0)
                category = (m.get("groupItemTagged") or m.get("category") or "other").lower()

                # Apply filters
                if volume_24h < self.config.min_volume_24h:
                    continue
                if incentive < self.config.min_incentive_size:
                    continue
                if category not in self.config.categories:
                    continue

                is_extreme = yes_price < self.config.extreme_low_threshold or yes_price > self.config.extreme_high_threshold

                score = compute_reward_score(
                    incentive_size=incentive,
                    liquidity=liquidity,
                    volume_24h=volume_24h,
                    target_spread=self.config.target_spread_pct,
                    max_spread=self.config.max_spread_pct,
                )

                scored.append(ScoredMarket(
                    condition_id=m.get("conditionId", ""),
                    question=m.get("question", "Unknown"),
                    yes_token_id=yes_tok.get("token_id", ""),
                    no_token_id=no_tok.get("token_id", ""),
                    yes_price=yes_price,
                    no_price=no_price,
                    volume_24h=volume_24h,
                    liquidity=liquidity,
                    incentive_size=incentive,
                    category=category,
                    is_extreme=is_extreme,
                    reward_score=score,
                    end_date=m.get("endDate"),
                ))
            except (KeyError, ValueError, TypeError):
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
