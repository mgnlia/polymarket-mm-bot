"""Tests for risk manager — circuit breakers, exposure limits, position tracking.

Updated to reflect:
  B3 fix: drawdown measured on portfolio value (cash + MTM), not cash alone
  B4 fix: fees NOT included in avg_cost; tracked separately
"""
import pytest
from risk import RiskManager
from config import Config


def _make_risk(capital: float = 500.0) -> RiskManager:
    config = Config()
    return RiskManager(config, starting_capital=capital)


# ── Basic state ───────────────────────────────────────────────────────────────

def test_initial_state():
    """Risk manager starts with correct initial state."""
    rm = _make_risk(500.0)
    assert rm.state.cash_usdc == 500.0
    assert rm.state.trading_halted is False
    assert rm.state.daily_pnl == 0.0
    assert rm.state.peak_portfolio_value == 500.0


def test_can_trade_basic():
    """Can trade when within limits."""
    rm = _make_risk(500.0)
    allowed, reason = rm.can_trade("mkt-1", 50.0)
    assert allowed is True
    assert reason == "ok"


def test_cannot_trade_insufficient_cash():
    """Cannot trade when cash < order size."""
    rm = _make_risk(10.0)
    allowed, reason = rm.can_trade("mkt-1", 50.0)
    assert allowed is False
    assert "cash" in reason.lower()


def test_cannot_trade_when_halted():
    """Cannot trade when trading is halted."""
    rm = _make_risk(500.0)
    rm._halt("test halt")
    allowed, reason = rm.can_trade("mkt-1", 50.0)
    assert allowed is False
    assert "halted" in reason.lower()


# ── B4 fix: fee accounting ────────────────────────────────────────────────────

def test_avg_cost_excludes_fees():
    """
    B4 regression: avg_cost should be based on price only, not price+fee.
    Fees are tracked separately in total_fees_paid.
    """
    rm = _make_risk(500.0)
    rm.record_fill("mkt-1", "Test?", "YES", "BUY", shares=10.0, price=0.50, fee=0.25)
    pos = rm.state.positions["mkt-1"]

    # avg_cost should be 0.50 (price only), NOT (10*0.50 + 0.25) / 10 = 0.525
    assert abs(pos.yes_avg_cost - 0.50) < 0.001, (
        f"avg_cost should be 0.50 (price only), got {pos.yes_avg_cost}"
    )
    # Cash: paid price + fee
    assert abs(rm.state.cash_usdc - (500.0 - 10 * 0.50 - 0.25)) < 0.001
    # Fee tracked separately
    assert abs(pos.total_fees_paid - 0.25) < 0.001


def test_realized_pnl_no_double_fee():
    """
    B4 regression: realized_pnl should be (exit_price - avg_cost) * shares.
    Fee should NOT be subtracted again from pnl (it was already deducted from cash).
    """
    rm = _make_risk(500.0)
    rm.record_fill("mkt-1", "Test?", "YES", "BUY", shares=10.0, price=0.50, fee=0.10)
    rm.record_fill("mkt-1", "Test?", "YES", "SELL", shares=10.0, price=0.80, fee=0.10)
    pos = rm.state.positions["mkt-1"]

    # realized_pnl = (0.80 - 0.50) * 10 = 3.00 (no fee subtraction)
    assert abs(pos.realized_pnl - 3.00) < 0.001, (
        f"realized_pnl should be 3.00 (fees not double-counted), got {pos.realized_pnl}"
    )
    # Total fees = 0.10 + 0.10 = 0.20
    assert abs(pos.total_fees_paid - 0.20) < 0.001
    # Net P&L = 3.00 - 0.20 = 2.80 (reported in summary)
    s = rm.summary()
    assert abs(s["net_pnl_after_fees"] - 2.80) < 0.001


