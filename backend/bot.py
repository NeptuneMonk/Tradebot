"""
Bot orchestrator:
- Holds BotConfig & ClassifierRules state (persisted in Mongo)
- Receives new launches + trade events from the listener
- Tracks per-mint mempool metrics (unique buyers, SOL inflow) for first ~60s
- Computes a "social trending" score for the token name (no X API)
- Decides entry after a small assessment delay; monitors held positions for exit
"""
import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta

from solders.pubkey import Pubkey

from models import BotConfig, ClassifierRules, Launch, Trade, now_utc
from classifier import classify
import pumpfun
from solana_client import get_sol_usd_price, LAMPORTS_PER_SOL
from wallet import get_keypair, get_pubkey
from social import score_term
from ws_hub import hub
from creator_history import record_new_launch, mark_outcome, get_creator, derive_rug_count

logger = logging.getLogger("bot")

ASSESS_DELAY_S = 3.0          # wait this long after launch before deciding entry
TRACK_DURATION_S = 60.0       # how long to keep collecting metrics after launch
PERSIST_INTERVAL_S = 2.0      # how often to flush tracker metrics to DB


class BotState:
    def __init__(self, db):
        self.db = db
        self.config = BotConfig()
        self.rules = ClassifierRules()
        self.active_trades: dict[str, dict] = {}
        self.recent_launches: list[dict] = []
        self.kill_switch_tripped = False
        self.listener_connected = False
        # mint -> {launch_id, start, buyers:set, sol_inflow_lamports, buy_count,
        #          curve_fill_pct, social_score, social_sources, last_persist}
        self.tracking: dict[str, dict] = {}

    async def load(self):
        cfg = await self.db.bot_config.find_one({"_id": "current"}, {"_id": 0})
        if cfg:
            self.config = BotConfig(**cfg)
        rules = await self.db.classifier_rules.find_one({"_id": "current"}, {"_id": 0})
        if rules:
            self.rules = ClassifierRules(**rules)
        async for t in self.db.trades.find({"status": "active"}, {"_id": 0}):
            self.active_trades[t["mint"]] = {"trade": t}

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

    # ---------- Listener handlers ----------
    async def on_launch(self, launch_data: dict):
        """New Pump.fun token created."""
        launch = Launch(
            mint=launch_data["mint"],
            creator=launch_data["creator"],
            bonding_curve=launch_data["bonding_curve"],
            name=launch_data.get("name"),
            symbol=launch_data.get("symbol"),
            signature=launch_data.get("signature"),
        )

        # Record into creator history & get rug count
        creator_doc = await record_new_launch(self.db, launch.creator, launch.mint)
        creator_rugs = derive_rug_count(creator_doc)

        # Initial baseline classification (no metrics yet, but we have rug count)
        verdict = classify(
            {"curve_fill_pct": 0, "elapsed_s": 0, "unique_buyers": 0,
             "sol_inflow": 0, "creator_rugs": creator_rugs, "social_score": 0},
            self.rules.model_dump(),
        )
        launch.classifier_action = verdict["action"]
        launch.classifier_risk = verdict["risk"]
        launch.classifier_reasons = verdict["reasons"]

        doc = launch.model_dump()
        doc["detected_at"] = doc["detected_at"].isoformat()
        # Surface a few creator stats inline on the launch doc
        doc["creator_tokens_created"] = (creator_doc or {}).get("tokens_created", 1)
        doc["creator_tokens_failed"] = (creator_doc or {}).get("tokens_failed", 0)
        doc["creator_tokens_graduated"] = (creator_doc or {}).get("tokens_graduated", 0)
        await self.db.launches.insert_one({**doc, "_id": launch.id})
        self.recent_launches.insert(0, doc)
        self.recent_launches = self.recent_launches[:50]

        # WS push
        await hub.broadcast("launch", doc)

        # Start in-memory metric tracker for this mint
        self.tracking[launch.mint] = {
            "launch_id": launch.id,
            "creator": launch.creator,
            "start": time.time(),
            "buyers": set(),
            "sol_inflow_lamports": 0,
            "buy_count": 0,
            "curve_fill_pct": 0.0,
            "social_score": 0,
            "social_sources": {},
            "last_persist": 0.0,
            "name": launch.name,
            "symbol": launch.symbol,
            "creator_rugs": creator_rugs,
        }

        asyncio.create_task(self._compute_social(launch.mint))
        asyncio.create_task(self._assess_and_enter(launch, creator_rugs))
        asyncio.create_task(self._tracker_cleanup(launch.mint))

    async def on_trade(self, trade_data: dict):
        """A buy/sell event was observed on Pump.fun."""
        mint = trade_data["mint"]
        bucket = self.tracking.get(mint)
        if not bucket:
            return  # not tracking this mint
        if trade_data.get("is_buy"):
            bucket["buyers"].add(trade_data["user"])
            bucket["sol_inflow_lamports"] += int(trade_data.get("sol_amount", 0))
            bucket["buy_count"] += 1
        # Update virtual-reserve-derived fill % if event includes it
        vsr = trade_data.get("virtual_sol_reserves", 0)
        if vsr:
            # Pump.fun graduates around 85 SOL total raise (~30 real SOL in real_sol_reserves;
            # using vsr - initial_vsr as a proxy)
            bucket["curve_fill_pct"] = min(
                100.0, max(0.0, (vsr - 30_000_000_000) / (85_000_000_000) * 100)
            )

        # Throttled persistence
        now = time.time()
        if now - bucket.get("last_persist", 0) >= PERSIST_INTERVAL_S:
            bucket["last_persist"] = now
            await self._persist_metrics(mint)

    async def _persist_metrics(self, mint: str):
        b = self.tracking.get(mint)
        if not b:
            return
        update = {
            "unique_buyers": len(b["buyers"]),
            "sol_inflow": b["sol_inflow_lamports"] / LAMPORTS_PER_SOL,
            "buy_count": b["buy_count"],
            "curve_fill_pct": b["curve_fill_pct"],
            "social_score": b["social_score"],
            "social_sources": b["social_sources"],
        }
        await self.db.launches.update_one({"_id": b["launch_id"]}, {"$set": update})
        for r in self.recent_launches:
            if r.get("id") == b["launch_id"]:
                r.update(update)
                break
        # WS push
        await hub.broadcast("launch_update", {"id": b["launch_id"], "mint": mint, **update})

    async def _compute_social(self, mint: str):
        b = self.tracking.get(mint)
        if not b:
            return
        try:
            res = await score_term(b.get("name"), b.get("symbol"))
            b["social_score"] = int(res.get("score", 0))
            b["social_sources"] = res.get("sources", {})
            await self._persist_metrics(mint)
        except Exception as e:
            logger.debug(f"social score failed for {mint}: {e}")

    async def _tracker_cleanup(self, mint: str):
        await asyncio.sleep(TRACK_DURATION_S)
        # Final metric flush
        await self._persist_metrics(mint)
        # Determine outcome and update creator history
        b = self.tracking.get(mint)
        if b:
            try:
                state = await pumpfun.fetch_bonding_curve_state(mint)
                if state:
                    real_sol = state["real_sol_reserves"] / LAMPORTS_PER_SOL
                    if state["complete"]:
                        await mark_outcome(self.db, b["creator"], "graduated")
                    elif real_sol < 0.5 and b["sol_inflow_lamports"] / LAMPORTS_PER_SOL < 1.0:
                        # Abandoned: very little real SOL accumulated
                        await mark_outcome(self.db, b["creator"], "failed")
                    # else: still active — leave counter as-is
            except Exception as e:
                logger.debug(f"outcome check failed for {mint}: {e}")
        self.tracking.pop(mint, None)

    # ---------- Entry decision ----------
    async def _assess_and_enter(self, launch: Launch, creator_rugs: int = 0):
        try:
            await asyncio.sleep(ASSESS_DELAY_S)
            b = self.tracking.get(launch.mint, {})
            metrics = {
                "elapsed_s": time.time() - b.get("start", time.time()),
                "curve_fill_pct": b.get("curve_fill_pct", 0.0),
                "unique_buyers": len(b.get("buyers", set())),
                "sol_inflow": b.get("sol_inflow_lamports", 0) / LAMPORTS_PER_SOL,
                "creator_rugs": creator_rugs,
                "social_score": b.get("social_score", 0),
            }
            verdict = classify(metrics, self.rules.model_dump())
            # Update launch verdict in DB
            await self.db.launches.update_one(
                {"_id": launch.id},
                {"$set": {
                    "classifier_action": verdict["action"],
                    "classifier_risk": verdict["risk"],
                    "classifier_reasons": verdict["reasons"],
                }},
            )
            for r in self.recent_launches:
                if r.get("id") == launch.id:
                    r["classifier_action"] = verdict["action"]
                    r["classifier_risk"] = verdict["risk"]
                    r["classifier_reasons"] = verdict["reasons"]
                    break

            if not self.config.enabled or self.kill_switch_tripped:
                return
            if await self.check_kill_switch():
                return
            if launch.mint in self.active_trades:
                return
            if verdict["action"] == "abort_trade":
                return

            await self._enter(launch, verdict["risk"], verdict["action"])
        except Exception as e:
            logger.exception(f"assess_and_enter failed for {launch.mint}: {e}")

    # ---------- Entry / exit (live + paper) ----------
    async def _enter(self, launch: Launch, risk_score: int, action: str):
        sol_price = await get_sol_usd_price()
        trade_usd = max(self.config.min_trade_usd, self.config.max_trade_usd)
        trade_sol = trade_usd / sol_price if sol_price > 0 else 0
        sol_in_lamports = int(trade_sol * LAMPORTS_PER_SOL)
        if sol_in_lamports <= 0:
            return

        state = await pumpfun.fetch_bonding_curve_state(launch.mint)
        if not state or state["complete"]:
            return

        tokens_out, max_sol = pumpfun.quote_buy_tokens(
            state, sol_in_lamports, self.config.slippage_bps
        )
        if tokens_out <= 0:
            return

        entry_price_sol = sol_in_lamports / tokens_out / LAMPORTS_PER_SOL
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
            risk_score=risk_score,
            classifier_action=action,
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
        await self.db.launches.update_one({"_id": launch.id}, {"$set": {"entered": True}})
        for r in self.recent_launches:
            if r.get("id") == launch.id:
                r["entered"] = True
                break

        self.active_trades[launch.mint] = {"trade": trade.model_dump(), "launch": launch.model_dump()}
        await hub.broadcast("trade_enter", trade.model_dump())
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
        slot = self.active_trades.get(mint)
        if not slot:
            return
        trade_doc = slot["trade"]
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

                cur_price_sol = state["virtual_sol_reserves"] / state["virtual_token_reserves"] / LAMPORTS_PER_SOL
                pct_change = (cur_price_sol - trade_doc["entry_price_sol"]) / max(trade_doc["entry_price_sol"], 1e-18) * 100

                if pct_change >= self.config.take_profit_pct:
                    await self._exit(mint, reason=f"take-profit hit (+{pct_change:.1f}%)")
                    return
                if pct_change <= -self.config.stop_loss_pct:
                    await self._exit(mint, reason=f"stop-loss hit ({pct_change:.1f}%)")
                    return

                if time.time() - last_classify > 2.0:
                    last_classify = time.time()
                    b = self.tracking.get(mint, {})
                    metrics = {
                        "elapsed_s": elapsed + ASSESS_DELAY_S,
                        "curve_fill_pct": b.get("curve_fill_pct", 0.0),
                        "unique_buyers": len(b.get("buyers", set())),
                        "sol_inflow": b.get("sol_inflow_lamports", 0) / LAMPORTS_PER_SOL,
                        "creator_rugs": b.get("creator_rugs", 0),
                        "social_score": b.get("social_score", 0),
                    }
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
        await hub.broadcast("trade_exit", trade_doc)
        await self.check_kill_switch()
