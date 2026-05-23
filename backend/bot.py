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
from collections import deque
from datetime import datetime, timezone, timedelta

from solders.pubkey import Pubkey

from models import BotConfig, ClassifierRules, Launch, Trade, now_utc
from classifier import classify
import pumpfun
import pumpswap
from solana_client import get_sol_usd_price, LAMPORTS_PER_SOL
from wallet import get_keypair, get_pubkey
from social import score_term
from ws_hub import hub
from creator_history import record_new_launch, mark_outcome, get_creator, derive_rug_count
from scanner import MomentumScanner, velocity_pct_strict
from discovery import PumpfunDiscovery

logger = logging.getLogger("bot")

ASSESS_DELAY_S = 3.0          # wait this long after launch before deciding entry
TRACK_DURATION_S = 60.0       # short-window heavy tracking (for fresh-launch classifier)
SCANNER_TRACK_HOURS = 4       # how long we keep light tracking for the scanner
PERSIST_INTERVAL_S = 2.0      # how often to flush tracker metrics to DB
MAX_TRACKED_MINTS = 500       # cap memory


class BotState:
    def __init__(self, db):
        self.db = db
        self.config = BotConfig()
        self.rules = ClassifierRules()
        self.active_trades: dict[str, dict] = {}
        self.recent_launches: list[dict] = []
        self.kill_switch_tripped = False
        self.listener_connected = False
        self.tracking: dict[str, dict] = {}
        # Re-entry watchlist: mint -> {exit_price_sol, exit_time, attempts, ...}
        self.reentry_watch: dict[str, dict] = {}
        self._reentry_task: asyncio.Task | None = None
        # Mints we have already entered (or attempted) so the scanner doesn't double-trade
        self.entered_mints: set[str] = set()
        self._scanner_task: asyncio.Task | None = None
        self.scanner = MomentumScanner(self)
        self.discovery = PumpfunDiscovery(self)

    async def load(self):
        cfg = await self.db.bot_config.find_one({"_id": "current"}, {"_id": 0})
        if cfg:
            self.config = BotConfig(**cfg)
        rules = await self.db.classifier_rules.find_one({"_id": "current"}, {"_id": 0})
        if rules:
            self.rules = ClassifierRules(**rules)
        async for t in self.db.trades.find({"status": "active"}, {"_id": 0}):
            self.active_trades[t["mint"]] = {"trade": t}
        # Start re-entry watcher
        if self._reentry_task is None or self._reentry_task.done():
            self._reentry_task = asyncio.create_task(self._reentry_watcher())
        # Start momentum scanner
        if self._scanner_task is None or self._scanner_task.done():
            self._scanner_task = asyncio.create_task(self.scanner.loop())
        # Start Pump.fun discovery (aged tokens)
        self.discovery.start()

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

    # ---------- Re-entry on winners ----------
    async def _reentry_watcher(self):
        """Background scanner: for every closed-profitable mint on the watchlist,
        watch the price for a pullback. If pullback >= configured pct AND the
        token has not died (real_sol_reserves still growing), re-enter at a
        smaller size. Capped by max_attempts per mint."""
        while True:
            try:
                now = time.time()
                to_remove: list[str] = []
                for mint, w in list(self.reentry_watch.items()):
                    if not self.config.reentry_enabled:
                        to_remove.append(mint); continue
                    if now - w["exit_time"] > w["window_s"]:
                        to_remove.append(mint); continue
                    if w["attempts"] >= w["max_attempts"]:
                        to_remove.append(mint); continue
                    if mint in self.active_trades:
                        continue  # already re-entered; wait for that to close
                    if not self.config.enabled or self.kill_switch_tripped:
                        continue
                    if await self.check_kill_switch():
                        continue

                    state = await pumpfun.fetch_bonding_curve_state(mint)
                    if not state or state["complete"]:
                        to_remove.append(mint); continue
                    cur_price = state["virtual_sol_reserves"] / state["virtual_token_reserves"] / LAMPORTS_PER_SOL
                    # Track peak after exit so we measure pullback from local high
                    if cur_price > w["peak_price_after_exit"]:
                        w["peak_price_after_exit"] = cur_price
                    pullback = (w["peak_price_after_exit"] - cur_price) / max(w["peak_price_after_exit"], 1e-18) * 100
                    if pullback >= w["pullback_pct"]:
                        # Optional sanity: only re-enter if curve still has inflow
                        # (compare cur_real_sol vs threshold). Skip strict check for now.
                        try:
                            await self._attempt_reentry(w)
                        except Exception as e:
                            logger.exception(f"reentry attempt failed for {mint}: {e}")
                for m in to_remove:
                    self.reentry_watch.pop(m, None)
                    await hub.broadcast("reentry_watch_remove", {"mint": m})
            except Exception as e:
                logger.debug(f"reentry watcher loop error: {e}")
            await asyncio.sleep(2.0)

    async def _attempt_reentry(self, w: dict):
        mint = w["mint"]
        # Apply the same portfolio + liquidity gates as fresh entries
        if len(self.active_trades) >= max(1, self.config.max_concurrent_positions):
            return
        sol_price = await get_sol_usd_price()
        base_usd = max(self.config.min_trade_usd, self.config.max_trade_usd)
        trade_usd = max(self.config.min_trade_usd, base_usd * w["size_multiplier"])
        trade_sol = trade_usd / sol_price if sol_price > 0 else 0
        sol_in_lamports = int(trade_sol * LAMPORTS_PER_SOL)
        if sol_in_lamports <= 0:
            return
        state = await pumpfun.fetch_bonding_curve_state(mint)
        if not state or state["complete"]:
            return
        real_sol = state["real_sol_reserves"] / LAMPORTS_PER_SOL
        if real_sol < self.config.min_curve_liquidity_sol:
            return
        tokens_out, max_sol = pumpfun.quote_buy_tokens(state, sol_in_lamports, self.config.slippage_bps)
        if tokens_out <= 0:
            return
        entry_price_sol = sol_in_lamports / tokens_out / LAMPORTS_PER_SOL
        mode = "live" if self.config.live_trading else "paper"
        trade = Trade(
            mint=mint,
            name=w.get("name"),
            symbol=w.get("symbol"),
            status="active",
            mode=mode,
            entry_sol=trade_sol,
            entry_usd=trade_sol * sol_price,
            entry_tokens=tokens_out,
            entry_price_sol=entry_price_sol,
            risk_score=40,
            classifier_action="reentry",
        )
        if mode == "live":
            try:
                kp = get_keypair()
                user = get_pubkey()
                mint_pk = Pubkey.from_string(mint)
                ixs = [
                    pumpfun.build_create_ata_ix(user, user, mint_pk),
                    pumpfun.build_buy_ix(user, mint_pk, tokens_out, max_sol),
                ]
                sig = await pumpfun.send_versioned_tx(kp, ixs, self.config.priority_fee_microlamports)
                trade.entry_sig = sig
            except Exception as e:
                logger.exception(f"Live re-entry buy failed for {mint}: {e}")
                trade.status = "failed"
                trade.exit_reason = f"reentry buy failed: {e}"
                await self._persist_trade(trade)
                return
        await self._persist_trade(trade)
        w["attempts"] += 1
        self.active_trades[mint] = {"trade": trade.model_dump(), "launch": {"creator": w.get("creator"), "mint": mint}}
        await hub.broadcast("trade_enter", trade.model_dump())
        await hub.broadcast("reentry_attempted", {"mint": mint, "attempts": w["attempts"]})
        asyncio.create_task(self._monitor_position(mint))

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
            "buy_events": deque(maxlen=500),  # (ts, sol_lamports, user)
            "sol_inflow_lamports": 0,
            "buy_count": 0,
            "curve_fill_pct": 0.0,
            "social_score": 0,
            "social_sources": {},
            "last_persist": 0.0,
            "name": launch.name,
            "symbol": launch.symbol,
            "creator_rugs": creator_rugs,
            "first_seen_price_sol": 0.0,  # filled on first TradeEvent / curve fetch
            "last_price_sol": 0.0,
            # Throttled price-time samples (~1Hz) for the entry-velocity gate
            "price_samples": deque(maxlen=120),  # 2min @ 1Hz
            "last_price_sample_ts": 0.0,
            "scanner_eligible": True,
            "scanner_last_attempt": 0.0,
            # Social proof — populated asynchronously by _fetch_socials below
            # (Pump.fun indexes the mint a few seconds after creation)
            "reply_count": 0,
            "twitter": "",
            "telegram": "",
            "website": "",
        }
        # LRU-style cap: drop oldest if over the limit
        if len(self.tracking) > MAX_TRACKED_MINTS:
            oldest = min(self.tracking.items(), key=lambda kv: kv[1]["start"])[0]
            self.tracking.pop(oldest, None)

        asyncio.create_task(self._compute_social(launch.mint))
        asyncio.create_task(self._fetch_pumpfun_socials(launch.mint))
        asyncio.create_task(self._assess_and_enter(launch, creator_rugs))
        asyncio.create_task(self._tracker_cleanup(launch.mint))

    async def on_trade(self, trade_data: dict):
        """A buy/sell event was observed on Pump.fun."""
        mint = trade_data["mint"]
        bucket = self.tracking.get(mint)
        # Track price via virtual reserves first (so active-trade fast path can use it)
        vsr = trade_data.get("virtual_sol_reserves", 0)
        vtr = trade_data.get("virtual_token_reserves", 0)
        cur_price = None
        if vsr and vtr:
            cur_price = vsr / vtr / LAMPORTS_PER_SOL

        # FAST EXIT PATH: if we hold this mint, check TP/SL on every trade event
        # (sub-100ms reaction instead of 800ms poll loop — eliminates SL overshoot)
        if cur_price and mint in self.active_trades:
            asyncio.create_task(self._check_fast_exit(mint, cur_price))

        if not bucket:
            return
        now = time.time()
        if trade_data.get("is_buy"):
            bucket["buyers"].add(trade_data["user"])
            bucket["sol_inflow_lamports"] += int(trade_data.get("sol_amount", 0))
            bucket["buy_count"] += 1
            bucket["buy_events"].append((now, int(trade_data.get("sol_amount", 0)), trade_data["user"]))
        if cur_price:
            if bucket["first_seen_price_sol"] <= 0:
                bucket["first_seen_price_sol"] = cur_price
            bucket["last_price_sol"] = cur_price
            bucket["last_vsr_lamports"] = vsr  # for real-SOL estimate in scanner snapshot
            bucket["curve_fill_pct"] = min(
                100.0, max(0.0, (vsr - 30_000_000_000) / (85_000_000_000) * 100)
            )
            # Throttled price sampling (~1Hz) for the entry-velocity gate
            if now - bucket.get("last_price_sample_ts", 0) >= 1.0:
                bucket["last_price_sample_ts"] = now
                samples = bucket.get("price_samples")
                if samples is not None:
                    samples.append((now, cur_price))

        if now - bucket.get("last_persist", 0) >= PERSIST_INTERVAL_S:
            bucket["last_persist"] = now
            await self._persist_metrics(mint)

    async def _check_fast_exit(self, mint: str, cur_price_sol: float):
        """Real-time TP/SL/trailing-stop check fired by on_trade.
        Idempotent: only acts once per mint."""
        slot = self.active_trades.get(mint)
        if not slot:
            return
        trade_doc = slot["trade"]
        entry_p = trade_doc.get("entry_price_sol", 0)
        if entry_p <= 0:
            return
        # Update peak for trailing stop
        peak = slot.get("peak_price_sol", entry_p)
        if cur_price_sol > peak:
            peak = cur_price_sol
            slot["peak_price_sol"] = peak

        pct_change = (cur_price_sol - entry_p) / entry_p * 100

        # Take profit — either full exit or partial-then-tighten-trailing.
        # NOTE: after a successful partial, we skip the TP check entirely — the
        # runner is governed by the tightened trailing stop only.
        if pct_change >= self.config.take_profit_pct and not slot.get("partial_done"):
            ptp = self.config.partial_tp_pct
            if 0 < ptp < 100:
                # Reserve the partial flag synchronously to prevent concurrent
                # fast-exit invocations from racing into the same partial.
                slot["partial_done"] = True
                did = await self._partial_exit(
                    mint, ptp / 100.0,
                    reason=f"partial-tp ({ptp:.0f}%) at +{pct_change:.1f}% [fast]",
                )
                if did:
                    slot["peak_price_sol"] = cur_price_sol
                    return
                # Partial failed — clear the reservation and fall through to full exit
                slot["partial_done"] = False
            await self._exit(mint, reason=f"take-profit hit (+{pct_change:.1f}%) [fast]")
            return
        # Trailing stop (if enabled and we have unrealized gain).
        # After partial TP, use the tighter trail to lock in runner gains.
        trail_pct = (
            self.config.partial_tp_trail_tighten_pct
            if slot.get("partial_done") and self.config.partial_tp_trail_tighten_pct > 0
            else self.config.trailing_stop_pct
        )
        if trail_pct > 0 and peak > entry_p:
            trail_drop = (peak - cur_price_sol) / peak * 100
            if trail_drop >= trail_pct:
                peak_pct = (peak - entry_p) / entry_p * 100
                await self._exit(
                    mint,
                    reason=f"trailing-stop hit (peak +{peak_pct:.1f}%, now +{pct_change:.1f}%) [fast]",
                )
                return
        # Hard stop loss
        if pct_change <= -self.config.stop_loss_pct:
            await self._exit(mint, reason=f"stop-loss hit ({pct_change:.1f}%) [fast]")
            return

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

    async def _fetch_pumpfun_socials(self, mint: str):
        """Pull on-chain social proof fields (reply_count, twitter, telegram,
        website) from Pump.fun's `/coins/{mint}` endpoint. Used by the entry
        gate. Re-tries a few times because the mint is only indexed by Pump's
        API after the first trade event hits it (usually 2-10s post-creation).
        """
        import httpx
        b = self.tracking.get(mint)
        if not b:
            return
        url = f"https://frontend-api-v3.pump.fun/coins/{mint}"
        # Up to 4 attempts with backoff (2s, 6s, 14s, 30s — covers ~50s window)
        for delay in (2.0, 6.0, 14.0, 30.0):
            await asyncio.sleep(delay)
            if mint not in self.tracking:
                return  # bucket evicted
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    r = await client.get(url, headers={"accept": "application/json"})
                    if r.status_code != 200:
                        continue
                    c = r.json() or {}
                    b["reply_count"] = int(c.get("reply_count") or 0)
                    b["twitter"] = (c.get("twitter") or "").strip()
                    b["telegram"] = (c.get("telegram") or "").strip()
                    b["website"] = (c.get("website") or "").strip()
                    # We got a real response — exit early. Any later updates
                    # will be picked up by the discovery refresh loop once the
                    # mint becomes discoverable.
                    return
            except Exception as e:
                logger.debug(f"social fetch retry for {mint}: {e}")

    async def _tracker_cleanup(self, mint: str):
        await asyncio.sleep(TRACK_DURATION_S)
        # Final metric flush for the heavy-tracking window
        await self._persist_metrics(mint)
        # Determine launch outcome at the 60s mark (good signal vs the 4h scanner window)
        b = self.tracking.get(mint)
        if b:
            try:
                state = await pumpfun.fetch_bonding_curve_state(mint)
                if state:
                    real_sol = state["real_sol_reserves"] / LAMPORTS_PER_SOL
                    if state["complete"]:
                        await mark_outcome(self.db, b["creator"], "graduated")
                    elif real_sol < 0.5 and b["sol_inflow_lamports"] / LAMPORTS_PER_SOL < 1.0:
                        await mark_outcome(self.db, b["creator"], "failed")
            except Exception as e:
                logger.debug(f"outcome check failed for {mint}: {e}")
        # Schedule final removal at cfg.scanner_window_hours (honors live config)
        async def _final_drop():
            try:
                window_h = max(1, int(self.config.scanner_window_hours))
            except Exception:
                window_h = SCANNER_TRACK_HOURS
            remaining = max(0, window_h * 3600 - TRACK_DURATION_S)
            await asyncio.sleep(remaining)
            self.tracking.pop(mint, None)
        asyncio.create_task(_final_drop())

    # ---------- Entry decision (assess only — entry handled by MomentumScanner) ----------
    async def _assess_and_enter(self, launch: Launch, creator_rugs: int = 0):
        """Runs the classifier on the early-window metrics so the Recent Launches
        feed shows a verdict, but does NOT auto-enter. All entries now flow
        through the momentum scanner (both new and seasoned bands)."""
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
        except Exception as e:
            logger.exception(f"assess failed for {launch.mint}: {e}")

    # ---------- Entry / exit (live + paper) ----------
    async def _enter(self, launch: Launch, risk_score: int, action: str):
        # Portfolio limit: don't pile in beyond max concurrent positions
        if len(self.active_trades) >= max(1, self.config.max_concurrent_positions):
            return

        sol_price = await get_sol_usd_price()
        trade_usd = max(self.config.min_trade_usd, self.config.max_trade_usd)
        trade_sol = trade_usd / sol_price if sol_price > 0 else 0
        sol_in_lamports = int(trade_sol * LAMPORTS_PER_SOL)
        if sol_in_lamports <= 0:
            return

        # Route by protocol — graduated tokens trade on PumpSwap AMM
        bucket = self.tracking.get(launch.mint, {})
        protocol = bucket.get("protocol") or "pumpfun"
        pumpswap_state: dict | None = None
        if protocol == "pumpswap":
            pool = bucket.get("pumpswap_pool") or (await pumpswap.find_pool_for_mint(launch.mint))
            if not pool:
                logger.info(f"skip {launch.mint} [pumpswap]: no pool found")
                return
            pumpswap_state = await pumpswap.fetch_pool_state(pool)
            if not pumpswap_state:
                logger.info(f"skip {launch.mint} [pumpswap]: pool state unavailable")
                return
            bucket["pumpswap_pool"] = pool
            state = {
                "real_sol_reserves": pumpswap_state["quote_reserves"],
                "complete": False,
            }
        else:
            state = await pumpfun.fetch_bonding_curve_state(launch.mint)
            if not state or state["complete"]:
                return

        # Resolve band-specific gates: "new" (action=momentum_new) uses tighter
        # thresholds, "seasoned" (action=scanner_momentum) uses base thresholds.
        is_new_band = action == "momentum_new"
        min_liq = self.config.min_curve_liquidity_sol_new if is_new_band else self.config.min_curve_liquidity_sol
        min_buyers = self.config.min_buyers_for_entry_new if is_new_band else self.config.min_buyers_for_entry

        # Liquidity gate: skip entry if curve has too little real SOL
        real_sol = state["real_sol_reserves"] / LAMPORTS_PER_SOL
        if real_sol < min_liq:
            logger.info(f"skip {launch.mint} [{action}]: liquidity {real_sol:.2f} SOL < min {min_liq}")
            return

        # Buyer gate (NEW band only — seasoned tokens don't flow through the
        # Helius mempool listener so buyers set is always empty)
        if is_new_band and min_buyers > 0:
            b = self.tracking.get(launch.mint, {})
            buyers = len(b.get("buyers", set()))
            if buyers < min_buyers:
                logger.info(f"skip {launch.mint} [{action}]: only {buyers} buyers < min {min_buyers}")
                return

        # Pre-trade classifier gate (NEW band PumpFun only — seasoned/PumpSwap
        # tokens don't have mempool metrics so the classifier would spuriously
        # abort them). If the classifier would abort/exit_early *immediately*
        # post-entry, refuse to enter — saves entry fees + exit slippage on a
        # certain loser.
        if is_new_band and protocol == "pumpfun":
            b = self.tracking.get(launch.mint, {})
            metrics = {
                "elapsed_s": time.time() - b.get("start", time.time()),
                "curve_fill_pct": b.get("curve_fill_pct", 0.0),
                "unique_buyers": len(b.get("buyers", set())),
                "sol_inflow": b.get("sol_inflow_lamports", 0) / LAMPORTS_PER_SOL,
                "creator_rugs": b.get("creator_rugs", 0),
                "social_score": b.get("social_score", 0),
            }
            verdict = classify(metrics, self.rules.model_dump())
            if verdict["action"] in ("abort_trade", "exit_early"):
                logger.info(
                    f"skip {launch.mint} [{action}]: pre-trade classifier "
                    f"{verdict['action']} — {verdict['reasons']}"
                )
                await hub.broadcast("scanner_skip", {
                    "mint": launch.mint, "symbol": launch.symbol,
                    "band": "new", "reason": verdict["action"],
                    "details": verdict["reasons"],
                })
                return

        # Entry-velocity gate (pattern-mining insight: SL exits dominate losers
        # 39% vs winners 2%). Reject "dead-cat" entries by requiring a minimum
        # price velocity over the configured window right before entry. Uses
        # `price_samples` populated by either on_trade (NEW band) or the
        # discovery refresh loop (SEASONED band). Skips silently if we don't
        # yet have enough history to span the requested window — safer than
        # using a partial-window reading to abort.
        b = self.tracking.get(launch.mint, {})
        samples = b.get("price_samples")
        vel_window = max(5, int(self.config.scanner_entry_velocity_window_s))
        velocity = velocity_pct_strict(samples, time.time(), vel_window) if samples else None
        if velocity is not None and velocity < self.config.scanner_entry_velocity_min_pct:
            logger.info(
                f"skip {launch.mint} [{action}]: entry velocity "
                f"{velocity:+.2f}% over {vel_window}s < min "
                f"{self.config.scanner_entry_velocity_min_pct:.2f}% (dead-cat filter)"
            )
            await hub.broadcast("scanner_skip", {
                "mint": launch.mint, "symbol": launch.symbol,
                "band": "new" if is_new_band else "seasoned",
                "reason": "entry_velocity",
                "details": [
                    f"velocity {velocity:+.2f}% over {vel_window}s < "
                    f"{self.config.scanner_entry_velocity_min_pct:.2f}%"
                ],
            })
            return

        # On-chain social-proof gate. When enabled, require at least one
        # social link (twitter / telegram / website) AND reply_count >= min.
        # If we have no Pump.fun metadata yet (fresh launch not yet indexed),
        # treat it as a fail — fees protection trumps timeliness.
        if self.config.gate_socials_required:
            b = self.tracking.get(launch.mint, {})
            reply_count = int(b.get("reply_count") or 0)
            has_social = bool((b.get("twitter") or b.get("telegram") or b.get("website") or "").strip())
            min_replies = max(0, int(self.config.gate_min_reply_count))
            if not has_social or reply_count < min_replies:
                logger.info(
                    f"skip {launch.mint} [{action}]: socials gate — "
                    f"reply_count={reply_count} (min {min_replies}), "
                    f"has_social={has_social}"
                )
                await hub.broadcast("scanner_skip", {
                    "mint": launch.mint, "symbol": launch.symbol,
                    "band": "new" if is_new_band else "seasoned",
                    "reason": "socials",
                    "details": [
                        f"reply_count={reply_count} < min {min_replies}"
                        if reply_count < min_replies
                        else "no twitter / telegram / website link"
                    ],
                })
                return

        tokens_out, max_sol = (
            pumpswap.quote_buy_tokens(pumpswap_state, sol_in_lamports, self.config.slippage_bps)
            if protocol == "pumpswap"
            else pumpfun.quote_buy_tokens(state, sol_in_lamports, self.config.slippage_bps)
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
        # Stash protocol on the trade dict (kept in active_trades) so _exit can route
        trade_extras = {"protocol": protocol, "pumpswap_pool": bucket.get("pumpswap_pool", "")}

        if mode == "live":
            try:
                kp = get_keypair()
                user = get_pubkey()
                mint_pk = Pubkey.from_string(launch.mint)
                if protocol == "pumpswap":
                    user_token_ata = pumpswap.get_associated_token_address(user, mint_pk, pumpswap.TOKEN_PROGRAM)
                    wsol_acc, wsol_ixs = pumpswap.build_wsol_wrap_ixs(user, max_sol)
                    ixs = [
                        pumpswap.build_create_ata_ix(user, user, mint_pk),
                        *wsol_ixs,
                        pumpswap.build_buy_ix(
                            user, pumpswap_state, user_token_ata, wsol_acc,
                            base_amount_out=tokens_out,
                            max_quote_amount_in=max_sol,
                        ),
                        pumpswap.build_close_wsol_ix(user, wsol_acc),
                    ]
                    sig = await pumpfun.send_versioned_tx(
                        kp, ixs, self.config.priority_fee_microlamports,
                        compute_unit_limit=400_000,
                    )
                else:
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
                # Cooldown the mint in the scanner so we don't retry the same
                # broken tx every pass.
                b = self.tracking.get(launch.mint)
                if b is not None:
                    b["scanner_last_attempt"] = time.time()
                return

        await self._persist_trade(trade)
        await self.db.launches.update_one({"_id": launch.id}, {"$set": {"entered": True}})
        for r in self.recent_launches:
            if r.get("id") == launch.id:
                r["entered"] = True
                break

        self.active_trades[launch.mint] = {
            "trade": trade.model_dump(),
            "launch": launch.model_dump(),
            **trade_extras,
        }
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

                # Protocol-aware price polling
                protocol = slot.get("protocol", "pumpfun")
                if protocol == "pumpswap":
                    pool = slot.get("pumpswap_pool") or ""
                    pool_state = await pumpswap.fetch_pool_state(pool) if pool else None
                    if not pool_state:
                        await asyncio.sleep(1.0)
                        continue
                    cur_price_sol = pumpswap.price_sol_per_raw_token(pool_state)
                else:
                    state = await pumpfun.fetch_bonding_curve_state(mint)
                    if not state:
                        await asyncio.sleep(1.0)
                        continue
                    if state["complete"]:
                        await self._exit(mint, reason="bonding curve completed (LP about to deploy)")
                        return
                    cur_price_sol = state["virtual_sol_reserves"] / state["virtual_token_reserves"] / LAMPORTS_PER_SOL

                pct_change = (cur_price_sol - trade_doc["entry_price_sol"]) / max(trade_doc["entry_price_sol"], 1e-18) * 100

                if pct_change >= self.config.take_profit_pct and not slot.get("partial_done"):
                    ptp = self.config.partial_tp_pct
                    if 0 < ptp < 100:
                        slot["partial_done"] = True
                        did = await self._partial_exit(
                            mint, ptp / 100.0,
                            reason=f"partial-tp ({ptp:.0f}%) at +{pct_change:.1f}%",
                        )
                        if did:
                            slot["peak_price_sol"] = cur_price_sol
                            await asyncio.sleep(0.5)
                            continue
                        slot["partial_done"] = False
                    await self._exit(mint, reason=f"take-profit hit (+{pct_change:.1f}%)")
                    return
                if pct_change <= -self.config.stop_loss_pct:
                    await self._exit(mint, reason=f"stop-loss hit ({pct_change:.1f}%)")
                    return

                # Classifier monitoring applies only to NEW band entries (fresh
                # mempool launches). Seasoned/PumpSwap trades skip it because
                # `curve_fill_pct=100` would trigger spurious exit_early signals
                # and we don't have meaningful mempool metrics for them.
                if (
                    time.time() - last_classify > 2.0
                    and trade_doc.get("classifier_action") == "momentum_new"
                    and protocol == "pumpfun"
                ):
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

    async def _partial_exit(self, mint: str, fraction: float, reason: str):
        """Sell `fraction` of the remaining position. Banks realized PnL onto
        the trade doc, reduces `entry_tokens`, and keeps the slot active so the
        runner can ride further with a tightened trailing stop.

        Returns True if a partial exit was actually performed."""
        slot = self.active_trades.get(mint)
        if not slot:
            return False
        if slot.get("partial_persisted"):
            return False  # already partialled — don't re-partial
        if not (0.0 < fraction < 1.0):
            return False
        trade_doc = slot["trade"]
        protocol = slot.get("protocol", "pumpfun")
        sol_price = await get_sol_usd_price()
        pumpswap_state = None
        if protocol == "pumpswap":
            pool = slot.get("pumpswap_pool") or ""
            pumpswap_state = await pumpswap.fetch_pool_state(pool) if pool else None
            state = pumpswap_state
        else:
            state = await pumpfun.fetch_bonding_curve_state(mint)
        if not state:
            return False

        held = int(trade_doc["entry_tokens"])
        sell_tokens = int(held * fraction)
        if sell_tokens <= 0:
            return False

        # For live trades, cap by ACTUAL wallet balance
        if trade_doc["mode"] == "live":
            try:
                user = get_pubkey()
                mint_pk = Pubkey.from_string(mint)
                ata = (
                    pumpswap.get_associated_token_address(user, mint_pk, pumpswap.TOKEN_PROGRAM)
                    if protocol == "pumpswap"
                    else pumpfun.derive_associated_token(user, mint_pk)
                )
                actual = await pumpswap.get_token_balance(ata)
                if actual > 0:
                    sell_tokens = min(sell_tokens, actual)
            except Exception as e:
                logger.warning(f"partial balance read failed for {mint}: {e}")

        exit_slip = self.config.exit_slippage_bps if self.config.exit_slippage_bps > 0 else self.config.slippage_bps
        if protocol == "pumpswap":
            sol_out, min_sol = pumpswap.quote_sell_sol(pumpswap_state, sell_tokens, exit_slip)
        else:
            sol_out, min_sol = pumpfun.quote_sell_sol(state, sell_tokens, exit_slip)
        if sol_out <= 0:
            return False
        partial_sol = sol_out / LAMPORTS_PER_SOL

        partial_sig = None
        if trade_doc["mode"] == "live":
            try:
                kp = get_keypair()
                user = get_pubkey()
                mint_pk = Pubkey.from_string(mint)
                if protocol == "pumpswap":
                    user_token_ata = pumpswap.get_associated_token_address(user, mint_pk, pumpswap.TOKEN_PROGRAM)
                    wsol_acc, wsol_ixs = pumpswap.build_wsol_wrap_ixs(user, 0)
                    ixs = [
                        *wsol_ixs,
                        pumpswap.build_sell_ix(
                            user, pumpswap_state, user_token_ata, wsol_acc,
                            base_amount_in=sell_tokens, min_quote_amount_out=min_sol,
                        ),
                        pumpswap.build_close_wsol_ix(user, wsol_acc),
                    ]
                    partial_sig = await pumpfun.send_versioned_tx(
                        kp, ixs, self.config.priority_fee_microlamports, compute_unit_limit=400_000,
                    )
                else:
                    ix = pumpfun.build_sell_ix(user, mint_pk, sell_tokens, min_sol)
                    partial_sig = await pumpfun.send_versioned_tx(
                        kp, [ix], self.config.priority_fee_microlamports
                    )
            except Exception as e:
                logger.exception(f"partial sell failed for {mint}: {e}")
                return False

        # Compute realized contribution from this partial
        entry_sol_per_token = trade_doc["entry_sol"] / max(trade_doc["entry_tokens"], 1)
        partial_cost_sol = entry_sol_per_token * sell_tokens
        realized_sol = partial_sol - partial_cost_sol
        realized_usd = realized_sol * sol_price

        # Update trade doc — reduce remaining position, bank realized PnL
        trade_doc["partial_done"] = True
        trade_doc["partial_sell_tokens"] = sell_tokens
        trade_doc["partial_sell_sol"] = partial_sol
        trade_doc["partial_sell_usd"] = partial_sol * sol_price
        trade_doc["partial_realized_sol"] = realized_sol
        trade_doc["partial_realized_usd"] = realized_usd
        trade_doc["partial_sig"] = partial_sig
        trade_doc["partial_reason"] = reason
        trade_doc["entry_tokens"] = held - sell_tokens
        trade_doc["entry_sol"] = trade_doc["entry_sol"] - partial_cost_sol
        trade_doc["entry_usd"] = trade_doc["entry_sol"] * sol_price
        slot["partial_done"] = True
        slot["partial_persisted"] = True

        await self.db.trades.update_one(
            {"_id": trade_doc["id"]}, {"$set": trade_doc}, upsert=True
        )
        await hub.broadcast("trade_partial", {
            "id": trade_doc["id"], "mint": mint, "symbol": trade_doc.get("symbol"),
            "partial_realized_usd": realized_usd, "fraction": fraction, "reason": reason,
        })
        logger.info(
            f"partial exit {mint} ({fraction*100:.0f}%): banked ${realized_usd:+.2f}, "
            f"remaining {trade_doc['entry_tokens']} tokens"
        )
        return True

    async def _exit(self, mint: str, reason: str):
        slot = self.active_trades.pop(mint, None)
        if not slot:
            return
        trade_doc = slot["trade"]
        sol_price = await get_sol_usd_price()
        protocol = slot.get("protocol", "pumpfun")
        # Resolve sell quote per protocol
        pumpswap_state = None
        if protocol == "pumpswap":
            pool = slot.get("pumpswap_pool") or ""
            pumpswap_state = await pumpswap.fetch_pool_state(pool) if pool else None
            state = pumpswap_state
        else:
            state = await pumpfun.fetch_bonding_curve_state(mint)
        if not state:
            trade_doc["status"] = "closed"
            trade_doc["exit_reason"] = f"{reason} | {protocol} state unavailable"
            trade_doc["exit_time"] = now_utc().isoformat()
            await self.db.trades.update_one(
                {"_id": trade_doc["id"]}, {"$set": trade_doc}, upsert=True
            )
            return

        tokens_in = int(trade_doc["entry_tokens"])
        # Use exit_slippage_bps if user has set it, else fall back to slippage_bps
        exit_slip = self.config.exit_slippage_bps if self.config.exit_slippage_bps > 0 else self.config.slippage_bps

        # For live trades, size the sell by the ACTUAL wallet balance — fees
        # taken at buy time mean our balance is usually a touch lower than
        # entry_tokens, and trying to sell more than we hold reverts the tx.
        if trade_doc["mode"] == "live":
            try:
                user = get_pubkey()
                mint_pk = Pubkey.from_string(mint)
                if protocol == "pumpswap":
                    ata = pumpswap.get_associated_token_address(user, mint_pk, pumpswap.TOKEN_PROGRAM)
                    actual = await pumpswap.get_token_balance(ata)
                else:
                    ata = pumpfun.derive_associated_token(user, mint_pk)
                    actual = await pumpswap.get_token_balance(ata)
                if actual > 0:
                    tokens_in = min(tokens_in, actual)
            except Exception as e:
                logger.warning(f"balance read failed for {mint}, falling back to entry_tokens: {e}")

        if protocol == "pumpswap":
            sol_out, min_sol = pumpswap.quote_sell_sol(pumpswap_state, tokens_in, exit_slip)
        else:
            sol_out, min_sol = pumpfun.quote_sell_sol(state, tokens_in, exit_slip)
        exit_sol = sol_out / LAMPORTS_PER_SOL
        exit_price_sol = sol_out / tokens_in / LAMPORTS_PER_SOL if tokens_in > 0 else 0

        exit_sig = None
        if trade_doc["mode"] == "live":
            try:
                kp = get_keypair()
                user = get_pubkey()
                mint_pk = Pubkey.from_string(mint)
                if protocol == "pumpswap":
                    user_token_ata = pumpswap.get_associated_token_address(user, mint_pk, pumpswap.TOKEN_PROGRAM)
                    wsol_acc, wsol_ixs = pumpswap.build_wsol_wrap_ixs(user, 0)
                    ixs = [
                        *wsol_ixs,
                        pumpswap.build_sell_ix(
                            user, pumpswap_state, user_token_ata, wsol_acc,
                            base_amount_in=tokens_in,
                            min_quote_amount_out=min_sol,
                        ),
                        pumpswap.build_close_wsol_ix(user, wsol_acc),
                    ]
                    exit_sig = await pumpfun.send_versioned_tx(
                        kp, ixs, self.config.priority_fee_microlamports,
                        compute_unit_limit=400_000,
                    )
                else:
                    ix = pumpfun.build_sell_ix(user, mint_pk, tokens_in, min_sol)
                    exit_sig = await pumpfun.send_versioned_tx(
                        kp, [ix], self.config.priority_fee_microlamports
                    )
            except Exception as e:
                logger.exception(f"Live sell failed: {e}")
                trade_doc["exit_reason"] = f"{reason} | sell failed: {e}"

        pnl_sol = exit_sol - trade_doc["entry_sol"]
        # Combine with any earlier partial-TP realised PnL so the trade's
        # reported total reflects both legs.
        partial_realized_sol = float(trade_doc.get("partial_realized_sol") or 0.0)
        partial_realized_usd = float(trade_doc.get("partial_realized_usd") or 0.0)
        total_pnl_sol = pnl_sol + partial_realized_sol
        total_pnl_usd = (pnl_sol * sol_price) + partial_realized_usd
        # PnL % is over the ORIGINAL cost basis so it stays comparable to
        # non-partial trades. We reconstruct original cost = current entry_sol +
        # partial cost basis (= partial_sell_sol - partial_realized_sol).
        orig_cost_sol = (
            trade_doc["entry_sol"]
            + (float(trade_doc.get("partial_sell_sol") or 0.0) - partial_realized_sol)
        )
        pnl_pct = (total_pnl_sol / orig_cost_sol * 100) if orig_cost_sol > 0 else 0

        trade_doc.update(
            {
                "status": "closed",
                "exit_time": now_utc().isoformat(),
                "exit_sol": exit_sol,
                "exit_usd": exit_sol * sol_price,
                "exit_price_sol": exit_price_sol,
                "exit_sig": exit_sig,
                "exit_reason": reason,
                "pnl_sol": total_pnl_sol,
                "pnl_usd": total_pnl_usd,
                "pnl_pct": pnl_pct,
            }
        )
        await self.db.trades.update_one(
            {"_id": trade_doc["id"]}, {"$set": trade_doc}, upsert=True
        )
        await hub.broadcast("trade_exit", trade_doc)
        # Re-entry watchlist: if we exited profitably and curve hasn't graduated, watch for a pullback
        if self.config.reentry_enabled and total_pnl_sol > 0 and not state.get("complete", False):
            self.reentry_watch[mint] = {
                "mint": mint,
                "name": trade_doc.get("name"),
                "symbol": trade_doc.get("symbol"),
                "exit_price_sol": exit_price_sol,
                "exit_time": time.time(),
                "attempts": 0,
                "max_attempts": self.config.reentry_max_attempts,
                "window_s": self.config.reentry_window_seconds,
                "pullback_pct": self.config.reentry_pullback_pct,
                "size_multiplier": self.config.reentry_size_multiplier,
                "original_pnl_usd": total_pnl_usd,
                "peak_price_after_exit": exit_price_sol,
                "creator": (slot.get("launch") or {}).get("creator"),
            }
            await hub.broadcast("reentry_watch_add", self.reentry_watch[mint])
        await self.check_kill_switch()
