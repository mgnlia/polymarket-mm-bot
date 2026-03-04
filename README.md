# Polymarket Market Maker Bot

Automated market-making bot for [Polymarket](https://polymarket.com) that earns **triple revenue streams**:
1. **Spread capture** — bid/ask spread on every filled order
2. **Liquidity rewards** — daily USDC from Polymarket's MM rewards program
3. **Builder Program rewards** — weekly USDC from the [Polymarket Builder Program](https://docs.polymarket.com/builders/overview)

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
- Builder Program adds additional weekly USDC rewards on top

## Architecture
```
bot.py            ← Main orchestration loop (Triple Revenue)
scanner.py        ← Fetch & score rewarded markets (Gamma API)
quoter.py         ← Build & place two-sided limit orders (with builder attribution)
risk.py           ← Position tracking, exposure limits, circuit breakers
hedger.py         ← Delta-neutral rebalancing
rewards.py        ← Track daily USDC payouts (MM program)
builder_auth.py   ← Builder Program auth: X-Builder-Id / X-Builder-Signature headers
builder_rewards.py← Builder Program volume & weekly reward tracking
config.py         ← All parameters (env-configurable)
api.py            ← FastAPI server: dashboard + /api/builder endpoint
```

## Setup

### Prerequisites
- Python 3.9+
- Polygon wallet with USDC (start with $500 recommended)
- Polymarket account with API access
- (Optional) Builder Program registration at https://polymarket.com/settings?tab=builder

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

## Environment Variables

### Required
| Variable | Description |
|----------|-------------|
| `PRIVATE_KEY` | Polygon wallet private key |
| `FUNDER_ADDRESS` | Wallet address |
| `BOT_CONTROL_TOKEN` | Secret token for stop/resume API |

### Builder Program (NEW — enables weekly USDC rewards)
| Variable | Required | Description |
|----------|----------|-------------|
| `POLYMARKET_BUILDER_ID` | Recommended | Your builder wallet address/ID (sets `X-Builder-Id` header) |
| `POLYMARKET_BUILDER_SIGNATURE` | Recommended | Static signature from builder portal (sets `X-Builder-Signature` header) |
| `POLY_BUILDER_API_KEY` | Advanced | Builder API key (for full HMAC signing) |
| `POLY_BUILDER_SECRET` | Advanced | HMAC signing secret (base64-encoded) |
| `POLY_BUILDER_PASSPHRASE` | Advanced | API passphrase |
| `POLY_RELAYER_HOST` | No | Relayer URL for gasless txns (default: `https://relayer.polymarket.com`) |

### Optional
| Variable | Default | Description |
|----------|---------|-------------|
| `MAX_MARKETS` | 10 | Max markets to quote simultaneously |
| `ORDER_SIZE_USDC` | 50 | USDC per side per market |
| `MAX_TOTAL_EXPOSURE_USDC` | 500 | Total portfolio cap |
| `TARGET_SPREAD_PCT` | 0.02 | Quote tightness target |
| `MAX_SPREAD_PCT` | 0.05 | Max spread tolerance |
| `DAILY_LOSS_LIMIT_USDC` | 100 | Daily realized loss stop-loss |
| `CORS_ALLOWED_ORIGINS` | localhost | Comma-separated allowed origins |

## Builder Program Setup

The [Polymarket Builder Program](https://docs.polymarket.com/builders/overview) rewards developers who route volume through their builder profile with weekly USDC payouts.

### How to Register
1. Go to https://polymarket.com/settings?tab=builder
2. Register your wallet as a builder
3. Copy your **Builder ID** (wallet address) and **Builder Signature**
4. Add to your `.env`:
   ```bash
   POLYMARKET_BUILDER_ID=0xYourWalletAddress
   POLYMARKET_BUILDER_SIGNATURE=your_signature_from_portal
   ```

### How It Works
Every order placed by this bot includes two HTTP headers on CLOB API requests:
```
X-Builder-Id: <your_builder_id>
X-Builder-Signature: <your_signature>
```
These headers attribute the order volume to your builder profile. Polymarket distributes weekly USDC rewards proportional to attributed volume.

### Builder Stats Endpoint
```bash
GET /api/builder
```
Returns:
```json
{
  "builder_key": "0xABC...",
  "tier": "Verified",
  "leaderboard_rank": 42,
  "total_volume_usdc": 125000.00,
  "total_orders_attributed": 3840,
  "total_rewards_usdc": 487.50,
  "current_week": {
    "volume_usdc": 18500.00,
    "orders": 560
  },
  "revenue_streams": {
    "spread_capture": "active",
    "liquidity_rewards": "active",
    "builder_rewards": "active"
  }
}
```

### Builder Orders Endpoint
```bash
GET /api/builder/orders?limit=50
```
Returns recent orders attributed to your builder profile.

### Relayer Client (Gasless Transactions)
When builder credentials are configured, the bot initializes a Relayer Client for gasless onchain operations (wallet deployment, USDC approvals, CTF operations). This eliminates the need to hold MATIC for gas.

Reference: https://docs.polymarket.com/builders/overview

## Run
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
| `GET /health` | None | Health check + builder enabled status |
| `GET /api/status` | None | Bot status, uptime, cycles |
| `GET /api/risk` | None | Risk summary, P&L, exposure |
| `GET /api/rewards` | None | Daily/weekly/monthly MM rewards |
| `GET /api/markets` | None | Active markets being quoted |
| `GET /api/positions` | None | Current positions |
| `GET /api/builder` | None | **Builder Program stats** — volume, rewards, rank, tier |
| `GET /api/builder/orders` | None | **Builder attributed order history** |
| `POST /api/bot/stop` | **Bearer token** | Stop bot (cancels all orders) |
| `POST /api/bot/resume` | **Bearer token** | Resume after halt |

## Security
### Control Endpoint Authentication
`POST /api/bot/stop` and `POST /api/bot/resume` require a Bearer token:
```bash
curl -X POST http://localhost:8000/api/bot/stop \
  -H "Authorization: Bearer your-secret-token"
```
Set `BOT_CONTROL_TOKEN` in your environment. If not set, a random token is generated at startup (printed to logs). CORS is restricted to `CORS_ALLOWED_ORIGINS` (default: `http://localhost:3000`). Never use `*` in production.

## Risk Engine
### Drawdown Calculation
Drawdown is measured on **portfolio value** (cash + mark-to-market positions), not raw cash:
```
drawdown = (peak_portfolio_value - current_portfolio_value) / peak_portfolio_value
```

### Fee Accounting
- `avg_cost` tracks **execution price only** (no fees embedded)
- Fees are tracked separately in `total_fees_paid` per position
- `realized_pnl = (exit_price - avg_cost) × shares`
- `net_pnl_after_fees = total_realized_pnl - total_fees_paid`

## Airdrop Positioning
The upcoming $POLY token airdrop is expected to reward:
- ✅ Trading volume (market making generates continuous volume)
- ✅ Liquidity provision (core function of this bot)
- ✅ Market diversity (configured across 6 categories)
- ✅ Active months (24/7 Railway deployment)
- ✅ Builder Program participation (weekly USDC + airdrop eligibility)

## Disclaimer
This bot trades real money. Start with small capital ($50–100) to validate performance before scaling. Market making carries inventory risk — prices can move against your positions.
