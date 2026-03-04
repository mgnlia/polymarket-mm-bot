"""Central configuration for the Polymarket Market Maker Bot."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # ── Polymarket CLOB credentials ──────────────────────────────────────────
    clob_host: str = "https://clob.polymarket.com"
    private_key: str = field(default_factory=lambda: os.environ.get("PRIVATE_KEY", ""))
    funder_address: str = field(default_factory=lambda: os.environ.get("FUNDER_ADDRESS", ""))
    chain_id: int = 137  # Polygon mainnet
    signature_type: int = 0  # 0=EOA, 1=Magic/email, 2=proxy

    # ── Market selection ─────────────────────────────────────────────────────
    categories: List[str] = field(default_factory=lambda: [
        "politics", "sports", "crypto", "economics", "entertainment", "science"
    ])
    max_markets: int = 10          # Max concurrent markets to quote
    min_incentive_size: float = 50.0   # Min reward pool size (USDC) to bother
    min_volume_24h: float = 5000.0     # Min 24h volume to quote

    # ── Quoting parameters ───────────────────────────────────────────────────
    # Reward formula: S(v,s) = ((v-s)/v)^2 * b
    # Tighter spread → exponentially more rewards
    max_spread_pct: float = 0.05   # 5¢ max spread (v in formula)
    target_spread_pct: float = 0.02  # 2¢ target (tight for high rewards)
    order_size_usdc: float = 50.0  # USDC per side per market
    min_order_size_usdc: float = 10.0  # Minimum order size

    # ── Risk parameters ──────────────────────────────────────────────────────
    max_position_per_market_usdc: float = 50.0   # Max net exposure per market
    max_total_exposure_usdc: float = 500.0        # Total portfolio exposure cap
    max_drawdown_pct: float = 0.20                # Halt at 20% drawdown
    daily_loss_limit_usdc: float = 100.0          # Stop for day if hit

    # ── Extreme market handling ──────────────────────────────────────────────
    # Markets <10¢ or >90¢ — MUST post both sides for full reward score
    extreme_low_threshold: float = 0.10
    extreme_high_threshold: float = 0.90
    extreme_market_size_multiplier: float = 0.5   # Smaller size in extremes

    # ── Scheduling ───────────────────────────────────────────────────────────
    quote_refresh_interval_s: int = 30    # Refresh quotes every 30s
    scanner_interval_s: int = 300         # Re-scan markets every 5 min
    rewards_check_interval_s: int = 3600  # Check rewards every hour
    order_stale_after_s: int = 120        # Cancel orders older than 2 min

    # ── Logging / DB ─────────────────────────────────────────────────────────
    log_level: str = field(default_factory=lambda: os.environ.get("LOG_LEVEL", "INFO"))
    db_path: str = "mm_bot.db"

    # ── API server (for dashboard) ────────────────────────────────────────────
    api_host: str = "0.0.0.0"
    api_port: int = int(os.environ.get("PORT", "8000"))

    @classmethod
    def from_env(cls) -> "Config":
        """Load config, overriding defaults with env vars where provided."""
        return cls(
            private_key=os.environ.get("PRIVATE_KEY", ""),
            funder_address=os.environ.get("FUNDER_ADDRESS", ""),
            chain_id=int(os.environ.get("CHAIN_ID", "137")),
            signature_type=int(os.environ.get("SIGNATURE_TYPE", "0")),
            max_markets=int(os.environ.get("MAX_MARKETS", "10")),
            max_spread_pct=float(os.environ.get("MAX_SPREAD_PCT", "0.05")),
            target_spread_pct=float(os.environ.get("TARGET_SPREAD_PCT", "0.02")),
            order_size_usdc=float(os.environ.get("ORDER_SIZE_USDC", "50.0")),
            max_position_per_market_usdc=float(os.environ.get("MAX_POSITION_PER_MARKET_USDC", "50.0")),
            max_total_exposure_usdc=float(os.environ.get("MAX_TOTAL_EXPOSURE_USDC", "500.0")),
            max_drawdown_pct=float(os.environ.get("MAX_DRAWDOWN_PCT", "0.20")),
            daily_loss_limit_usdc=float(os.environ.get("DAILY_LOSS_LIMIT_USDC", "100.0")),
        )

    def validate(self) -> None:
        """Raise if required fields are missing."""
        if not self.private_key:
            raise ValueError("PRIVATE_KEY is required")
        if not self.funder_address:
            raise ValueError("FUNDER_ADDRESS is required")
        if self.target_spread_pct >= self.max_spread_pct:
            raise ValueError("target_spread_pct must be < max_spread_pct")
