# Polymarket Market Maker Bot

Automated market-making bot for [Polymarket](https://polymarket.com) that earns daily USDC liquidity rewards by providing two-sided quotes on prediction markets.

## Strategy

### Reward Formula
Polymarket rewards market makers using a quadratic scoring formula:

```
S(v, s) = ((v - s) / v)² × b
```

- `v` = max spread config (your tolerance)
- `s` = actual spread from midpoint (tighter = exponentially more rewards)
- `b` = market's daily reward pool (USDC)

**Key insight:** Quoting at 2¢ spread on a 5¢ max earns `((0.05-0.02)/0.05)² = 0.36 = 36%` of the pool vs 0% at 5¢.

### Two-Sided Depth Requirement
- Two-sided quotes = **full score**
- Single-sided = **1/3 score** (c=3.0 penalty)
- Extreme markets (<10¢ or >90¢) **must** post both sides

### Revenue Potential
- Rewards sampled every minute, paid daily at midnight UTC
- Minimum payout: $1 USDC/day
- Estimated: $20–50/day with $500 capital across 10 markets

## Architecture

```
bot.py          ← Main orchestration loop
scanner.py      ← Fetch & score rewarded markets (Gamma API)
quoter.py       ← Build & place two-sided limit orders
risk.py         ← Position tracking, exposure limits, circuit breakers
hedger.py       ← Delta-neutral rebalancing
rewards.py      ← Track daily USDC payouts
config.py       ← All parameters (env-configurable)
api.py          ← FastAPI server for dashboard
```

## Setup

### Prerequisites
- Python 3.9+
- Polygon wallet with USDC (start with $500 recommended)
- Polymarket account with API access

### Install

```bash
# Clone
git clone https://github.com/mgnlia/polymarket-mm-bot
cd polymarket-mm-bot

# Install with uv (recommended)
pip install uv
uv pip install -e .

# Or pip
pip install -e .
```

### Configure

```bash
cp .env.example .env
# Edit .env with your PRIVATE_KEY and FUNDER_ADDRESS
```

### Run

```bash
# Bot only (dry-run if no keys)
python bot.py

# With API server (for dashboard)
uvicorn api:app --host 0.0.0.0 --port 8000

# Docker
docker build -t mm-bot .
docker run --env-file .env -p 8000:8000 mm-bot
```

## Risk Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `MAX_SPREAD_PCT` | 5% | Max spread tolerance (v in formula) |
| `TARGET_SPREAD_PCT` | 2% | Quote tightness target |
| `ORDER_SIZE_USDC` | $50 | USDC per side per market |
| `MAX_POSITION_PER_MARKET_USDC` | $50 | Max net exposure per market |
| `MAX_TOTAL_EXPOSURE_USDC` | $500 | Total portfolio cap |
| `MAX_DRAWDOWN_PCT` | 20% | Auto-halt threshold |
| `DAILY_LOSS_LIMIT_USDC` | $100 | Daily stop-loss |

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Health check |
| `GET /api/status` | Bot status, uptime, cycles |
| `GET /api/risk` | Risk summary, P&L, exposure |
| `GET /api/rewards` | Daily/weekly/monthly rewards |
| `GET /api/markets` | Active markets being quoted |
| `GET /api/positions` | Current positions |
| `POST /api/bot/stop` | Stop bot (cancels all orders) |
| `POST /api/bot/resume` | Resume after halt |

## Airdrop Positioning

The upcoming $POLY token airdrop is expected to reward:
- ✅ Trading volume (market making generates continuous volume)
- ✅ Liquidity provision (core function of this bot)
- ✅ Market diversity (configured across 6 categories)
- ✅ Active months (24/7 Railway deployment)

## Disclaimer

This bot trades real money. Start with small capital ($50–100) to validate performance before scaling. Market making carries inventory risk — prices can move against your positions.
