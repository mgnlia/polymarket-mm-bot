"""
Builder Program integration for Polymarket MM Bot.

Wraps py-builder-signing-sdk to:
  - Generate signed builder authentication headers for every CLOB request
  - Initialise the CLOB client with a BuilderConfig so all orders are attributed
  - Provide a BuilderTracker that polls the CLOB for builder-attributed volume/rewards
  - Expose a gasless Relayer client helper for onchain ops (wallet deploy, approvals, CTF)

Reference:
  https://docs.polymarket.com/builders/overview
  https://github.com/Polymarket/py-builder-signing-sdk
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class BuilderVolumeSummary:
    """Snapshot of builder-attributed volume and reward estimates."""
    total_volume_usdc: float = 0.0
    total_trades: int = 0
    week_volume_usdc: float = 0.0
    week_trades: int = 0
    estimated_weekly_reward_usdc: float = 0.0
    tier: str = "Unverified"          # Unverified | Verified | Partner
    leaderboard_rank: Optional[int] = None
    last_updated: Optional[datetime] = None
    trades_by_market: Dict[str, float] = field(default_factory=dict)


@dataclass
class BuilderTrade:
    """Single trade attributed to our builder account."""
    id: str
    market: str
    asset_id: str
    side: str
    size: float
    size_usdc: float
    price: float
    status: str
    transaction_hash: Optional[str]
    match_time: str
    fee_usdc: float = 0.0


# ---------------------------------------------------------------------------
# Builder client wrapper
# ---------------------------------------------------------------------------

class BuilderClient:
    """
    Wraps py-builder-signing-sdk and provides:
      - builder_config(): returns a BuilderConfig for the CLOB client
      - relayer_client(): returns a RelayerClient for gasless onchain ops
      - fetch_builder_trades(): polls CLOB for attributed trades
      - fetch_volume_summary(): aggregates volume + reward estimates
    """

    CLOB_HOST = "https://clob.polymarket.com"

    def __init__(
        self,
        builder_api_key: str,
        builder_secret: str,
        builder_passphrase: str,
        remote_signer_url: Optional[str] = None,
        remote_signer_token: Optional[str] = None,
    ) -> None:
        self._key = builder_api_key
        self._secret = builder_secret
        self._passphrase = builder_passphrase
        self._remote_url = remote_signer_url
        self._remote_token = remote_signer_token
        self._http = httpx.AsyncClient(timeout=30.0)
        self._sdk_config = None  # lazy-loaded BuilderConfig

    # ------------------------------------------------------------------
    # SDK helpers
    # ------------------------------------------------------------------

    def _get_sdk_config(self):
        """Lazy-load and return a py-builder-signing-sdk BuilderConfig."""
        if self._sdk_config is not None:
            return self._sdk_config

        try:
            from py_builder_signing_sdk import BuilderConfig, BuilderApiKeyCreds, RemoteBuilderConfig  # type: ignore

            if self._remote_url:
                # Remote signing mode (HSM / separate signing service)
                remote_cfg = RemoteBuilderConfig(
                    url=self._remote_url,
                    token=self._remote_token,
                )
                self._sdk_config = BuilderConfig(remote_builder_config=remote_cfg)
                logger.info("[builder] Using remote signing at %s", self._remote_url)
            else:
                # Local signing mode (API key + secret + passphrase)
                creds = BuilderApiKeyCreds(
                    key=self._key,
                    secret=self._secret,
                    passphrase=self._passphrase,
                )
                self._sdk_config = BuilderConfig(local_builder_creds=creds)
                logger.info("[builder] Using local signing with key %s…", self._key[:8])

            return self._sdk_config

        except ImportError:
            logger.warning(
                "[builder] py-builder-signing-sdk not installed — "
                "builder attribution disabled. Install with: pip install py-builder-signing-sdk"
            )
            return None

    def builder_config(self):
        """Return the BuilderConfig to pass into the CLOB client constructor."""
        return self._get_sdk_config()

    def generate_headers(self, method: str, path: str, body: str = "") -> Dict[str, str]:
        """
        Generate signed builder authentication headers for a single request.

        These are the headers that attribute an order to our builder profile:
          POLY_ADDRESS, POLY_SIGNATURE, POLY_TIMESTAMP, POLY_NONCE (builder variant)
        """
        cfg = self._get_sdk_config()
        if cfg is None:
            return {}
        try:
            return cfg.generate_builder_headers(method, path, body)
        except Exception as exc:
            logger.error("[builder] Header generation failed: %s", exc)
            return {}

    def init_clob_client(
        self,
        private_key: str,
        chain_id: int,
        signature_type: int,
        funder_address: str,
        api_creds=None,
    ):
        """
        Initialise a py-clob-client ClobClient with builder attribution.

        The builder config is injected as the 9th positional argument so that
        every order posted through this client carries the builder headers.
        """
        try:
            from py_clob_client.client import ClobClient  # type: ignore

            builder_cfg = self.builder_config()
            client = ClobClient(
                host=self.CLOB_HOST,
                key=private_key,
                chain_id=chain_id,
                signature_type=signature_type,
                funder=funder_address,
                builder_config=builder_cfg,   # builder attribution header injection
            )

            # Set or derive L2 API credentials
            if api_creds:
                client.set_api_creds(api_creds)
            else:
                creds = client.create_or_derive_api_creds()
                client.set_api_creds(creds)

            logger.info(
                "[builder] CLOB client initialised with builder attribution (key=%s…)",
                self._key[:8] if self._key else "none",
            )
            return client

        except ImportError:
            logger.warning("[builder] py-clob-client not installed — returning None")
            return None
        except Exception as exc:
            logger.error("[builder] CLOB client init failed: %s", exc)
            return None

    def init_relayer_client(self, private_key: str, chain_id: int = 137):
        """
        Initialise the Polymarket Relayer client for gasless onchain ops.

        Covers: Safe wallet deployment, USDC.e + outcome token approvals,
        CTF split/merge/redeem, and transaction monitoring.
        """
        try:
            from py_relayer_client.client import RelayerClient  # type: ignore

            builder_cfg = self.builder_config()
            relayer = RelayerClient(
                key=private_key,
                chain_id=chain_id,
                builder_config=builder_cfg,
            )
            logger.info("[builder] Relayer client initialised (gasless mode)")
            return relayer

        except ImportError:
            logger.info(
                "[builder] py-relayer-client not installed — "
                "gasless transactions unavailable. Install: pip install py-relayer-client"
            )
            return None
        except Exception as exc:
            logger.error("[builder] Relayer client init failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Volume & trade tracking via CLOB builder endpoints
    # ------------------------------------------------------------------

    async def fetch_builder_trades(
        self,
        clob_client=None,
        after: Optional[str] = None,
        market: Optional[str] = None,
        limit: int = 100,
    ) -> List[BuilderTrade]:
        """
        Fetch trades attributed to our builder account.

        Uses the CLOB client's getBuilderTrades() if available; falls back to
        a direct authenticated HTTP call.
        """
        if clob_client is not None:
            try:
                params: Dict[str, Any] = {}
                if after:
                    params["after"] = after
                if market:
                    params["market"] = market

                resp = await asyncio.to_thread(
                    clob_client.get_builder_trades, **params
                )
                raw_trades = resp.get("trades", []) if isinstance(resp, dict) else []
                return [self._parse_trade(t) for t in raw_trades]

            except AttributeError:
                # Older py-clob-client without get_builder_trades
                logger.debug("[builder] get_builder_trades not on client — using HTTP fallback")
            except Exception as exc:
                logger.warning("[builder] get_builder_trades failed: %s", exc)

        # HTTP fallback — direct authenticated request
        return await self._fetch_builder_trades_http(after=after, market=market, limit=limit)

    async def _fetch_builder_trades_http(
        self,
        after: Optional[str] = None,
        market: Optional[str] = None,
        limit: int = 100,
    ) -> List[BuilderTrade]:
        """Direct HTTP call to /builder/trades with signed headers."""
        path = "/builder/trades"
        params: Dict[str, Any] = {"limit": limit}
        if after:
            params["after"] = after
        if market:
            params["market"] = market

        headers = self.generate_headers("GET", path)
        if not headers:
            logger.warning("[builder] No builder headers — skipping trade fetch")
            return []

        try:
            resp = await self._http.get(
                f"{self.CLOB_HOST}{path}",
                params=params,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
            return [self._parse_trade(t) for t in data.get("trades", [])]
        except httpx.HTTPStatusError as exc:
            logger.warning("[builder] Trade fetch HTTP error %s: %s", exc.response.status_code, exc)
            return []
        except Exception as exc:
            logger.warning("[builder] Trade fetch failed: %s", exc)
            return []

    def _parse_trade(self, raw: Dict[str, Any]) -> BuilderTrade:
        return BuilderTrade(
            id=raw.get("id", ""),
            market=raw.get("market", ""),
            asset_id=raw.get("assetId", ""),
            side=raw.get("side", ""),
            size=float(raw.get("size", 0)),
            size_usdc=float(raw.get("sizeUsdc", 0)),
            price=float(raw.get("price", 0)),
            status=raw.get("status", ""),
            transaction_hash=raw.get("transactionHash"),
            match_time=raw.get("matchTime", ""),
            fee_usdc=float(raw.get("feeUsdc", 0)),
        )

    async def fetch_volume_summary(
        self, clob_client=None
    ) -> BuilderVolumeSummary:
        """
        Aggregate builder-attributed volume and estimate weekly rewards.

        Fetches all attributed trades (up to 1000 most recent), computes:
          - total volume / trades
          - rolling 7-day volume / trades
          - estimated weekly reward (heuristic: 0.05% of weekly volume)
        """
        summary = BuilderVolumeSummary(last_updated=datetime.now(timezone.utc))

        try:
            trades = await self.fetch_builder_trades(clob_client=clob_client, limit=500)
        except Exception as exc:
            logger.warning("[builder] Volume summary fetch failed: %s", exc)
            return summary

        if not trades:
            return summary

        now = datetime.now(timezone.utc)
        week_cutoff_iso = now.replace(
            hour=0, minute=0, second=0, microsecond=0
        )

        total_vol = 0.0
        week_vol = 0.0
        total_trades = 0
        week_trades = 0
        by_market: Dict[str, float] = {}

        for t in trades:
            vol = t.size_usdc
            total_vol += vol
            total_trades += 1

            # Approximate 7-day window from match_time string
            try:
                match_dt = datetime.fromisoformat(t.match_time.replace("Z", "+00:00"))
                days_ago = (now - match_dt).days
                if days_ago < 7:
                    week_vol += vol
                    week_trades += 1
            except (ValueError, AttributeError):
                pass

            if t.market:
                by_market[t.market] = by_market.get(t.market, 0.0) + vol

        # Heuristic reward estimate: ~0.05% of weekly builder volume
        # Actual rewards depend on tier, leaderboard rank, and pool size
        estimated_reward = week_vol * 0.0005

        summary.total_volume_usdc = round(total_vol, 2)
        summary.total_trades = total_trades
        summary.week_volume_usdc = round(week_vol, 2)
        summary.week_trades = week_trades
        summary.estimated_weekly_reward_usdc = round(estimated_reward, 4)
        summary.trades_by_market = {k: round(v, 2) for k, v in by_market.items()}

        logger.info(
            "[builder] Volume summary — total=%.2f USDC (%d trades), "
            "7d=%.2f USDC (%d trades), est_reward=%.4f USDC/week",
            total_vol, total_trades, week_vol, week_trades, estimated_reward,
        )
        return summary

    async def close(self) -> None:
        await self._http.aclose()


# ---------------------------------------------------------------------------
# Convenience factory
# ---------------------------------------------------------------------------

def create_builder_client(
    api_key: str,
    secret: str,
    passphrase: str,
    remote_signer_url: Optional[str] = None,
    remote_signer_token: Optional[str] = None,
) -> Optional[BuilderClient]:
    """
    Factory that returns a BuilderClient if credentials are provided,
    or None if all three are empty (graceful degradation).
    """
    if not (api_key and secret and passphrase):
        logger.info(
            "[builder] No builder credentials configured — "
            "builder attribution disabled. "
            "Set POLY_BUILDER_API_KEY / POLY_BUILDER_SECRET / POLY_BUILDER_PASSPHRASE "
            "to enable weekly USDC rewards."
        )
        return None

    return BuilderClient(
        builder_api_key=api_key,
        builder_secret=secret,
        builder_passphrase=passphrase,
        remote_signer_url=remote_signer_url,
        remote_signer_token=remote_signer_token,
    )
