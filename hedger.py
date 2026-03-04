"""Hedger — offset directional risk by rebalancing positions."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

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
    - If net long YES on a market beyond threshold → sell YES / buy NO to reduce
    - If net short YES beyond threshold → buy YES / sell NO
    - Hedges are placed as market orders (immediate) to reduce risk fast
    """

    # Threshold: hedge when net exposure > X% of per-market limit
    HEDGE_THRESHOLD_PCT = 0.60  # 60% of max_position triggers hedge

    def __init__(self, config: Config, risk: RiskManager) -> None:
        self.config = config
        self.risk = risk

    def compute_hedges(self, markets: List[ScoredMarket]) -> List[HedgeAction]:
        """
        Compute hedge actions needed for current positions.
        Returns list of actions to execute.
        """
        actions: List[HedgeAction] = []
        threshold = self.config.max_position_per_market_usdc * self.HEDGE_THRESHOLD_PCT

        for market in markets:
            pos = self.risk.state.positions.get(market.condition_id)
            if not pos:
                continue

            net_yes = pos.yes_shares - pos.no_shares  # net YES position

            if net_yes > threshold:
                # Too long YES — hedge by buying NO
                hedge_size = min(
                    (net_yes - threshold * 0.5) * market.no_price,
                    self.config.max_position_per_market_usdc * 0.3,
                )
                if hedge_size >= self.config.min_order_size_usdc:
                    actions.append(HedgeAction(
                        condition_id=market.condition_id,
                        token_id=market.no_token_id,
                        side="BUY",
                        token="NO",
                        size_usdc=hedge_size,
                        reason=f"Net long YES {net_yes:.1f} shares > threshold {threshold:.1f}",
                    ))

            elif net_yes < -threshold:
                # Too long NO — hedge by buying YES
                hedge_size = min(
                    (-net_yes - threshold * 0.5) * market.yes_price,
                    self.config.max_position_per_market_usdc * 0.3,
                )
                if hedge_size >= self.config.min_order_size_usdc:
                    actions.append(HedgeAction(
                        condition_id=market.condition_id,
                        token_id=market.yes_token_id,
                        side="BUY",
                        token="YES",
                        size_usdc=hedge_size,
                        reason=f"Net long NO {-net_yes:.1f} shares > threshold {threshold:.1f}",
                    ))

        if actions:
            print(f"[hedger] {len(actions)} hedge action(s) needed")
            for a in actions:
                print(f"  → {a.side} {a.token} {a.size_usdc:.2f} USDC on {a.condition_id[:16]} — {a.reason}")

        return actions

    @property
    def _min_order_size(self) -> float:
        return self.config.min_order_size_usdc

    # Patch config access
    def __getattr__(self, name):
        if name == "config":
            raise AttributeError(name)
        return getattr(self.config, name)
