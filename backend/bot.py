"""
Bot orchestrator:
- Holds BotConfig & ClassifierRules state (persisted in Mongo)
- Receives new launches from the listener
- Decides to enter (paper or live)
- Tracks active positions & exits based on classifier + take-profit/stop-loss/timeout
- Enforces daily kill-switch
"""
import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta

from solders.pubkey import Pubkey

from models import BotConfig, ClassifierRules, Launch, Trade, now_utc, new_id
from classifier import classify
import pumpfun
from solana_client import get_sol_usd_price, LAMPORTS_PER_SOL
from wallet import get_keypair, get_pubkey

logger = logging.getLogger("bot")


class BotState:
    def __init__(self, db):
        self.db = db
        self.config = BotConfig()
        self.rules = ClassifierRules()
        self.active_trades: dict[str, dict] = {}  # mint -> trade dict + runtime
        self.recent_launches: list[dict] = []  # capped in memory
        self.kill_switch_tripped = False
        self.listener_connected = False
        self._daily_pnl_cache_date = None
        self._monitor_task: asyncio.Task | None = None

    async def load(self):
        cfg = await self.db.bot_config.find_one({"_id": "current"}, {"_id": 0})
        if cfg:
            self.config = BotConfig(**cfg)
        rules = await self.db.classifier_rules.find_one({"_id": "current"}, {"_id": 0})
        if rules:
            self.rules = ClassifierRules(**rules)
        # Reload active trades from DB
        async for t in self.db.trades.find({"status": "active"}, {"_id": 0}):
            self.active_trades[t["mint"]] = {"trade": t, "metrics": _new_metrics()}

    async def save_config(self):
        await self.db.bot_config.update_one(
            {"_id": "current"},
            {"$set": {**self.config.model_dump(), "_id": "current"}},
            upsert=True,
        )

    async def save_rules(self):
        await self.db.classifier_rules.update_one(
            {"_id": "current"},
            {"$set": {**self.rules.model_dump(), "_id": "current"}},
            upsert=True,
        )

    async def daily_pnl_usd(self) -> float:
        start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        cursor = self.db.trades.find(
            {"status": "closed", "exit_time": {"$gte": start.isoformat()}},
            {"_id": 0, "pnl_usd": 1},
        )
        total = 0.0
        async for d in cursor:
            total += float(d.get("pnl_usd", 0.0))
        return total

    async def check_kill_switch(self) -> bool:
        pnl = await self.daily_pnl_usd()
        if pnl <= -abs(self.config.daily_kill_switch_usd):
            self.kill_switch_tripped = True
            self.config.enabled = False
            await self.save_config()
            return True
        return False

    async def on_launch(self, launch_data: dict):
        """Called by listener for every new Pump.fun launch."""
        launch = Launch(
            mint=launch_data["mint"],
            creator=launch_data["creator"],
            bonding_curve=launch_data["bonding_curve"],
            name=launch_data.get("name"),
            symbol=launch_data.get("symbol"),
            signature=launch_data.get("signature"),
        )

        # Initial classification with no metrics yet (we'll re-check after window)
        verdict = classify(
            {
                "curve_fill_pct": 0,
                "elapsed_s": 0,
                "unique_buyers": 0,
                "sol_inflow": 0,
                "creator_rugs": 0,
            },
            self.rules.model_dump(),
        )
        launch.classifier_action = verdict["action"]
        launch.classifier_risk = verdict["risk"]
        launch.classifier_reasons = verdict["reasons"]

        # Persist launch
        doc = launch.model_dump()
        doc["detected_at"] = doc["detected_at"].isoformat()
        await self.db.launches.insert_one({**doc, "_id": launch.id})
        # Cache in memory (capped)
        self.recent_launches.insert(0, doc)
        self.recent_launches = self.recent_launches[:50]

        # Should we attempt entry?
        if not self.config.enabled:
            return
        if self.kill_switch_tripped:
            return
        if await self.check_kill_switch():
            return
        if launch.mint in self.active_trades:
            return
        if verdict["action"] == "abort_trade":
            return

        # Spawn entry+monitor coroutine
        asyncio.create_task(self._enter_and_monitor(launch))

    async def _enter_and_monitor(self, launch: Launch):
        try:
            await self._enter(launch)
        except Exception as e:
            logger.exception(f"entry failed for {launch.mint}: {e}")

    async def _enter(self, launch: Launch):
        sol_price = await get_sol_usd_price()
        # Choose trade size (clamped to max)
        trade_usd = min(self.config.max_trade_usd, max(self.config.min_trade_usd, self.config.min_trade_usd))
        trade_sol = trade_usd / sol_price if sol_price > 0 else 0
        sol_in_lamports = int(trade_sol * LAMPORTS_PER_SOL)
        if sol_in_lamports <= 0:
            return

        # Fetch bonding curve state
        state = await pumpfun.fetch_bonding_curve_state(launch.mint)
        if not state:
            logger.warning(f"No curve state for {launch.mint}, skipping")
            return
        if state["complete"]:
            return  # already graduated

        tokens_out, max_sol = pumpfun.quote_buy_tokens(
            state, sol_in_lamports, self.config.slippage_bps
        )
        if tokens_out <= 0:
            return

        entry_price_sol = sol_in_lamports / tokens_out / LAMPORTS_PER_SOL  # SOL per token
        mode = "live" if self.config.live_trading else "paper"

        trade = Trade(
            mint=launch.mint,
            name=launch.name,
            symbol=launch.symbol,
            status="active",
            mode=mode,
            entry_sol=trade_sol,
            entry_usd=trade_sol * sol_price,
            entry_tokens=tokens_out,
            entry_price_sol=entry_price_sol,
            risk_score=launch.classifier_risk or 50,
            classifier_action=launch.classifier_action,
        )

        if mode == "live":
            try:
                kp = get_keypair()
                user = get_pubkey()
                mint_pk = Pubkey.from_string(launch.mint)
                ixs = [
                    pumpfun.build_create_ata_ix(user, user, mint_pk),
                    pumpfun.build_buy_ix(user, mint_pk, tokens_out, max_sol),
                ]
                sig = await pumpfun.send_versioned_tx(
                    kp, ixs, self.config.priority_fee_microlamports
                )
                trade.entry_sig = sig
            except Exception as e:
                logger.exception(f"Live buy failed for {launch.mint}: {e}")
                trade.status = "failed"
                trade.exit_reason = f"buy failed: {e}"
                await self._persist_trade(trade)
                return

        await self._persist_trade(trade)
        self.active_trades[launch.mint] = {
            "trade": trade.model_dump(),
            "metrics": _new_metrics(),
            "launch": launch.model_dump(),
        }
        asyncio.create_task(self._monitor_position(launch.mint))

    async def _persist_trade(self, trade: Trade):
        doc = trade.model_dump()
        doc["entry_time"] = doc["entry_time"].isoformat()
        if doc.get("exit_time"):
            doc["exit_time"] = doc["exit_time"].isoformat()
        await self.db.trades.update_one(
            {"_id": trade.id}, {"$set": {**doc, "_id": trade.id}}, upsert=True
        )

    async def _monitor_position(self, mint: str):
        """Poll bonding curve, re-classify periodically, exit when conditions met."""
        slot = self.active_trades.get(mint)
        if not slot:
            return
        trade_doc = slot["trade"]
        metrics = slot["metrics"]
        start = time.time()
        max_hold = self.config.hold_max_seconds
        last_classify = 0.0

        try:
            while True:
                elapsed = time.time() - start
                if elapsed > max_hold:
                    await self._exit(mint, reason=f"timeout after {max_hold}s")
                    return

                state = await pumpfun.fetch_bonding_curve_state(mint)
                if not state:
                    await asyncio.sleep(1.0)
                    continue
                if state["complete"]:
                    await self._exit(mint, reason="bonding curve completed (LP about to deploy)")
                    return

                # Estimate current price = vSOL/vTOK (lamports per token base unit)
                cur_price_sol = state["virtual_sol_reserves"] / state["virtual_token_reserves"] / LAMPORTS_PER_SOL
                pct_change = (cur_price_sol - trade_doc["entry_price_sol"]) / max(trade_doc["entry_price_sol"], 1e-18) * 100

                if pct_change >= self.config.take_profit_pct:
                    await self._exit(mint, reason=f"take-profit hit (+{pct_change:.1f}%)")
                    return
                if pct_change <= -self.config.stop_loss_pct:
                    await self._exit(mint, reason=f"stop-loss hit ({pct_change:.1f}%)")
                    return

                # Update metrics window
                metrics["elapsed_s"] = elapsed
                # Curve fill % = real_sol_reserves / 85 SOL (Pump.fun graduates at ~85 SOL)
                metrics["curve_fill_pct"] = (state["real_sol_reserves"] / LAMPORTS_PER_SOL) / 85.0 * 100

                # Re-classify every 2s
                if time.time() - last_classify > 2.0:
                    last_classify = time.time()
                    verdict = classify(metrics, self.rules.model_dump())
                    trade_doc["risk_score"] = verdict["risk"]
                    if verdict["action"] == "abort_trade":
                        await self._exit(mint, reason=f"classifier abort: {verdict['reasons']}")
                        return
                    if verdict["action"] == "exit_early" and elapsed > 3:
                        await self._exit(mint, reason=f"classifier exit_early: {verdict['reasons']}")
                        return

                await asyncio.sleep(0.8)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(f"monitor error for {mint}: {e}")

    async def _exit(self, mint: str, reason: str):
        slot = self.active_trades.pop(mint, None)
        if not slot:
            return
        trade_doc = slot["trade"]
        sol_price = await get_sol_usd_price()
        state = await pumpfun.fetch_bonding_curve_state(mint)
        if not state:
            # Can't quote; mark as failed exit
            trade_doc["status"] = "closed"
            trade_doc["exit_reason"] = f"{reason} | curve state unavailable"
            trade_doc["exit_time"] = now_utc().isoformat()
            await self.db.trades.update_one(
                {"_id": trade_doc["id"]}, {"$set": trade_doc}, upsert=True
            )
            return

        tokens_in = int(trade_doc["entry_tokens"])
        sol_out, min_sol = pumpfun.quote_sell_sol(state, tokens_in, self.config.slippage_bps)
        exit_sol = sol_out / LAMPORTS_PER_SOL
        exit_price_sol = sol_out / tokens_in / LAMPORTS_PER_SOL if tokens_in > 0 else 0

        exit_sig = None
        if trade_doc["mode"] == "live":
            try:
                kp = get_keypair()
                user = get_pubkey()
                mint_pk = Pubkey.from_string(mint)
                ix = pumpfun.build_sell_ix(user, mint_pk, tokens_in, min_sol)
                exit_sig = await pumpfun.send_versioned_tx(
                    kp, [ix], self.config.priority_fee_microlamports
                )
            except Exception as e:
                logger.exception(f"Live sell failed: {e}")
                trade_doc["exit_reason"] = f"{reason} | sell failed: {e}"

        pnl_sol = exit_sol - trade_doc["entry_sol"]
        pnl_usd = pnl_sol * sol_price
        pnl_pct = (pnl_sol / trade_doc["entry_sol"] * 100) if trade_doc["entry_sol"] > 0 else 0

        trade_doc.update(
            {
                "status": "closed",
                "exit_time": now_utc().isoformat(),
                "exit_sol": exit_sol,
                "exit_usd": exit_sol * sol_price,
                "exit_price_sol": exit_price_sol,
                "exit_sig": exit_sig,
                "exit_reason": reason,
                "pnl_sol": pnl_sol,
                "pnl_usd": pnl_usd,
                "pnl_pct": pnl_pct,
            }
        )
        await self.db.trades.update_one(
            {"_id": trade_doc["id"]}, {"$set": trade_doc}, upsert=True
        )
        # Trigger kill switch check
        await self.check_kill_switch()


def _new_metrics() -> dict:
    return {
        "curve_fill_pct": 0.0,
        "elapsed_s": 0.0,
        "unique_buyers": 0,
        "sol_inflow": 0.0,
        "creator_rugs": 0,
    }
