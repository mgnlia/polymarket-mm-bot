"""Risk manager — delta-neutral position management, exposure limits, circuit breakers."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Dict, Optional

from config import Config


@dataclass
class Position:
    condition_id: str
    question: str
    yes_shares: float = 0.0
    no_shares: float = 0.0
    yes_avg_cost: float = 0.0
    no_avg_cost: float = 0.0
    realized_pnl: float = 0.0
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class RiskState:
    total_capital_usdc: float
    starting_capital_usdc: float
    cash_usdc: float
    positions: Dict[str, Position] = field(default_factory=dict)
    daily_pnl: float = 0.0
    total_realized_pnl: float = 0.0
    trading_halted: bool = False
    halt_reason: Optional[str] = None
    last_reset_date: date = field(default_factory=date.today)


class RiskManager:
    """
    Manages risk across all positions:
    - Tracks net exposure per market and globally
    - Enforces max position limits
    - Circuit breakers: max drawdown + daily loss limit
    - Provides delta-neutral signals to quoter
    """

    def __init__(self, config: Config, starting_capital: float) -> None:
        self.config = config
        self.state = RiskState(
            total_capital_usdc=starting_capital,
            starting_capital_usdc=starting_capital,
            cash_usdc=starting_capital,
        )

    # ── Position tracking ────────────────────────────────────────────────────

    def record_fill(
        self,
        condition_id: str,
        question: str,
        token_side: str,  # "YES" | "NO"
        side: str,         # "BUY" | "SELL"
        shares: float,
        price: float,
        fee: float = 0.0,
    ) -> None:
        """Update position and cash after a fill."""
        pos = self.state.positions.setdefault(
            condition_id, Position(condition_id=condition_id, question=question)
        )
        cost = shares * price + fee

        if token_side == "YES":
            if side == "BUY":
                # Update avg cost
                total_shares = pos.yes_shares + shares
                if total_shares > 0:
                    pos.yes_avg_cost = (pos.yes_shares * pos.yes_avg_cost + cost) / total_shares
                pos.yes_shares = total_shares
                self.state.cash_usdc -= cost
            else:  # SELL
                proceeds = shares * price - fee
                pnl = (price - pos.yes_avg_cost) * shares - fee
                pos.yes_shares -= shares
                pos.realized_pnl += pnl
                self.state.cash_usdc += proceeds
                self.state.daily_pnl += pnl
                self.state.total_realized_pnl += pnl
        else:  # NO token
            if side == "BUY":
                total_shares = pos.no_shares + shares
                if total_shares > 0:
                    pos.no_avg_cost = (pos.no_shares * pos.no_avg_cost + cost) / total_shares
                pos.no_shares = total_shares
                self.state.cash_usdc -= cost
            else:
                proceeds = shares * price - fee
                pnl = (price - pos.no_avg_cost) * shares - fee
                pos.no_shares -= shares
                pos.realized_pnl += pnl
                self.state.cash_usdc += proceeds
                self.state.daily_pnl += pnl
                self.state.total_realized_pnl += pnl

        self._check_circuit_breakers()

    def get_net_yes_exposure(self, condition_id: str) -> float:
        """
        Net YES exposure in USDC for a market.
        Positive = net long YES, negative = net short YES (long NO).
        """
        pos = self.state.positions.get(condition_id)
        if not pos:
            return 0.0
        yes_val = pos.yes_shares * self.config.max_spread_pct  # rough mark
        no_val = pos.no_shares * self.config.max_spread_pct
        return yes_val - no_val

    def get_total_exposure(self) -> float:
        """Total absolute exposure across all markets."""
        total = 0.0
        for pos in self.state.positions.values():
            total += abs(pos.yes_shares) + abs(pos.no_shares)
        return total

    def get_unrealized_pnl(self, condition_id: str, yes_price: float, no_price: float) -> float:
        """Calculate unrealized P&L for a position at current prices."""
        pos = self.state.positions.get(condition_id)
        if not pos:
            return 0.0
        yes_pnl = pos.yes_shares * (yes_price - pos.yes_avg_cost) if pos.yes_shares > 0 else 0.0
        no_pnl = pos.no_shares * (no_price - pos.no_avg_cost) if pos.no_shares > 0 else 0.0
        return yes_pnl + no_pnl

    # ── Pre-trade checks ─────────────────────────────────────────────────────

    def can_trade(self, condition_id: str, size_usdc: float) -> tuple[bool, str]:
        """
        Check if a trade is allowed.
        Returns (allowed, reason).
        """
        if self.state.trading_halted:
            return False, f"Trading halted: {self.state.halt_reason}"

        # Check daily reset
        today = date.today()
        if today > self.state.last_reset_date:
            self.state.daily_pnl = 0.0
            self.state.last_reset_date = today

        # Cash check
        if self.state.cash_usdc < size_usdc:
            return False, f"Insufficient cash: {self.state.cash_usdc:.2f} < {size_usdc:.2f}"

        # Per-market position limit
        pos = self.state.positions.get(condition_id)
        if pos:
            market_exposure = (pos.yes_shares + pos.no_shares) * 0.5  # rough
            if market_exposure + size_usdc > self.config.max_position_per_market_usdc:
                return False, f"Market exposure limit reached for {condition_id[:16]}"

        # Total exposure limit
        total_exp = self.get_total_exposure()
        if total_exp + size_usdc > self.config.max_total_exposure_usdc:
            return False, f"Total exposure limit reached: {total_exp:.2f}"

        return True, "ok"

    # ── Circuit breakers ─────────────────────────────────────────────────────

    def _check_circuit_breakers(self) -> None:
        """Check drawdown and daily loss limits."""
        # Max drawdown
        drawdown = (self.state.starting_capital_usdc - self.state.cash_usdc) / self.state.starting_capital_usdc
        if drawdown >= self.config.max_drawdown_pct:
            self._halt(f"Max drawdown reached: {drawdown:.1%}")
            return

        # Daily loss limit
        if abs(self.state.daily_pnl) >= self.config.daily_loss_limit_usdc and self.state.daily_pnl < 0:
            self._halt(f"Daily loss limit reached: {self.state.daily_pnl:.2f} USDC")

    def _halt(self, reason: str) -> None:
        if not self.state.trading_halted:
            self.state.trading_halted = True
            self.state.halt_reason = reason
            print(f"[risk] TRADING HALTED — {reason}")

    def resume_trading(self) -> None:
        """Manually resume after halt (requires human confirmation)."""
        self.state.trading_halted = False
        self.state.halt_reason = None
        print("[risk] Trading resumed by operator")

    # ── Reporting ────────────────────────────────────────────────────────────

    def summary(self) -> dict:
        return {
            "cash_usdc": round(self.state.cash_usdc, 2),
            "total_realized_pnl": round(self.state.total_realized_pnl, 2),
            "daily_pnl": round(self.state.daily_pnl, 2),
            "total_exposure": round(self.get_total_exposure(), 2),
            "trading_halted": self.state.trading_halted,
            "halt_reason": self.state.halt_reason,
            "open_positions": len([p for p in self.state.positions.values()
                                   if p.yes_shares > 0 or p.no_shares > 0]),
        }
