"""FastAPI server — exposes bot state for the dashboard.

New endpoints (Builder Program):
  GET /api/builder         — builder volume, rewards, rank, tier
  GET /api/builder/orders  — attributed order history
"""
from __future__ import annotations

import asyncio
import os
import secrets
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Security, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from bot import MarketMakerBot
from builder_auth import BuilderCreds, init_clob_client_with_builder, init_relayer_client
from config import Config

# ── Global bot instance ──────────────────────────────────────────────────────

_bot: Optional[MarketMakerBot] = None
_bot_task: Optional[asyncio.Task] = None

# ── Bearer token auth ────────────────────────────────────────────────────────

_bearer_scheme = HTTPBearer(auto_error=False)


def _get_control_token() -> str:
    token = os.environ.get("BOT_CONTROL_TOKEN", "")
    if not token:
        token = secrets.token_urlsafe(32)
        print(
            f"[api] WARNING: BOT_CONTROL_TOKEN not set. "
            f"Using random token for this session: {token!r}\n"
            f"         Set BOT_CONTROL_TOKEN in your environment to persist this."
        )
    return token


_CONTROL_TOKEN: str = _get_control_token()


def require_control_auth(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer_scheme),
) -> None:
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not secrets.compare_digest(credentials.credentials, _CONTROL_TOKEN):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid control token",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ── App lifecycle ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _bot, _bot_task
    config = Config.from_env()

    # Initialize with builder attribution if creds are set
    builder_creds = BuilderCreds.from_env()
    try:
        config.validate()
        clob_client, is_builder = init_clob_client_with_builder(config, builder_creds)
        relayer_client = init_relayer_client(config, builder_creds) if is_builder else None
    except ValueError:
        clob_client = None
        relayer_client = None

    _bot = MarketMakerBot(
        config,
        clob_client=clob_client,
        builder_creds=builder_creds,
        relayer_client=relayer_client,
    )
    _bot_task = asyncio.create_task(_bot.start())
    yield
    if _bot:
        await _bot.stop()
    if _bot_task:
        _bot_task.cancel()


app = FastAPI(
    title="Polymarket MM Bot API",
    description="Triple revenue stream: spread capture + liquidity rewards + builder rewards",
    version="2.0.0",
    lifespan=lifespan,
)

_raw_origins = os.environ.get("CORS_ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:3001")
_allowed_origins = [o.strip() for o in _raw_origins.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
    allow_credentials=False,
)


# ── Read-only endpoints ───────────────────────────────────────────────────────

@app.get("/health")
async def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        # Use public property — not _bot._builder_creds
        "builder_enabled": _bot.builder_creds.configured if _bot else False,
    }


@app.get("/api/status")
async def get_status() -> Dict[str, Any]:
    if not _bot:
        return {"status": "not_started"}
    return {
        "status": _bot.stats.get("status", "unknown"),
        "uptime_s": _bot.stats.get("uptime_s", 0),
        "quote_cycles": _bot.stats.get("quote_cycles", 0),
        "markets_quoted": _bot.stats.get("markets_quoted", 0),
        "dry_run": _bot.quoter._client is None,
        # Use public property — not _bot._builder_creds
        "builder_enabled": _bot.builder_creds.configured,
        "errors": _bot.stats.get("errors", []),
    }


@app.get("/api/risk")
async def get_risk() -> Dict[str, Any]:
    if not _bot:
        return {}
    return _bot.risk.summary()


@app.get("/api/rewards")
async def get_rewards() -> Dict[str, Any]:
    """Liquidity rewards (existing MM program)."""
    if not _bot:
        return {}
    s = _bot.rewards.summary
    return {
        "today_usdc": round(s.today_earned_usdc, 4),
        "week_usdc": round(s.week_earned_usdc, 4),
        "month_usdc": round(s.month_earned_usdc, 4),
        "total_usdc": round(s.total_earned_usdc, 4),
        "by_market": s.by_market,
        "last_updated": s.last_updated.isoformat() if s.last_updated else None,
    }


@app.get("/api/builder")
async def get_builder_stats() -> Dict[str, Any]:
    """
    Builder Program stats — volume attribution, weekly rewards, leaderboard rank.

    Revenue stream #3: weekly USDC rewards from Polymarket Builder Program.
    """
    if not _bot:
        return {"error": "bot not started"}

    # Use public property — not _bot._builder_rewards
    summary = _bot.builder_rewards.summary()
    summary["revenue_streams"] = {
        "spread_capture": "active",
        "liquidity_rewards": "active",
        # Use public property — not _bot._builder_creds
        "builder_rewards": "active" if _bot.builder_creds.configured else "disabled",
    }
    return summary


@app.get("/api/builder/orders")
async def get_builder_orders(limit: int = 50) -> Dict[str, Any]:
    """
    Recent orders attributed to the builder profile.
    Returns last `limit` orders (default 50).
    """
    if not _bot:
        return {"orders": [], "total": 0}

    # Use public property — not _bot._builder_rewards
    br = _bot.builder_rewards
    orders = br._orders[-limit:]
    return {
        "total": len(br._orders),
        "returned": len(orders),
        "orders": [
            {
                "order_id": o.order_id,
                "condition_id": o.market_condition_id[:16] + "...",
                "side": o.side,
                "price": round(o.price, 4),
                "size_usdc": round(o.size_usdc, 2),
                "attributed": o.attributed,
                "timestamp": o.timestamp,
            }
            for o in reversed(orders)
        ],
    }


@app.get("/api/markets")
async def get_markets() -> Dict[str, Any]:
    if not _bot:
        return {"markets": []}
    # Use public property — not _bot._active_markets
    markets = [
        {
            "condition_id": m.condition_id,
            "question": m.question,
            "yes_price": m.yes_price,
            "no_price": m.no_price,
            "volume_24h": m.volume_24h,
            "rewards_min_size": m.rewards_min_size,
            "rewards_max_spread": m.rewards_max_spread,
            "category": m.category,
            "is_extreme": m.is_extreme,
            "reward_score": round(m.reward_score, 4),
        }
        for m in _bot.active_markets
    ]
    return {"markets": markets, "count": len(markets)}


@app.get("/api/positions")
async def get_positions() -> Dict[str, Any]:
    if not _bot:
        return {"positions": []}
    positions = []
    for cid, pos in _bot.risk.state.positions.items():
        positions.append({
            "condition_id": cid,
            "question": pos.question,
            "yes_shares": round(pos.yes_shares, 4),
            "no_shares": round(pos.no_shares, 4),
            "yes_avg_cost": round(pos.yes_avg_cost, 4),
            "no_avg_cost": round(pos.no_avg_cost, 4),
            "realized_pnl": round(pos.realized_pnl, 4),
            "total_fees_paid": round(pos.total_fees_paid, 4),
        })
    return {"positions": positions}


# ── Control endpoints ─────────────────────────────────────────────────────────

@app.post("/api/bot/stop", dependencies=[Depends(require_control_auth)])
async def stop_bot() -> Dict[str, str]:
    if _bot:
        await _bot.stop()
    return {"message": "Bot stopped"}


@app.post("/api/bot/resume", dependencies=[Depends(require_control_auth)])
async def resume_bot() -> Dict[str, str]:
    if _bot:
        _bot.risk.resume_trading()
    return {"message": "Trading resumed"}
