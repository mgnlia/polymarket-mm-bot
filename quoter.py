"""Quoter — calculate fair value and place two-sided limit orders.

Upgraded with Builder Program attribution:
- All orders tagged with builder headers via BuilderCreds
- Attributed orders tracked in BuilderRewardsTracker
- Relayer client used for gasless onchain ops
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import httpx

from builder_auth import BuilderCreds, build_builder_headers
from builder_rewards import BuilderRewardsTracker
from config import Config
from scanner import ScoredMarket


@dataclass
class Quote:
    market_condition_id: str
    token_id: str
    side: str          # "BUY" | "SELL"
    price: float
    size: float
    order_id: Optional[str] = None
    status: str = "pending"  # pending | placed | filled | cancelled


@dataclass
class MarketQuotes:
    market: ScoredMarket
    yes_bid: Optional[Quote]   # Buy YES (bid on YES token)
    yes_ask: Optional[Quote]   # Sell YES (ask on YES token)
    no_bid: Optional[Quote]    # Buy NO (bid on NO token)
    no_ask: Optional[Quote]    # Sell NO (ask on NO token)


class Quoter:
    """
    Calculates and places two-sided quotes for rewarded markets.

    Reward formula: S(v, s) = ((v - s) / v)^2 * b
    - Tighter spread → exponentially more rewards
    - Two-sided depth = full score; single-sided = 1/3 score (c=3.0)
    - In extreme markets (<10¢ or >90¢), MUST post both sides

    Builder Program integration:
    - Builder headers injected into every order (via session patch or per-request)
    - Placed orders recorded in BuilderRewardsTracker for volume attribution
    """

    def __init__(
        self,
        config: Config,
        clob_client=None,
        builder_creds: Optional[BuilderCreds] = None,
        builder_rewards: Optional[BuilderRewardsTracker] = None,
        relayer_client=None,
    ) -> None:
        self.config = config
        self._client = clob_client
        self._builder_creds = builder_creds or BuilderCreds.from_env()
        self._builder_rewards = builder_rewards
        self._relayer = relayer_client
        self._active_orders: Dict[str, List[Quote]] = {}  # condition_id → quotes
        self._http = httpx.AsyncClient(timeout=30.0)

        if self._builder_creds.configured:
            print(f"[quoter] Builder attribution ENABLED (key={self._builder_creds.key[:8]}...)")
        else:
            print("[quoter] Builder attribution DISABLED (set POLY_BUILDER_API_KEY etc.)")

    async def close(self) -> None:
        await self._http.aclose()

    def compute_reward_score(self, actual_spread: float) -> float:
        """Estimate our reward score for a given spread."""
        v = self.config.max_spread_pct
        s = actual_spread
        if v <= 0 or s >= v:
            return 0.0
        return ((v - s) / v) ** 2

    def _fair_value(self, market: ScoredMarket) -> float:
        fv = market.yes_price
        return max(0.02, min(0.98, fv))

    def _compute_quotes(self, market: ScoredMarket) -> Tuple[float, float, float]:
        fv = self._fair_value(market)
        half_spread = self.config.target_spread_pct / 2.0
        if market.is_extreme:
            half_spread = min(half_spread, 0.01)
        bid = max(0.01, fv - half_spread)
        ask = min(0.99, fv + half_spread)
        bid = round(bid, 3)
        ask = round(ask, 3)
        return bid, ask, ask - bid

    def _order_size(self, market: ScoredMarket) -> float:
        size = self.config.order_size_usdc
        if market.is_extreme:
            size *= self.config.extreme_market_size_multiplier
        return max(self.config.min_order_size_usdc, size)

    def build_quotes(self, market: ScoredMarket, net_position: float = 0.0) -> MarketQuotes:
        bid_price, ask_price, spread = self._compute_quotes(market)
        size = self._order_size(market)
        score = self.compute_reward_score(spread)

        print(
            f"[quoter] {market.question[:50]} | FV={self._fair_value(market):.3f} "
            f"bid={bid_price:.3f} ask={ask_price:.3f} spread={spread:.3f} "
            f"reward_score={score:.3f}"
        )

        yes_bid_size = size
        yes_ask_size = size
        no_bid_size = size
        no_ask_size = size

        if net_position > 0:
            yes_bid_size *= 0.5
            yes_ask_size *= 1.5
        elif net_position < 0:
            yes_bid_size *= 1.5
            yes_ask_size *= 0.5

        return MarketQuotes(
            market=market,
            yes_bid=Quote(
                market_condition_id=market.condition_id,
                token_id=market.yes_token_id,
                side="BUY",
                price=bid_price,
                size=yes_bid_size,
            ),
            yes_ask=Quote(
                market_condition_id=market.condition_id,
                token_id=market.yes_token_id,
                side="SELL",
                price=ask_price,
                size=yes_ask_size,
            ),
            no_bid=Quote(
                market_condition_id=market.condition_id,
                token_id=market.no_token_id,
                side="BUY",
                price=1.0 - ask_price,
                size=no_bid_size,
            ),
            no_ask=Quote(
                market_condition_id=market.condition_id,
                token_id=market.no_token_id,
                side="SELL",
                price=1.0 - bid_price,
                size=no_ask_size,
            ),
        )

    async def place_quotes(self, market_quotes: MarketQuotes) -> MarketQuotes:
        quotes = [
            market_quotes.yes_bid,
            market_quotes.yes_ask,
            market_quotes.no_bid,
            market_quotes.no_ask,
        ]

        for q in quotes:
            if q is None:
                continue
            if self._client is None:
                # Dry-run mode
                print(
                    f"[quoter:dry-run] Would place {q.side} {q.size:.2f} USDC "
                    f"@ {q.price:.3f} on {q.token_id[:16]}... "
                    f"[builder={'yes' if self._builder_creds.configured else 'no'}]"
                )
                q.order_id = f"dry-{q.token_id[:8]}-{q.side}"
                q.status = "placed"
                # Still record for volume tracking in dry-run
                self._record_attributed(q)
            else:
                try:
                    resp = await self._place_limit_order(q)
                    q.order_id = resp.get("orderID") or resp.get("id")
                    q.status = "placed"
                    self._record_attributed(q)
                except Exception as e:
                    print(f"[quoter] Order placement failed: {e}")
                    q.status = "failed"

        self._active_orders[market_quotes.market.condition_id] = [q for q in quotes if q]
        return market_quotes

    def _record_attributed(self, q: Quote) -> None:
        """Record the order in builder rewards tracker if attribution is active."""
        if self._builder_rewards and self._builder_creds.configured and q.order_id:
            self._builder_rewards.record_attributed_order(
                order_id=q.order_id,
                condition_id=q.market_condition_id,
                token_id=q.token_id,
                side=q.side,
                price=q.price,
                size_usdc=q.size,
            )

    async def _place_limit_order(self, quote: Quote) -> dict:
        """Place a single limit order via py-clob-client with builder headers."""
        from py_clob_client.clob_types import LimitOrderArgs, OrderType  # type: ignore
        from py_clob_client.order_builder.constants import BUY, SELL  # type: ignore

        side = BUY if quote.side == "BUY" else SELL
        args = LimitOrderArgs(
            token_id=quote.token_id,
            price=quote.price,
            size=quote.size,
            side=side,
        )
        signed = await asyncio.to_thread(self._client.create_limit_order, args)

        # Builder headers are injected via the patched session (builder_auth.py),
        # but we also pass them explicitly for clients that support it directly.
        extra_headers = {}
        if self._builder_creds.configured:
            extra_headers = build_builder_headers(
                self._builder_creds,
                method="POST",
                path="/order",
            )

        # post_order may accept headers kwarg in newer py-clob-client versions
        try:
            return await asyncio.to_thread(
                self._client.post_order, signed, OrderType.GTC, headers=extra_headers
            )
        except TypeError:
            # Older versions don't accept headers kwarg — session patch handles it
            return await asyncio.to_thread(self._client.post_order, signed, OrderType.GTC)

    async def cancel_market_orders(self, condition_id: str) -> None:
        orders = self._active_orders.get(condition_id, [])
        for q in orders:
            if q.order_id and q.status == "placed":
                if self._client is None:
                    print(f"[quoter:dry-run] Would cancel order {q.order_id}")
                    q.status = "cancelled"
                else:
                    try:
                        await asyncio.to_thread(self._client.cancel, q.order_id)
                        q.status = "cancelled"
                    except Exception as e:
                        print(f"[quoter] Cancel failed for {q.order_id}: {e}")
        self._active_orders.pop(condition_id, None)

    async def cancel_all_orders(self) -> None:
        for condition_id in list(self._active_orders.keys()):
            await self.cancel_market_orders(condition_id)
        print("[quoter] All orders cancelled")
