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
    yes_avg_cost: float = 0.0   # avg cost per share (excluding fees — fees tracked separately)
    no_avg_cost: float = 0.0
    realized_pnl: float = 0.0
    total_fees_paid: float = 0.0
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
    # B3 FIX: track peak portfolio value for proper drawdown calculation
    peak_portfolio_value: float = 0.0


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
            peak_portfolio_value=starting_capital,
        )

    # ── Portfolio value ──────────────────────────────────────────────────────

    def get_portfolio_value(self, market_prices: Optional[Dict[str, tuple[float, float]]] = None) -> float:
        """
        Compute total portfolio value = cash + mark-to-market positions.

        market_prices: dict of condition_id → (yes_price, no_price)
        If not provided, uses avg_cost as a conservative estimate.
        """
        mtm = 0.0
        for cid, pos in self.state.positions.items():
            if market_prices and cid in market_prices:
                yes_px, no_px = market_prices[cid]
            else:
                # Conservative: mark at avg cost (no unrealized gain/loss)
                yes_px = pos.yes_avg_cost
                no_px = pos.no_avg_cost
            mtm += pos.yes_shares * yes_px + pos.no_shares * no_px
        return self.state.cash_usdc + mtm

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
        market_prices: Optional[Dict[str, tuple[float, float]]] = None,
    ) -> None:
        """
        Update position and cash after a fill.

        B4 FIX: Fees are tracked separately and NOT included in avg_cost.
        - avg_cost = weighted average of execution prices only (no fees)
        - realized_pnl = (exit_price - avg_cost) * shares
        - total_fees_paid accumulates all fees paid
        - Net P&L = realized_pnl - total_fees_paid (fees deducted once, at the end)

        Cash accounting is exact:
        - BUY:  cash -= shares * price + fee   (pay price + fee)
        - SELL: cash += shares * price - fee   (receive price, pay fee)
        """
        pos = self.state.positions.setdefault(
            condition_id, Position(condition_id=condition_id, question=question)
        )

        if token_side == "YES":
            if side == "BUY":
                # Update avg cost using PRICE only (not price+fee) — B4 fix
                total_shares = pos.yes_shares + shares
                if total_shares > 0:
                    pos.yes_avg_cost = (pos.yes_shares * pos.yes_avg_cost + shares * price) / total_shares
                pos.yes_shares = total_shares
                self.state.cash_usdc -= (shares * price + fee)
            else:  # SELL
                # P&L = (exit_price - avg_cost) * shares — fees tracked separately (B4 fix)
                pnl = (price - pos.yes_avg_cost) * shares
                pos.yes_shares -= shares
                pos.realized_pnl += pnl
                self.state.cash_usdc += (shares * price - fee)
                self.state.daily_pnl += pnl
                self.state.total_realized_pnl += pnl
        else:  # NO token
            if side == "BUY":
                total_shares = pos.no_shares + shares
                if total_shares > 0:
                    pos.no_avg_cost = (pos.no_shares * pos.no_avg_cost + shares * price) / total_shares
                pos.no_shares = total_shares
                self.state.cash_usdc -= (shares * price + fee)
            else:
                pnl = (price - pos.no_avg_cost) * shares
                pos.no_shares -= shares
                pos.realized_pnl += pnl
                self.state.cash_usdc += (shares * price - fee)
                self.state.daily_pnl += pnl
                self.state.total_realized_pnl += pnl

        # Track fees separately for net P&L reporting
        pos.total_fees_paid += fee

        # B3 FIX: update peak portfolio value after each fill
        portfolio_val = self.get_portfolio_value(market_prices)
        if portfolio_val > self.state.peak_portfolio_value:
            self.state.peak_portfolio_value = portfolio_val

        self._check_circuit_breakers(market_prices)

    def get_net_yes_exposure(self, condition_id: str) -> float:
        """
        Net YES exposure in shares for a market.
        Positive = net long YES, negative = net short YES (long NO).
        """
        pos = self.state.positions.get(condition_id)
        if not pos:
            return 0.0
        return pos.yes_shares - pos.no_shares

    def get_total_exposure(self) -> float:
        """Total absolute share exposure across all markets."""
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

        # Per-market position limit (in USDC notional)
        pos = self.state.positions.get(condition_id)
        if pos:
            market_exposure = (pos.yes_shares + pos.no_shares) * 0.5  # rough midpoint estimate
            if market_exposure + size_usdc > self.config.max_position_per_market_usdc:
                return False, f"Market exposure limit reached for {condition_id[:16]}"

        # Total exposure limit (in shares — compare to USDC limit at ~0.5 avg price)
        total_exp = self.get_total_exposure() * 0.5  # rough USDC value
        if total_exp + size_usdc > self.config.max_total_exposure_usdc:
            return False, f"Total exposure limit reached: {total_exp:.2f}"

        return True, "ok"

    # ── Circuit breakers ─────────────────────────────────────────────────────

    def _check_circuit_breakers(
        self,
        market_prices: Optional[Dict[str, tuple[float, float]]] = None,
    ) -> None:
        """
        Check drawdown and daily loss limits.

        B3 FIX: Drawdown is now measured as portfolio value drawdown from peak,
        NOT as cash spent. This correctly handles the case where cash decreases
        because we bought shares that have appreciated in value.

        drawdown = (peak_portfolio_value - current_portfolio_value) / peak_portfolio_value
        """
        portfolio_val = self.get_portfolio_value(market_prices)

        # Max drawdown from peak (portfolio value, not cash)
        if self.state.peak_portfolio_value > 0:
            drawdown = (self.state.peak_portfolio_value - portfolio_val) / self.state.peak_portfolio_value
            if drawdown >= self.config.max_drawdown_pct:
                self._halt(f"Max drawdown reached: {drawdown:.1%} (portfolio ${portfolio_val:.2f} vs peak ${self.state.peak_portfolio_value:.2f})")
                return

        # Daily loss limit (realized P&L only — unrealized swings don't trigger halt)
        if self.state.daily_pnl < 0 and abs(self.state.daily_pnl) >= self.config.daily_loss_limit_usdc:
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
        total_fees = sum(p.total_fees_paid for p in self.state.positions.values())
        return {
            "cash_usdc": round(self.state.cash_usdc, 2),
            "total_realized_pnl": round(self.state.total_realized_pnl, 2),
            "total_fees_paid": round(total_fees, 4),
            "net_pnl_after_fees": round(self.state.total_realized_pnl - total_fees, 4),
            "daily_pnl": round(self.state.daily_pnl, 2),
            "total_exposure": round(self.get_total_exposure(), 2),
            "peak_portfolio_value": round(self.state.peak_portfolio_value, 2),
            "trading_halted": self.state.trading_halted,
            "halt_reason": self.state.halt_reason,
            "open_positions": len([p for p in self.state.positions.values()
                                   if p.yes_shares > 0 or p.no_shares > 0]),
        }
