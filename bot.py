"""Main bot loop — orchestrates scanner, quoter, risk, hedger, rewards.

Upgraded with Builder Program integration:
- Builder-attributed CLOB client (gasless via relayer)
- BuilderRewardsTracker for volume/reward monitoring
- Builder rewards loop (hourly leaderboard refresh)
"""
from __future__ import annotations

import asyncio
import signal
from datetime import datetime, timezone
from typing import Dict, List, Optional

from builder_auth import BuilderCreds, init_clob_client_with_builder, init_relayer_client
from builder_rewards import BuilderRewardsTracker
from config import Config
from hedger import Hedger, HedgeAction
from quoter import MarketQuotes, Quoter
from rewards import RewardTracker
from risk import RiskManager
from scanner import MarketScanner, ScoredMarket


class MarketMakerBot:
    """
    Polymarket Market Maker Bot — Triple Revenue Stream:
      1. Spread capture (bid/ask)
      2. Liquidity rewards (Polymarket MM program)
      3. Builder Program rewards (weekly USDC via volume attribution)
    """

    def __init__(
        self,
        config: Config,
        clob_client=None,
        builder_creds: Optional[BuilderCreds] = None,
        relayer_client=None,
    ) -> None:
        self.config = config

        # Builder setup
        self._builder_creds = builder_creds or BuilderCreds.from_env()
        self._relayer = relayer_client
        self._builder_rewards = BuilderRewardsTracker(
            builder_key=config.builder_api_key
        )

        # Core components
        self.scanner = MarketScanner(config)
        self.quoter = Quoter(
            config,
            clob_client=clob_client,
            builder_creds=self._builder_creds,
            builder_rewards=self._builder_rewards,
            relayer_client=relayer_client,
        )
        self.risk = RiskManager(config, starting_capital=config.max_total_exposure_usdc)
        self.hedger = Hedger(config, self.risk)
        self.rewards = RewardTracker(config.funder_address)

        self._active_markets: List[ScoredMarket] = []
        self._active_quotes: Dict[str, MarketQuotes] = {}
        self._running = False
        self._quote_cycle = 0
        self._start_time = datetime.now(timezone.utc)

        self.stats: Dict = {
            "status": "stopped",
            "uptime_s": 0,
            "quote_cycles": 0,
            "markets_quoted": 0,
            "errors": [],
            "builder_enabled": self._builder_creds.configured,
        }

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        print("[bot] Starting Polymarket Market Maker Bot (Triple Revenue)")
        self.config.print_startup_summary()
        self._running = True
        self.stats["status"] = "running"

        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self.stop()))

        try:
            await asyncio.gather(
                self._scanner_loop(),
                self._quote_loop(),
                self._rewards_loop(),
                self._builder_rewards_loop(),
                self._stats_loop(),
            )
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    async def stop(self) -> None:
        if not self._running:
            return
        print("[bot] Shutting down — cancelling all orders...")
        self._running = False
        self.stats["status"] = "stopped"
        await self.quoter.cancel_all_orders()
        await self.scanner.close()
        await self.quoter.close()
        await self.rewards.close()
        await self._builder_rewards.close()
        print("[bot] Shutdown complete")

    # ── Loops ─────────────────────────────────────────────────────────────────

    async def _scanner_loop(self) -> None:
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
        while self._running and not self._active_markets:
            await asyncio.sleep(2)

        while self._running:
            if self.risk.state.trading_halted:
                print(f"[bot] Trading halted: {self.risk.state.halt_reason}. Waiting...")
                await asyncio.sleep(30)
                continue

            try:
                markets = await self.scanner.refresh_prices(self._active_markets)
                for market in markets:
                    try:
                        await self._requote_market(market)
                    except Exception as e:
                        self._log_error(f"Quote error on {market.condition_id[:16]}: {e}")

                hedge_actions = self.hedger.compute_hedges(markets)
                for action in hedge_actions:
                    await self._execute_hedge(action)

                self._quote_cycle += 1
                self.stats["quote_cycles"] = self._quote_cycle
            except Exception as e:
                self._log_error(f"Quote loop error: {e}")

            await asyncio.sleep(self.config.quote_refresh_interval_s)

    async def _requote_market(self, market: ScoredMarket) -> None:
        condition_id = market.condition_id
        await self.quoter.cancel_market_orders(condition_id)

        size = self.config.order_size_usdc
        allowed, reason = self.risk.can_trade(condition_id, size * 2)
        if not allowed:
            print(f"[bot] Skipping {market.question[:40]} — {reason}")
            return

        net_pos = self.risk.get_net_yes_exposure(condition_id)
        quotes = self.quoter.build_quotes(market, net_position=net_pos)
        placed = await self.quoter.place_quotes(quotes)
        self._active_quotes[condition_id] = placed

    async def _execute_hedge(self, action: HedgeAction) -> None:
        allowed, reason = self.risk.can_trade(action.condition_id, action.size_usdc)
        if not allowed:
            print(f"[hedger] Skipping hedge — {reason}")
            return
        if self.quoter._client is None:
            print(
                f"[hedger:dry-run] Would hedge {action.side} {action.token} "
                f"{action.size_usdc:.2f} USDC — {action.reason}"
            )
        else:
            print(f"[hedger] Executing hedge: {action}")

    async def _rewards_loop(self) -> None:
        """Liquidity rewards (existing MM program)."""
        while self._running:
            try:
                await self.rewards.fetch_rewards()
                self.rewards.log_summary()
            except Exception as e:
                self._log_error(f"Rewards error: {e}")
            await asyncio.sleep(self.config.rewards_check_interval_s)

    async def _builder_rewards_loop(self) -> None:
        """Builder Program rewards — fetch leaderboard stats hourly."""
        if not self._builder_creds.configured:
            print("[bot] Builder rewards loop skipped (no builder creds)")
            return

        while self._running:
            try:
                await self._builder_rewards.fetch_builder_stats()
                self._builder_rewards.log_summary()
            except Exception as e:
                self._log_error(f"Builder rewards error: {e}")
            await asyncio.sleep(self.config.builder_rewards_check_interval_s)

    async def _stats_loop(self) -> None:
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
            self.stats["builder_rewards"] = {
                "week_volume": self._builder_rewards.stats.current_week_volume_usdc,
                "total_rewards": self._builder_rewards.stats.total_rewards_usdc,
                "rank": self._builder_rewards.stats.leaderboard_rank,
                "tier": self._builder_rewards.stats.tier,
            }
            await asyncio.sleep(10)

    def _log_error(self, msg: str) -> None:
        print(f"[bot] ERROR: {msg}")
        self.stats["errors"] = ([msg] + self.stats["errors"])[:20]


async def main() -> None:
    config = Config.from_env()

    try:
        config.validate()
        print("[bot] Config validated ✓")
        builder_creds = BuilderCreds.from_env()
        clob_client, is_builder = init_clob_client_with_builder(config, builder_creds)
        relayer_client = init_relayer_client(config, builder_creds) if is_builder else None
    except ValueError as e:
        print(f"[bot] Config warning: {e}")
        print("[bot] Running in DRY-RUN mode (no live orders)")
        clob_client = None
        builder_creds = BuilderCreds.from_env()
        relayer_client = None

    bot = MarketMakerBot(
        config,
        clob_client=clob_client,
        builder_creds=builder_creds,
        relayer_client=relayer_client,
    )
    await bot.start()


if __name__ == "__main__":
    asyncio.run(main())
