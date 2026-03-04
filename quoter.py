"""Quoter — calculate fair value and place two-sided limit orders."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import httpx

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
    """

    def __init__(self, config: Config, clob_client=None) -> None:
        self.config = config
        self._client = clob_client  # py-clob-client instance (None = dry-run)
        self._active_orders: Dict[str, List[Quote]] = {}  # condition_id → quotes
        self._http = httpx.AsyncClient(timeout=30.0)

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
        """
        Estimate fair value from current prices.
        Uses midpoint of YES price as fair value.
        Clamps to [0.02, 0.98] to avoid degenerate quotes.
        """
        fv = market.yes_price
        return max(0.02, min(0.98, fv))

    def _compute_quotes(self, market: ScoredMarket) -> Tuple[float, float, float]:
        """
        Compute bid/ask prices and spread.
        Returns (bid_price, ask_price, half_spread).
        """
        fv = self._fair_value(market)
        half_spread = self.config.target_spread_pct / 2.0

        # In extreme markets, use tighter quotes (more risk, but required for full score)
        if market.is_extreme:
            half_spread = min(half_spread, 0.01)

        bid = max(0.01, fv - half_spread)
        ask = min(0.99, fv + half_spread)

        # Ensure minimum tick size (0.001 on Polymarket)
        bid = round(bid, 3)
        ask = round(ask, 3)

        return bid, ask, ask - bid

    def _order_size(self, market: ScoredMarket) -> float:
        """Determine order size in USDC, reduced for extreme markets."""
        size = self.config.order_size_usdc
        if market.is_extreme:
            size *= self.config.extreme_market_size_multiplier
        return max(self.config.min_order_size_usdc, size)

    def build_quotes(self, market: ScoredMarket, net_position: float = 0.0) -> MarketQuotes:
        """
        Build two-sided quotes for a market.

        net_position: current net YES exposure in USDC
            positive = long YES, negative = short YES (long NO)
        """
        bid_price, ask_price, spread = self._compute_quotes(market)
        size = self._order_size(market)

        score = self.compute_reward_score(spread)
        print(
            f"[quoter] {market.question[:50]} | FV={self._fair_value(market):.3f} "
            f"bid={bid_price:.3f} ask={ask_price:.3f} spread={spread:.3f} "
            f"reward_score={score:.3f}"
        )

        # Skew sizes based on net position (delta-neutral management)
        yes_bid_size = size
        yes_ask_size = size
        no_bid_size = size
        no_ask_size = size

        if net_position > 0:
            # Long YES — reduce YES bids, increase YES asks to reduce exposure
            yes_bid_size *= 0.5
            yes_ask_size *= 1.5
        elif net_position < 0:
            # Short YES — increase YES bids, reduce YES asks
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
                price=1.0 - ask_price,  # NO bid = 1 - YES ask
                size=no_bid_size,
            ),
            no_ask=Quote(
                market_condition_id=market.condition_id,
                token_id=market.no_token_id,
                side="SELL",
                price=1.0 - bid_price,  # NO ask = 1 - YES bid
                size=no_ask_size,
            ),
        )

    async def place_quotes(self, market_quotes: MarketQuotes) -> MarketQuotes:
        """
        Place all four quotes (YES bid/ask, NO bid/ask).
        In dry-run mode (no client), just logs and marks as 'placed'.
        """
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
                print(f"[quoter:dry-run] Would place {q.side} {q.size:.2f} USDC @ {q.price:.3f} on {q.token_id[:16]}...")
                q.order_id = f"dry-{q.token_id[:8]}-{q.side}"
                q.status = "placed"
            else:
                try:
                    resp = await self._place_limit_order(q)
                    q.order_id = resp.get("orderID") or resp.get("id")
                    q.status = "placed"
                except Exception as e:
                    print(f"[quoter] Order placement failed: {e}")
                    q.status = "failed"

        self._active_orders[market_quotes.market.condition_id] = [q for q in quotes if q]
        return market_quotes

    async def _place_limit_order(self, quote: Quote) -> dict:
        """Place a single limit order via py-clob-client."""
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
        return await asyncio.to_thread(self._client.post_order, signed, OrderType.GTC)

    async def cancel_market_orders(self, condition_id: str) -> None:
        """Cancel all active orders for a market."""
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
        """Emergency cancel all active orders."""
        for condition_id in list(self._active_orders.keys()):
            await self.cancel_market_orders(condition_id)
        print("[quoter] All orders cancelled")