def test_cash_accounting_buy_sell():
    """Cash is debited correctly on buy (price+fee) and credited on sell (price-fee)."""
    rm = _make_risk(500.0)
    rm.record_fill("mkt-1", "Test?", "YES", "BUY", shares=10.0, price=0.50, fee=0.05)
    # After buy: cash = 500 - (10*0.50 + 0.05) = 500 - 5.05 = 494.95
    assert abs(rm.state.cash_usdc - 494.95) < 0.001

    rm.record_fill("mkt-1", "Test?", "YES", "SELL", shares=10.0, price=0.80, fee=0.05)
    # After sell: cash = 494.95 + (10*0.80 - 0.05) = 494.95 + 7.95 = 502.90
    assert abs(rm.state.cash_usdc - 502.90) < 0.001


# ── B3 fix: drawdown uses portfolio value, not cash ───────────────────────────

def test_drawdown_uses_portfolio_value_not_cash():
    """
    B3 regression: buying shares that appreciate should NOT trigger drawdown.
    Old bug: drawdown = (starting_capital - cash) / starting_capital
    This would halt trading even when portfolio is UP.
    """
    config = Config()
    config.max_drawdown_pct = 0.20  # 20% drawdown limit
    rm = RiskManager(config, starting_capital=100.0)

    # Buy 50 shares at $0.50 → spend $25 cash (25% of capital)
    # Old code would see drawdown = (100-75)/100 = 25% → HALT (wrong!)
    rm.record_fill(
        "mkt-1", "Test?", "YES", "BUY",
        shares=50.0, price=0.50, fee=0.0,
        market_prices={"mkt-1": (0.50, 0.50)},  # mark at cost → no unrealized gain
    )

    # Portfolio value = cash($75) + shares(50 * $0.50 = $25) = $100 → no drawdown
    portfolio_val = rm.get_portfolio_value({"mkt-1": (0.50, 0.50)})
    assert abs(portfolio_val - 100.0) < 0.01

    # Should NOT be halted — portfolio value is unchanged
    assert rm.state.trading_halted is False, (
        "Should NOT halt when portfolio value is unchanged (only cash decreased)"
    )


def test_drawdown_triggers_on_actual_loss():
    """Drawdown circuit breaker fires when portfolio value genuinely drops."""
    config = Config()
    config.max_drawdown_pct = 0.20  # 20%
    rm = RiskManager(config, starting_capital=100.0)

    # Buy 50 shares at $0.50 → portfolio = $100
    rm.record_fill(
        "mkt-1", "Test?", "YES", "BUY",
        shares=50.0, price=0.50, fee=0.0,
        market_prices={"mkt-1": (0.50, 0.50)},
    )
    assert rm.state.trading_halted is False

    # Price crashes to $0.10 → portfolio = $75 cash + 50*$0.10 = $80 → 20% drawdown
    rm._check_circuit_breakers(market_prices={"mkt-1": (0.10, 0.90)})
    assert rm.state.trading_halted is True, (
        "Should halt when portfolio value drops 20% from peak"
    )


def test_peak_portfolio_value_updates():
    """Peak portfolio value is updated when portfolio appreciates."""
    rm = _make_risk(100.0)
    assert rm.state.peak_portfolio_value == 100.0

    # Buy shares that appreciate
    rm.record_fill(
        "mkt-1", "Test?", "YES", "BUY",
        shares=10.0, price=0.50, fee=0.0,
        market_prices={"mkt-1": (0.80, 0.20)},  # price jumped to 0.80
    )
    # Portfolio = cash($95) + 10*$0.80 = $95 + $8 = $103 > $100 → new peak
    portfolio_val = rm.get_portfolio_value({"mkt-1": (0.80, 0.20)})
    assert portfolio_val > 100.0
    assert rm.state.peak_portfolio_value >= 100.0


# ── Daily loss circuit breaker ────────────────────────────────────────────────

