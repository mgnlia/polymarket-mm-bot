"""FastAPI server — exposes bot state for the dashboard."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, Dict

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from bot import MarketMakerBot
from config import Config

# Global bot instance
_bot: MarketMakerBot | None = None
_bot_task: asyncio.Task | None = None


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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


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
            "incentive_size": m.incentive_size,
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
        })
    return {"positions": positions}


@app.post("/api/bot/stop")
async def stop_bot() -> Dict[str, str]:
    if _bot:
        await _bot.stop()
    return {"message": "Bot stopped"}


@app.post("/api/bot/resume")
async def resume_bot() -> Dict[str, str]:
    if _bot:
        _bot.risk.resume_trading()
    return {"message": "Trading resumed"}
