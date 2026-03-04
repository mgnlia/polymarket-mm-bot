"""Hedger — offset directional risk by rebalancing positions.

W2 FIX: All hedge threshold comparisons are now in USDC notional, not raw
share counts. Previously, net_yes (shares) was compared directly to a threshold
derived from max_position_per_market_usdc (USDC), mixing units. A position of
100 shares at $0.10 = $10 USDC is very different from 100 shares at $0.90 = $90 USDC.

Fix: convert share imbalance to USDC by multiplying by current market prices
before comparing to the USDC threshold.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

from config import Config
from risk import RiskManager
from scanner import ScoredMarket


@dataclass
class HedgeAction:
    condition_id: str
    token_id: str
    side: str       # "BUY" | "SELL"
    token: str      # "YES" | "NO"
    size_usdc: float
    reason: str


class Hedger:
    """
    Monitors accumulated directional risk and generates hedge orders.

    Strategy:
    - If net long YES on a market beyond threshold → buy NO to reduce
    - If net short YES beyond threshold → buy YES to reduce
    - Hedges are placed as market orders (immediate) to reduce risk fast

    All thresholds are in USDC notional to avoid the unit mismatch (W2 fix).
    """

    # Threshold: hedge when net USDC exposure > X% of per-market limit
    HEDGE_THRESHOLD_PCT = 0.60  # 60% of max_position triggers hedge

    def __init__(self, config: Config, risk: RiskManager) -> None:
        self.config = config
        self.risk = risk

    def compute_hedges(self, markets: List[ScoredMarket]) -> List[HedgeAction]:
        """
        Compute hedge actions needed for current positions.

        W2 FIX — Unit consistency:
          net_yes_usdc = (yes_shares * yes_price) - (no_shares * no_price)
          threshold_usdc = max_position_per_market_usdc * HEDGE_THRESHOLD_PCT

        Both sides are now in USDC, making the comparison meaningful regardless
        of the market's current price level.
        """
        actions: List[HedgeAction] = []
        # Threshold is in USDC notional — same unit as net_yes_usdc below
        threshold_usdc = self.config.max_position_per_market_usdc * self.HEDGE_THRESHOLD_PCT

        for market in markets:
            pos = self.risk.state.positions.get(market.condition_id)
            if not pos:
                continue

            # W2 FIX: convert shares → USDC using live prices from ScoredMarket.
            # yes_shares * yes_price = USDC value of YES leg
            # no_shares  * no_price  = USDC value of NO leg
            # Net (positive = net long YES in USDC terms)
            net_yes_usdc = (
                pos.yes_shares * market.yes_price
                - pos.no_shares * market.no_price
            )

            if net_yes_usdc > threshold_usdc:
                # Too long YES in USDC — hedge by buying NO
                excess_usdc = net_yes_usdc - threshold_usdc * 0.5
                hedge_size = min(
                    excess_usdc,
                    self.config.max_position_per_market_usdc * 0.3,
                )
                if hedge_size >= self.config.min_order_size_usdc:
                    actions.append(HedgeAction(
                        condition_id=market.condition_id,
                        token_id=market.no_token_id,
                        side="BUY",
                        token="NO",
                        size_usdc=hedge_size,
                        reason=(
                            f"Net long YES {net_yes_usdc:.2f} USDC "
                            f"> threshold {threshold_usdc:.2f} USDC"
                        ),
                    ))

            elif net_yes_usdc < -threshold_usdc:
                # Too long NO in USDC — hedge by buying YES
                excess_usdc = -net_yes_usdc - threshold_usdc * 0.5
                hedge_size = min(
                    excess_usdc,
                    self.config.max_position_per_market_usdc * 0.3,
                )
                if hedge_size >= self.config.min_order_size_usdc:
                    actions.append(HedgeAction(
                        condition_id=market.condition_id,
                        token_id=market.yes_token_id,
                        side="BUY",
                        token="YES",
                        size_usdc=hedge_size,
                        reason=(
                            f"Net long NO {-net_yes_usdc:.2f} USDC "
                            f"> threshold {threshold_usdc:.2f} USDC"
                        ),
                    ))

        if actions:
            print(f"[hedger] {len(actions)} hedge action(s) needed")
            for a in actions:
                print(
                    f"  → {a.side} {a.token} {a.size_usdc:.2f} USDC "
                    f"on {a.condition_id[:16]} — {a.reason}"
                )

        return actions

    @property
    def _min_order_size(self) -> float:
        return self.config.min_order_size_usdc
