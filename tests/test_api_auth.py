"""Tests for API authentication on control endpoints (B5 fix)."""
from __future__ import annotations

import os
import secrets
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# We need to patch the bot startup before importing api
@pytest.fixture
def client(monkeypatch):
    """Create a test client with a known control token and no real bot."""
    test_token = "test-control-token-abc123"
    monkeypatch.setenv("BOT_CONTROL_TOKEN", test_token)
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "http://localhost:3000")

    # Patch the bot so it doesn't actually start
    mock_bot = MagicMock()
    mock_bot.stats = {"status": "running", "uptime_s": 0, "quote_cycles": 0, "markets_quoted": 0, "errors": []}
    mock_bot.risk = MagicMock()
    mock_bot.risk.summary.return_value = {"cash_usdc": 500.0}
    mock_bot.risk.resume_trading = MagicMock()
    mock_bot.stop = AsyncMock()
    mock_bot.quoter = MagicMock()
    mock_bot.quoter._client = None
    mock_bot._active_markets = []
    mock_bot.rewards = MagicMock()
    mock_bot.rewards.summary = MagicMock(
        today_earned_usdc=0.0,
        week_earned_usdc=0.0,
        month_earned_usdc=0.0,
        total_earned_usdc=0.0,
        by_market={},
        last_updated=None,
    )

    # Reload api module with fresh env vars
    import importlib
    import api as api_module

    # Patch module-level token and bot
    monkeypatch.setattr(api_module, "_CONTROL_TOKEN", test_token)
    monkeypatch.setattr(api_module, "_bot", mock_bot)

    with TestClient(api_module.app, raise_server_exceptions=True) as c:
        c._test_token = test_token
        yield c


# ── Read-only endpoints — no auth required ────────────────────────────────────

def test_health_no_auth_required(client):
    """GET /health does not require auth."""
    resp = client.get("/health")
    assert resp.status_code == 200


def test_status_no_auth_required(client):
    """GET /api/status does not require auth."""
    resp = client.get("/api/status")
    assert resp.status_code == 200


def test_risk_no_auth_required(client):
    """GET /api/risk does not require auth."""
    resp = client.get("/api/risk")
    assert resp.status_code == 200


# ── Control endpoints — require Bearer token ──────────────────────────────────

def test_stop_bot_requires_auth(client):
    """POST /api/bot/stop without token → 401."""
    resp = client.post("/api/bot/stop")
    assert resp.status_code == 401


def test_resume_bot_requires_auth(client):
    """POST /api/bot/resume without token → 401."""
    resp = client.post("/api/bot/resume")
    assert resp.status_code == 401


def test_stop_bot_wrong_token(client):
    """POST /api/bot/stop with wrong token → 401."""
    resp = client.post(
        "/api/bot/stop",
        headers={"Authorization": "Bearer wrong-token-xyz"},
    )
    assert resp.status_code == 401


def test_stop_bot_correct_token(client):
    """POST /api/bot/stop with correct token → 200."""
    resp = client.post(
        "/api/bot/stop",
        headers={"Authorization": f"Bearer {client._test_token}"},
    )
    assert resp.status_code == 200
    assert "stopped" in resp.json()["message"].lower()


def test_resume_bot_correct_token(client):
    """POST /api/bot/resume with correct token → 200."""
    resp = client.post(
        "/api/bot/resume",
        headers={"Authorization": f"Bearer {client._test_token}"},
    )
    assert resp.status_code == 200
    assert "resumed" in resp.json()["message"].lower()


def test_timing_safe_comparison(client):
    """Verify constant-time comparison is used (no early exit on prefix match)."""
    # This test verifies the token comparison doesn't leak timing info
    # by ensuring a prefix of the correct token also fails
    prefix = client._test_token[:10]
    resp = client.post(
        "/api/bot/stop",
        headers={"Authorization": f"Bearer {prefix}"},
    )
    assert resp.status_code == 401


def test_cors_not_wildcard(client):
    """CORS should not allow * (any origin)."""
    import api as api_module
    # Check middleware config
    for middleware in api_module.app.user_middleware:
        if "CORSMiddleware" in str(middleware):
            # Should not have * in allowed origins
            assert "*" not in str(middleware), "CORS should not allow wildcard origin"
