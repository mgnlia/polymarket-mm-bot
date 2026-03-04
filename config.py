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

    # ── Builder Program credentials (NEW) ────────────────────────────────────
    # Get these from polymarket.com/settings?tab=builder
    builder_api_key: str = field(
        default_factory=lambda: os.environ.get("POLY_BUILDER_API_KEY", "")
    )
    builder_secret: str = field(
        default_factory=lambda: os.environ.get("POLY_BUILDER_SECRET", "")
    )
    builder_passphrase: str = field(
        default_factory=lambda: os.environ.get("POLY_BUILDER_PASSPHRASE", "")
    )
    relayer_host: str = field(
        default_factory=lambda: os.environ.get(
            "POLY_RELAYER_HOST", "https://relayer.polymarket.com"
        )
    )

    # ── Market selection ─────────────────────────────────────────────────────
    categories: List[str] = field(default_factory=lambda: [
        "politics", "sports", "crypto", "economics", "entertainment", "science"
    ])
    max_markets: int = 10
    min_incentive_size: float = 50.0
    min_volume_24h: float = 5000.0

    # ── Quoting parameters ───────────────────────────────────────────────────
    max_spread_pct: float = 0.05
    target_spread_pct: float = 0.02
    order_size_usdc: float = 50.0
    min_order_size_usdc: float = 10.0

    # ── Risk parameters ──────────────────────────────────────────────────────
    max_position_per_market_usdc: float = 50.0
    max_total_exposure_usdc: float = 500.0
    max_drawdown_pct: float = 0.20
    daily_loss_limit_usdc: float = 100.0

    # ── Extreme market handling ──────────────────────────────────────────────
    extreme_low_threshold: float = 0.10
    extreme_high_threshold: float = 0.90
    extreme_market_size_multiplier: float = 0.5

    # ── Scheduling ───────────────────────────────────────────────────────────
    quote_refresh_interval_s: int = 30
    scanner_interval_s: int = 300
    rewards_check_interval_s: int = 3600
    builder_rewards_check_interval_s: int = 3600  # NEW
    order_stale_after_s: int = 120

    # ── Logging / DB ─────────────────────────────────────────────────────────
    log_level: str = field(default_factory=lambda: os.environ.get("LOG_LEVEL", "INFO"))
    db_path: str = "mm_bot.db"

    # ── API server ────────────────────────────────────────────────────────────
    api_host: str = "0.0.0.0"
    api_port: int = field(default_factory=lambda: int(os.environ.get("PORT", "8000")))

    @property
    def builder_configured(self) -> bool:
        """True if all three builder credentials are set."""
        return bool(self.builder_api_key and self.builder_secret and self.builder_passphrase)

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
            max_position_per_market_usdc=float(
                os.environ.get("MAX_POSITION_PER_MARKET_USDC", "50.0")
            ),
            max_total_exposure_usdc=float(
                os.environ.get("MAX_TOTAL_EXPOSURE_USDC", "500.0")
            ),
            max_drawdown_pct=float(os.environ.get("MAX_DRAWDOWN_PCT", "0.20")),
            daily_loss_limit_usdc=float(os.environ.get("DAILY_LOSS_LIMIT_USDC", "100.0")),
            # Builder Program
            builder_api_key=os.environ.get("POLY_BUILDER_API_KEY", ""),
            builder_secret=os.environ.get("POLY_BUILDER_SECRET", ""),
            builder_passphrase=os.environ.get("POLY_BUILDER_PASSPHRASE", ""),
            relayer_host=os.environ.get(
                "POLY_RELAYER_HOST", "https://relayer.polymarket.com"
            ),
        )

    def validate(self) -> None:
        """Raise if required fields are missing."""
        if not self.private_key:
            raise ValueError("PRIVATE_KEY is required")
        if not self.funder_address:
            raise ValueError("FUNDER_ADDRESS is required")
        if self.target_spread_pct >= self.max_spread_pct:
            raise ValueError("target_spread_pct must be < max_spread_pct")

    def print_startup_summary(self) -> None:
        print(f"[config] CLOB host:        {self.clob_host}")
        print(f"[config] Chain ID:          {self.chain_id}")
        print(f"[config] Max markets:       {self.max_markets}")
        print(f"[config] Target spread:     {self.target_spread_pct:.1%}")
        print(f"[config] Max exposure:      ${self.max_total_exposure_usdc:.0f} USDC")
        print(f"[config] Builder Program:   {'ENABLED ✓' if self.builder_configured else 'disabled (set POLY_BUILDER_API_KEY)'}")
        if self.builder_configured:
            print(f"[config] Builder key:       {self.builder_api_key[:8]}...")
            print(f"[config] Relayer host:      {self.relayer_host}")
