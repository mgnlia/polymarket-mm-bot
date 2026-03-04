# Polymarket Market Maker Bot

Automated market-making bot for [Polymarket](https://polymarket.com) that earns daily USDC liquidity rewards by providing two-sided quotes on prediction markets.

## Strategy

### Reward Formula
Polymarket rewards market makers using a quadratic scoring formula:

```
S(v, s) = ((v - s) / v)² × b
```

- `v` = `rewardsMaxSpread` — maximum spread (in cents) that still earns rewards
- `s` = your actual quoted spread (tighter = exponentially more rewards)
- `b` = reward pool (not exposed per-market in Gamma API; score is relative)

**Key insight:** Quoting at 1¢ spread on a 2¢ max earns `((2-1)/2)² = 0.25 = 25%` of the pool vs 0% at 2¢+.

### Real Gamma API Fields (verified)
The Gamma API returns these fields as **JSON-encoded strings**, not arrays:
- `clobTokenIds`: `"[\"tokenId_yes\", \"tokenId_no\"]"` — must be `json.loads()`'d
- `outcomePrices`: `"[\"0.62\", \"0.38\"]"` — YES price at index 0
- `outcomes`: `"[\"Yes\", \"No\"]"`
- `rewardsMinSize`: minimum order size (USDC) to qualify for rewards (e.g. 20 USDC)
- `rewardsMaxSpread`: maximum spread in cents to still earn rewards (e.g. 2.0)

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
api.py          ← FastAPI server for dashboard (authenticated control)
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
# Edit .env with your PRIVATE_KEY, FUNDER_ADDRESS, and BOT_CONTROL_TOKEN
```

Key environment variables:

| Variable | Required | Description |
|----------|----------|-------------|
| `PRIVATE_KEY` | Yes (live) | Polygon wallet private key |
| `FUNDER_ADDRESS` | Yes (live) | Wallet address |
| `BOT_CONTROL_TOKEN` | **Yes** | Secret token for stop/resume API (see Security) |
| `CORS_ALLOWED_ORIGINS` | No | Comma-separated allowed origins (default: localhost) |

### Run

```bash
# Bot only (dry-run if no keys)
python bot.py

# With API server (for dashboard)
BOT_CONTROL_TOKEN=your-secret-token uvicorn api:app --host 0.0.0.0 --port 8000

# Docker
docker build -t mm-bot .
docker run --env-file .env -p 8000:8000 mm-bot
```

## Risk Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `MAX_SPREAD_PCT` | 5% | Max spread tolerance |
| `TARGET_SPREAD_PCT` | 2% | Quote tightness target |
| `ORDER_SIZE_USDC` | $50 | USDC per side per market |
| `MAX_POSITION_PER_MARKET_USDC` | $50 | Max net exposure per market |
| `MAX_TOTAL_EXPOSURE_USDC` | $500 | Total portfolio cap |
| `MAX_DRAWDOWN_PCT` | 20% | Auto-halt threshold (portfolio value, not cash) |
| `DAILY_LOSS_LIMIT_USDC` | $100 | Daily realized loss stop-loss |

## API Endpoints

| Endpoint | Auth | Description |
|----------|------|-------------|
| `GET /health` | None | Health check |
| `GET /api/status` | None | Bot status, uptime, cycles |
| `GET /api/risk` | None | Risk summary, P&L, exposure |
| `GET /api/rewards` | None | Daily/weekly/monthly rewards |
| `GET /api/markets` | None | Active markets being quoted |
| `GET /api/positions` | None | Current positions |
| `POST /api/bot/stop` | **Bearer token** | Stop bot (cancels all orders) |
| `POST /api/bot/resume` | **Bearer token** | Resume after halt |

## Security

### Control Endpoint Authentication (B5)
`POST /api/bot/stop` and `POST /api/bot/resume` require a Bearer token:

```bash
curl -X POST http://localhost:8000/api/bot/stop \
  -H "Authorization: Bearer your-secret-token"
```

Set `BOT_CONTROL_TOKEN` in your environment. If not set, a random token is generated at startup (printed to logs).

CORS is restricted to `CORS_ALLOWED_ORIGINS` (default: `http://localhost:3000`). Never use `*` in production.

## Risk Engine

### Drawdown Calculation (B3 fix)
Drawdown is measured on **portfolio value** (cash + mark-to-market positions), not raw cash:

```
drawdown = (peak_portfolio_value - current_portfolio_value) / peak_portfolio_value
```

This correctly handles the case where cash decreases because you bought shares that have since appreciated.

### Fee Accounting (B4 fix)
- `avg_cost` tracks **execution price only** (no fees embedded)
- Fees are tracked separately in `total_fees_paid` per position
- `realized_pnl = (exit_price - avg_cost) × shares`
- `net_pnl_after_fees = total_realized_pnl - total_fees_paid`

This prevents the double-counting bug where fees were added to avg_cost on BUY and subtracted again on SELL.

## Airdrop Positioning

The upcoming $POLY token airdrop is expected to reward:
- ✅ Trading volume (market making generates continuous volume)
- ✅ Liquidity provision (core function of this bot)
- ✅ Market diversity (configured across 6 categories)
- ✅ Active months (24/7 Railway deployment)

## Disclaimer

This bot trades real money. Start with small capital ($50–100) to validate performance before scaling. Market making carries inventory risk — prices can move against your positions.