def test_daily_loss_circuit_breaker():
    """Trading halts when daily loss limit is exceeded."""
    config = Config()
    config.daily_loss_limit_usdc = 50.0
    rm = RiskManager(config, starting_capital=500.0)

    # Buy at 0.80, sell at 0.20 → realized loss of $60 on 100 shares
    rm.record_fill("mkt-1", "Test?", "YES", "BUY", shares=100.0, price=0.80, fee=0.0)
    rm.record_fill("mkt-1", "Test?", "YES", "SELL", shares=100.0, price=0.20, fee=0.0)
    assert rm.state.trading_halted is True


# ── Position tracking ─────────────────────────────────────────────────────────

def test_resume_trading():
    """Can resume trading after halt."""
    rm = _make_risk(500.0)
    rm._halt("test")
    assert rm.state.trading_halted is True
    rm.resume_trading()
    assert rm.state.trading_halted is False


def test_position_tracking():
    """Position is correctly tracked after fills."""
    rm = _make_risk(500.0)
    rm.record_fill("mkt-1", "Test?", "YES", "BUY", shares=10.0, price=0.50, fee=0.0)
    pos = rm.state.positions["mkt-1"]
    assert pos.yes_shares == 10.0
    assert abs(pos.yes_avg_cost - 0.50) < 0.001
    assert abs(rm.state.cash_usdc - 495.0) < 0.01  # 500 - 10*0.5


def test_realized_pnl_on_sell():
    """Realized P&L is computed correctly on a winning trade."""
    rm = _make_risk(500.0)
    rm.record_fill("mkt-1", "Test?", "YES", "BUY", shares=10.0, price=0.50, fee=0.0)
    rm.record_fill("mkt-1", "Test?", "YES", "SELL", shares=10.0, price=0.80, fee=0.0)
    pos = rm.state.positions["mkt-1"]
    assert abs(pos.realized_pnl - 3.0) < 0.01  # (0.80-0.50)*10 = 3.0


def test_no_position_tracking():
    """NO token positions are tracked correctly."""
    rm = _make_risk(500.0)
    rm.record_fill("mkt-1", "Test?", "NO", "BUY", shares=20.0, price=0.40, fee=0.0)
    pos = rm.state.positions["mkt-1"]
    assert pos.no_shares == 20.0
    assert abs(pos.no_avg_cost - 0.40) < 0.001
    assert abs(rm.state.cash_usdc - (500.0 - 20 * 0.40)) < 0.001


def test_avg_cost_weighted_average():
    """Avg cost is correctly weighted when buying more shares."""
    rm = _make_risk(500.0)
    rm.record_fill("mkt-1", "Test?", "YES", "BUY", shares=10.0, price=0.40, fee=0.0)
    rm.record_fill("mkt-1", "Test?", "YES", "BUY", shares=10.0, price=0.60, fee=0.0)
    pos = rm.state.positions["mkt-1"]
    # Avg cost = (10*0.40 + 10*0.60) / 20 = 0.50
    assert abs(pos.yes_avg_cost - 0.50) < 0.001
    assert pos.yes_shares == 20.0


def test_summary_output():
    """Summary dict has expected keys including new fee tracking fields."""
    rm = _make_risk(500.0)
    s = rm.summary()
    assert "cash_usdc" in s
    assert "total_realized_pnl" in s
    assert "total_fees_paid" in s
    assert "net_pnl_after_fees" in s
    assert "daily_pnl" in s
    assert "trading_halted" in s
    assert "open_positions" in s
    assert "peak_portfolio_value" in s


def test_get_portfolio_value_no_positions():
    """Portfolio value with no positions equals cash."""
    rm = _make_risk(500.0)
    assert abs(rm.get_portfolio_value() - 500.0) < 0.001


def test_get_portfolio_value_with_positions():
    """Portfolio value = cash + mark-to-market positions."""
    rm = _make_risk(500.0)
    rm.record_fill("mkt-1", "Test?", "YES", "BUY", shares=10.0, price=0.50, fee=0.0)
    # cash = 495, yes_shares = 10 at price 0.70
    val = rm.get_portfolio_value({"mkt-1": (0.70, 0.30)})
    # 495 + 10 * 0.70 = 495 + 7 = 502
    assert abs(val - 502.0) < 0.001
