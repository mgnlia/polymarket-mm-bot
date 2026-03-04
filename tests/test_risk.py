"""Tests for risk manager — circuit breakers, exposure limits, position tracking."""
import pytest
from risk import RiskManager
from config import Config


def _make_risk(capital: float = 500.0) -> RiskManager:
    config = Config()
    return RiskManager(config, starting_capital=capital)


def test_initial_state():
    """Risk manager starts with correct initial state."""
    rm = _make_risk(500.0)
    assert rm.state.cash_usdc == 500.0
    assert rm.state.trading_halted is False
    assert rm.state.daily_pnl == 0.0


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


def test_drawdown_circuit_breaker():
    """Trading halts when drawdown exceeds max_drawdown_pct."""
    config = Config()
    config.max_drawdown_pct = 0.20  # 20%
    rm = RiskManager(config, starting_capital=100.0)

    # Simulate a losing trade: spend $25 (25% drawdown)
    rm.record_fill("mkt-1", "Test?", "YES", "BUY", shares=25.0, price=1.0, fee=0.0)
    assert rm.state.trading_halted is True
    assert rm.state.halt_reason is not None


def test_daily_loss_circuit_breaker():
    """Trading halts when daily loss limit is exceeded."""
    config = Config()
    config.daily_loss_limit_usdc = 50.0
    rm = RiskManager(config, starting_capital=500.0)

    # Buy at 0.80, sell at 0.20 → loss of $60 on 100 shares
    rm.record_fill("mkt-1", "Test?", "YES", "BUY", shares=100.0, price=0.80, fee=0.0)
    rm.record_fill("mkt-1", "Test?", "YES", "SELL", shares=100.0, price=0.20, fee=0.0)
    assert rm.state.trading_halted is True


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


def test_summary_output():
    """Summary dict has expected keys."""
    rm = _make_risk(500.0)
    s = rm.summary()
    assert "cash_usdc" in s
    assert "total_realized_pnl" in s
    assert "daily_pnl" in s
    assert "trading_halted" in s
    assert "open_positions" in s
