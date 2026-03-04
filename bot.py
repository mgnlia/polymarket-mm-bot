"""Main bot loop — orchestrates scanner, quoter, risk, hedger, rewards."""
from __future__ import annotations

import asyncio
import signal
from datetime import datetime, timezone
from typing import Dict, List

from config import Config
from hedger import Hedger, HedgeAction
from quoter import MarketQuotes, Quoter
from rewards import RewardTracker
from risk import RiskManager
from scanner import MarketScanner, ScoredMarket


class MarketMakerBot:
    """
    Polymarket Market Maker Bot

    Main loop:
    1. Scan for rewarded markets (every scanner_interval_s)
    2. For each market, build two-sided quotes
    3. Cancel stale orders, place fresh quotes
    4. Check for hedge needs
    5. Track rewards
    6. Enforce risk limits (circuit breakers)
    """

    def __init__(self, config: Config, clob_client=None) -> None:
        self.config = config
        self.scanner = MarketScanner(config)
        self.quoter = Quoter(config, clob_client)
        self.risk = RiskManager(config, starting_capital=config.max_total_exposure_usdc)
        self.hedger = Hedger(config, self.risk)
        self.rewards = RewardTracker(config.funder_address)

        self._active_markets: List[ScoredMarket] = []
        self._active_quotes: Dict[str, MarketQuotes] = {}
        self._running = False
        self._quote_cycle = 0
        self._start_time = datetime.now(timezone.utc)

        # Stats for dashboard API
        self.stats = {
            "status": "stopped",
            "uptime_s": 0,
            "quote_cycles": 0,
            "markets_quoted": 0,
            "errors": [],
        }

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the bot. Blocks until stopped."""
        print("[bot] Starting Polymarket Market Maker Bot")
        print(f"[bot] Max markets: {self.config.max_markets}")
        print(f"[bot] Target spread: {self.config.target_spread_pct:.1%}")
        print(f"[bot] Max exposure: ${self.config.max_total_exposure_usdc:.0f} USDC")
        print(f"[bot] Dry-run mode: {self.quoter._client is None}")

        self._running = True
        self.stats["status"] = "running"

        # Register signal handlers for graceful shutdown
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self.stop()))

        try:
            await asyncio.gather(
                self._scanner_loop(),
                self._quote_loop(),
                self._rewards_loop(),
                self._stats_loop(),
            )
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Graceful shutdown — cancel all orders first."""
        if not self._running:
            return
        print("[bot] Shutting down — cancelling all orders...")
        self._running = False
        self.stats["status"] = "stopped"
        await self.quoter.cancel_all_orders()
        await self.scanner.close()
        await self.quoter.close()
        await self.rewards.close()
        print("[bot] Shutdown complete")

    # ── Main loops ───────────────────────────────────────────────────────────

    async def _scanner_loop(self) -> None:
        """Periodically refresh market list."""
        while self._running:
            try:
                markets = await self.scanner.fetch_rewarded_markets()
                if markets:
                    self._active_markets = markets
                    self.stats["markets_quoted"] = len(markets)
                    print(f"[bot] Tracking {len(markets)} markets")
            except Exception as e:
                self._log_error(f"Scanner error: {e}")
            await asyncio.sleep(self.config.scanner_interval_s)

    async def _quote_loop(self) -> None:
        """Main quoting loop — refresh quotes every quote_refresh_interval_s."""
        # Wait for first scan
        while self._running and not self._active_markets:
            await asyncio.sleep(2)

        while self._running:
            if self.risk.state.trading_halted:
                print(f"[bot] Trading halted: {self.risk.state.halt_reason}. Waiting...")
                await asyncio.sleep(30)
                continue

            try:
                # Refresh prices
                markets = await self.scanner.refresh_prices(self._active_markets)

                for market in markets:
                    try:
                        await self._requote_market(market)
                    except Exception as e:
                        self._log_error(f"Quote error on {market.condition_id[:16]}: {e}")

                # Check hedges after quoting
                hedge_actions = self.hedger.compute_hedges(markets)
                for action in hedge_actions:
                    await self._execute_hedge(action)

                self._quote_cycle += 1
                self.stats["quote_cycles"] = self._quote_cycle

            except Exception as e:
                self._log_error(f"Quote loop error: {e}")

            await asyncio.sleep(self.config.quote_refresh_interval_s)

    async def _requote_market(self, market: ScoredMarket) -> None:
        """Cancel stale quotes and place fresh ones for a market."""
        condition_id = market.condition_id

        # Cancel existing orders
        await self.quoter.cancel_market_orders(condition_id)

        # Check if we can trade
        size = self.config.order_size_usdc
        allowed, reason = self.risk.can_trade(condition_id, size * 2)  # 2 sides
        if not allowed:
            print(f"[bot] Skipping {market.question[:40]} — {reason}")
            return

        # Get current net position for delta-neutral skewing
        net_pos = self.risk.get_net_yes_exposure(condition_id)

        # Build and place quotes
        quotes = self.quoter.build_quotes(market, net_position=net_pos)
        placed = await self.quoter.place_quotes(quotes)
        self._active_quotes[condition_id] = placed

    async def _execute_hedge(self, action: HedgeAction) -> None:
        """Execute a hedge order."""
        allowed, reason = self.risk.can_trade(action.condition_id, action.size_usdc)
        if not allowed:
            print(f"[hedger] Skipping hedge — {reason}")
            return

        if self.quoter._client is None:
            print(f"[hedger:dry-run] Would hedge {action.side} {action.token} "
                  f"{action.size_usdc:.2f} USDC — {action.reason}")
        else:
            # Place market order for hedge
            print(f"[hedger] Executing hedge: {action}")

    async def _rewards_loop(self) -> None:
        """Periodically fetch and log reward payouts."""
        while self._running:
            try:
                await self.rewards.fetch_rewards()
                self.rewards.log_summary()
            except Exception as e:
                self._log_error(f"Rewards error: {e}")
            await asyncio.sleep(self.config.rewards_check_interval_s)

    async def _stats_loop(self) -> None:
        """Update uptime stats every 10s."""
        while self._running:
            self.stats["uptime_s"] = int(
                (datetime.now(timezone.utc) - self._start_time).total_seconds()
            )
            self.stats["risk"] = self.risk.summary()
            self.stats["rewards"] = {
                "today": self.rewards.summary.today_earned_usdc,
                "week": self.rewards.summary.week_earned_usdc,
                "total": self.rewards.summary.total_earned_usdc,
            }
            await asyncio.sleep(10)

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _log_error(self, msg: str) -> None:
        print(f"[bot] ERROR: {msg}")
        self.stats["errors"] = ([msg] + self.stats["errors"])[:20]  # Keep last 20


async def main() -> None:
    """Entry point."""
    config = Config.from_env()

    # Validate config (will raise if PRIVATE_KEY missing)
    try:
        config.validate()
        print("[bot] Config validated")
        clob_client = _init_clob_client(config)
    except ValueError as e:
        print(f"[bot] Config warning: {e}")
        print("[bot] Running in DRY-RUN mode (no live orders)")
        clob_client = None

    bot = MarketMakerBot(config, clob_client)
    await bot.start()


def _init_clob_client(config: Config):
    """Initialize py-clob-client. Returns None if package not available."""
    try:
        from py_clob_client.client import ClobClient  # type: ignore
        client = ClobClient(
            host=config.clob_host,
            key=config.private_key,
            chain_id=config.chain_id,
            signature_type=config.signature_type,
            funder=config.funder_address,
        )
        # Derive API credentials
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        print("[bot] CLOB client initialized")
        return client
    except ImportError:
        print("[bot] py-clob-client not installed — dry-run mode")
        return None
    except Exception as e:
        print(f"[bot] CLOB client init failed: {e} — dry-run mode")
        return None


if __name__ == "__main__":
    asyncio.run(main())
