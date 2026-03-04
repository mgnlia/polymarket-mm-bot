"""FastAPI server — exposes bot state for the dashboard."""
from __future__ import annotations

import asyncio
import os
import secrets
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Security, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from bot import MarketMakerBot
from config import Config

# Global bot instance
_bot: MarketMakerBot | None = None
_bot_task: asyncio.Task | None = None

# ── B5 FIX: Bearer token auth for control endpoints ──────────────────────────

_bearer_scheme = HTTPBearer(auto_error=False)

def _get_control_token() -> str:
    """
    Load the control API token from environment.
    Falls back to a random token (logged at startup) if not set.
    In production, always set BOT_CONTROL_TOKEN in your environment.
    """
    token = os.environ.get("BOT_CONTROL_TOKEN", "")
    if not token:
        # Generate a random token and warn — this happens only if the env var
        # is missing, which means the operator forgot to set it.
        token = secrets.token_urlsafe(32)
        print(
            f"[api] WARNING: BOT_CONTROL_TOKEN not set. "
            f"Using random token for this session: {token!r}\n"
            f"         Set BOT_CONTROL_TOKEN in your environment to persist this."
        )
    return token


# Resolved once at import time (module-level singleton)
_CONTROL_TOKEN: str = _get_control_token()


def require_control_auth(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer_scheme),
) -> None:
    """
    FastAPI dependency — enforces Bearer token on control endpoints.
    Raises 401 if the token is missing or incorrect.
    Uses constant-time comparison to prevent timing attacks.
    """
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # secrets.compare_digest prevents timing-based token enumeration
    if not secrets.compare_digest(credentials.credentials, _CONTROL_TOKEN):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid control token",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ── App lifecycle ────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _bot, _bot_task
    config = Config.from_env()
    _bot = MarketMakerBot(config, clob_client=None)  # dry-run until keys provided
    _bot_task = asyncio.create_task(_bot.start())
    yield
    if _bot:
        await _bot.stop()
    if _bot_task:
        _bot_task.cancel()


app = FastAPI(title="Polymarket MM Bot API", lifespan=lifespan)

# B5 FIX: Restrict CORS origins instead of allowing *
# Read allowed origins from env (comma-separated); default to localhost only.
_raw_origins = os.environ.get("CORS_ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:3001")
_allowed_origins = [o.strip() for o in _raw_origins.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
    allow_credentials=False,
)


# ── Read-only endpoints (no auth required) ───────────────────────────────────

@app.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "ok"}


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
        "errors": _bot.stats.get("errors", []),
    }


@app.get("/api/risk")
async def get_risk() -> Dict[str, Any]:
    if not _bot:
        return {}
    return _bot.risk.summary()


@app.get("/api/rewards")
async def get_rewards() -> Dict[str, Any]:
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


@app.get("/api/markets")
async def get_markets() -> Dict[str, Any]:
    if not _bot:
        return {"markets": []}
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
        for m in _bot._active_markets
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


# ── Control endpoints (require Bearer token — B5 fix) ────────────────────────

@app.post("/api/bot/stop", dependencies=[Depends(require_control_auth)])
async def stop_bot() -> Dict[str, str]:
    """
    Halt the bot. Requires Authorization: Bearer <BOT_CONTROL_TOKEN>.
    """
    if _bot:
        await _bot.stop()
    return {"message": "Bot stopped"}


@app.post("/api/bot/resume", dependencies=[Depends(require_control_auth)])
async def resume_bot() -> Dict[str, str]:
    """
    Resume trading after a halt. Requires Authorization: Bearer <BOT_CONTROL_TOKEN>.
    """
    if _bot:
        _bot.risk.resume_trading()
    return {"message": "Trading resumed"}
