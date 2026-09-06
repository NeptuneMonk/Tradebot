"""
rh_paper — Robinhood Chain (PONS) PAPER trader. Phase B.

Consumes the watch-only buckets kept by `rh_discovery` and simulates entries
and exits with pessimistic realism:
  - PONS curve fee (1%) on both legs, launch-window snipe tax, flat RH gas,
  - `paper_entry_latency_ms` / `paper_exit_latency_ms` (fills use the price
    observed AFTER the delay, not the decision price),
  - standard TP / SL / trailing / max-hold from BotConfig, plus a forced exit
    on graduation (curve is swept — the position can't stay on the curve).

Isolation: positions live in `RHPaperTrader.positions`, never in
`BotState.active_trades`; nothing here can send a Solana or EVM transaction.
Runs only while the bot is Running AND `rh_paper_enabled` is on — open
positions keep being monitored regardless so exits always land.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from models import Trade, now_utc
from ws_hub import hub
from reentry_logic import decide_reentry, recent_buyers_and_inflow, trigger_context
import rh_live
import rh_wallet

if TYPE_CHECKING:
    from bot import BotState

logger = logging.getLogger("rh_paper")

CHAIN = "rh"
PROTOCOL = "pons"
ENTRY_ACTION = "rh_pons_paper"
CURVE_FEE_BPS = 100
SNIPE_TAX_START_BPS = 9900
SNIPE_TAX_SECONDS = 3
RH_GAS_USD = 0.02
RH_BLOCK_TIME_S = 0.1
MONITOR_INTERVAL_S = 1.0


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def snipe_tax_bps(elapsed_s: float) -> int:
    """PONS: snipeTaxStartBps >> ((elapsed*14) // snipeTaxSeconds), zero once elapsed >= snipeTaxSeconds."""
    if elapsed_s >= SNIPE_TAX_SECONDS:
        return 0
    return SNIPE_TAX_START_BPS >> int((max(0.0, elapsed_s) * 14) // SNIPE_TAX_SECONDS)


def fee_fraction(elapsed_s: float) -> float:
    return min(0.999, (CURVE_FEE_BPS + snipe_tax_bps(elapsed_s)) / 10_000.0)


class RHPaperTrader:
    def __init__(self, state: "BotState"):
        self.live_kill_tripped = False
        self.last_live_error = ""
        self.state = state
        self.positions: dict[str, dict] = {}
        self.entered: set[str] = set()
        self.watch: dict[str, dict] = {}   # re-entry watch after winning exits
        self._task: asyncio.Task | None = None
        self._pending_entries: set[str] = set()
        self.stats = {"entries": 0, "exits": 0, "skipped": 0, "last_scan_ts": 0.0}

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    def _active(self) -> bool:
        cfg = self.state.config
        return bool(getattr(cfg, "enabled", False)) and (
            bool(getattr(cfg, "rh_paper_enabled", False)) or bool(getattr(cfg, "rh_live_trading", False)))

    async def _loop(self):
        await asyncio.sleep(4.0)
        await self._restore()
        while True:
            try:
                now = time.time()
                if self._active():
                    self._scan_entries(now)
                    self._scan_reentries(now)
                await self._monitor(now)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"rh_paper loop error: {e}")
            await asyncio.sleep(MONITOR_INTERVAL_S)

    async def _restore(self):
        try:
            docs = await self.state.db.trades.find({"chain": CHAIN, "status": "active"}, {"_id": 0}).to_list(50)
        except Exception:
            docs = []
        for d in docs:
            self.entered.add(d["mint"])
            self.positions[d["mint"]] = {"trade": d, "peak_price": d.get("entry_price_quote") or 0.0,
                                         "_last_price": d.get("entry_price_quote") or 0.0, "opened": time.time()}

    # ---------- entries ----------
    def _gates(self, token: str, b: dict, now: float) -> str | None:
        cfg = self.state.config
        if token in self.entered or token in self._pending_entries:
            return "already-entered"
        if len(self.positions) + len(self._pending_entries) >= cfg.rh_max_positions:
            return "max-positions"
        if b.get("graduated"):
            return "graduated"
        quote_usd = self._quote_usd(b["quote_symbol"])
        if quote_usd <= 0:
            return "unpriced-quote"
        age = now - b["start"]
        if age < cfg.rh_min_age_s or age > cfg.rh_max_age_min * 60:
            return "age"
        if not (cfg.rh_min_curve_pct <= b["curve_fill_pct"] <= cfg.rh_max_curve_pct):
            return "curve"
        mc = b["usd_market_cap"]
        if mc <= 0 or mc < cfg.rh_min_mc_usd or mc > cfg.rh_max_mc_usd:
            return "mc"
        if len(b["buyers"]) < cfg.rh_min_unique_buyers:
            return "buyers"
        cur, first = b["last_price_quote"], b["first_price_quote"]
        growth = ((cur - first) / first * 100.0) if first > 0 and cur > 0 else 0.0
        if growth < cfg.rh_min_growth_pct:
            return "growth"
        cutoff_inflow = now - cfg.scanner_recent_inflow_window_s
        cutoff_vel = now - cfg.scanner_holder_velocity_window_s
        inflow = 0.0
        recent = set()
        for ts, q, w in b["buy_events"]:
            if ts >= cutoff_inflow:
                inflow += q
            if ts >= cutoff_vel:
                recent.add(w)
        if len(recent) < cfg.rh_min_new_buyers_1m:
            return "new-buyers"
        if inflow * quote_usd < cfg.rh_min_inflow_usd:
            return "inflow"
        last_ms = b["last_trade_ms"]
        if not last_ms or now - last_ms / 1000.0 > cfg.rh_max_last_trade_age_s:
            return "stale"
        return None

    def _scan_entries(self, now: float):
        self.stats["last_scan_ts"] = now
        for token, b in list(self.state.rh_discovery.tracking.items()):
            reason = self._gates(token, b, now)
            if reason is None:
                self._pending_entries.add(token)
                asyncio.create_task(self._enter(token))
            else:
                self.stats["skipped"] += 1

    def _quote_usd(self, sym: str) -> float:
        return self.state.rh_discovery._quote_usd(sym)

    # ---------- re-entry watch (winners only; same knobs as the SOL watcher) ----------
    def _watch_after_exit(self, token: str, t: dict, price: float, b: dict | None, now: float):
        cfg = self.state.config
        prev = self.watch.get(token)
        if (t.get("pnl_pct") or 0) <= 0:
            # A losing leg ends the watch — no chained retries off a stale peak.
            self.watch.pop(token, None)
            return
        if not getattr(cfg, "reentry_enabled", True) or price <= 0:
            return
        if not b or b.get("graduated"):
            return
        self.watch[token] = {
            "attempts": int(prev.get("attempts") or 0) if prev else 0,
            "last_exit_time": now, "last_exit_was_sl": False,
            "trough_after_peak": price,
            "mint": token, "chain": CHAIN, "name": t.get("name"), "symbol": t.get("symbol"),
            "exit_price_quote": price, "exit_price_sol": 0.0, "quote_symbol": t.get("quote_symbol"),
            "exit_time": now, "max_attempts": int(cfg.reentry_max_attempts),
            "window_s": int(cfg.reentry_window_seconds), "pullback_pct": float(cfg.reentry_pullback_pct),
            "size_multiplier": float(cfg.reentry_size_multiplier), "original_pnl_usd": t.get("pnl_usd") or 0.0,
            "peak_price_after_exit": price, "creator": t.get("creator"),
        }

    def _scan_reentries(self, now: float):
        cfg = self.state.config
        for token, w in list(self.watch.items()):
            if now - w["exit_time"] > w["window_s"] or w["attempts"] >= w["max_attempts"]:
                self.watch.pop(token, None)
                continue
            b = self.state.rh_discovery.tracking.get(token)
            if not b or b.get("graduated"):
                self.watch.pop(token, None)
                continue
            price = b["last_price_quote"]
            if price <= 0 or token in self.positions or token in self._pending_entries:
                continue
            window_s = float(getattr(cfg, "exit_momentum_window_s", 10))
            n_buyers, inflow_q = recent_buyers_and_inflow(b["buy_events"], now, window_s)
            quote_usd = self._quote_usd(b["quote_symbol"])
            inflow_ok = inflow_q * quote_usd >= float(getattr(cfg, "rh_min_inflow_usd", 0) or 0) * 0.5
            trigger = decide_reentry(w, price, now, n_buyers, inflow_ok, cfg)
            if trigger is None:
                continue
            w["last_ctx"] = trigger_context(w, price, n_buyers, trigger)
            w["attempts"] += 1
            w["last_trigger"] = trigger
            self.entered.discard(token)
            self._pending_entries.add(token)
            asyncio.create_task(self._enter(token, size_mult=w["size_multiplier"], reentry=w["last_trigger"],
                                            reentry_ctx=w.get("last_ctx")))

    async def manual_enter(self, token: str) -> dict:
        """Operator override: skip the momentum gates, keep only the position cap."""
        cfg = self.state.config
        b = self.state.rh_discovery.tracking.get(token)
        if not b:
            return {"ok": False, "reason": "token not tracked"}
        if b.get("graduated"):
            return {"ok": False, "reason": "graduated — curve closed"}
        if token in self.positions or token in self._pending_entries:
            return {"ok": False, "reason": "already in an active position"}
        if len(self.positions) + len(self._pending_entries) >= cfg.rh_max_positions:
            return {"ok": False, "reason": f"RH max positions reached ({cfg.rh_max_positions})"}
        if self._quote_usd(b["quote_symbol"]) <= 0:
            return {"ok": False, "reason": f"quote asset {b['quote_symbol']} has no USD price — can't size the paper stake"}
        if (b.get("last_price_quote") or 0) <= 0:
            return {"ok": False, "reason": "no curve price yet (waiting for first trade)"}
        self.entered.discard(token)
        self._pending_entries.add(token)
        await self._enter(token, manual=True)
        if token in self.positions:
            return {"ok": True, "mint": token, "symbol": b.get("symbol"), "mode": "paper", "chain": CHAIN}
        return {"ok": False, "reason": "paper entry did not open (unpriced quote or no price yet)"}

    async def _enter(self, token: str, size_mult: float = 1.0, reentry: str | None = None,
                     manual: bool = False, reentry_ctx: dict | None = None):
        cfg = self.state.config
        try:
            await asyncio.sleep(max(0, cfg.paper_entry_latency_ms) / 1000.0)
            b = self.state.rh_discovery.tracking.get(token)
            if not b or b.get("graduated") or token in self.entered:
                return
            price = b["last_price_quote"]
            quote_usd = self._quote_usd(b["quote_symbol"])
            if price <= 0 or quote_usd <= 0:
                return
            now = time.time()
            fee = fee_fraction(now - b["start"])
            _gov = getattr(self.state, "bankroll", None)
            stake_usd = float(cfg.max_trade_usd) * max(0.1, float(size_mult)) * (_gov.size_mult() if _gov else 1.0)
            stake_quote = stake_usd / quote_usd
            tokens = stake_quote * (1.0 - fee) / price
            mode = "paper"
            live_fill = None
            if self.live_ok(b):
                live_fill = await self._live_buy(token, b, stake_quote, price)
                if live_fill is None:
                    return
                mode = "live"
                stake_quote = live_fill["quote_wei"] / 1e18
                tokens = live_fill["tokens_raw"] / 1e18
                stake_usd = stake_quote * quote_usd
            trade = Trade(
                mint=token, creator=b["creator"], name=b["name"], symbol=b["symbol"],
                mode=mode, entry_usd=stake_usd, entry_tokens=tokens,
                protocol=PROTOCOL, classifier_action=ENTRY_ACTION, risk_score=50,
                chain=CHAIN, quote_symbol=b["quote_symbol"], entry_quote=stake_quote,
                entry_price_quote=(stake_quote / tokens if (live_fill and tokens > 0) else price),
                fees_usd=((live_fill["fee_wei"] + live_fill["gas_cost_wei"]) / 1e18 * quote_usd) if live_fill
                         else stake_quote * fee * quote_usd + RH_GAS_USD,
            )
            doc = trade.model_dump()
            if live_fill:
                doc.update({"entry_sig": live_fill["tx"], "entry_tokens_raw": str(live_fill["tokens_raw"]),
                            "entry_gas_usd": live_fill["gas_cost_wei"] / 1e18 * quote_usd,
                            "entry_latency_s": live_fill["latency_s"], "entry_block": live_fill["block"]})
            # entry_time stays a datetime (bot.py stores BSON dates; a string
            # here would sort below every SOL trade in /trades/history).
            doc["launch_id"] = b["launch_id"]
            if reentry:
                doc["reentry"] = reentry
                doc["reentry_trigger"] = reentry
                doc["reentry_ctx"] = reentry_ctx
                doc["classifier_action"] = "rh_pons_reentry"
            if manual:
                doc["classifier_action"] = "rh_pons_manual"
            await self.state.db.trades.update_one({"_id": trade.id}, {"$set": {**doc, "_id": trade.id}}, upsert=True)
            self.positions[token] = {"trade": doc, "peak_price": price, "_last_price": price, "opened": now}
            self.entered.add(token)
            self.stats["entries"] += 1
            launch_update = {"entered": True, "entry_action": ENTRY_ACTION}
            await self.state.db.launches.update_one({"_id": b["launch_id"]}, {"$set": launch_update})
            await hub.broadcast("launch_update", {"id": b["launch_id"], "mint": token, **launch_update})
            await hub.broadcast("trade_enter", {**doc, "entry_time": _iso(doc["entry_time"])})
            logger.info(f"rh_paper ENTER {b['symbol']} {token[:10]} ${stake_usd:.2f} @ {price:.3e} {b['quote_symbol']} fee={fee*100:.2f}%")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"rh_paper enter failed {token[:10]}: {e}")
        finally:
            self._pending_entries.discard(token)

    # ---------- live execution ----------
    def live_ok(self, b: dict) -> bool:
        cfg = self.state.config
        return bool(getattr(cfg, "rh_live_trading", False)) and b.get("quote_symbol") == "ETH" \
            and not self.live_kill_tripped

    async def live_pnl_today_usd(self) -> float:
        since = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        total = 0.0
        async for d in self.state.db.trades.find({"chain": CHAIN, "mode": "live", "status": "closed",
                                                  "exit_time": {"$gte": since}}, {"_id": 0, "pnl_usd": 1}):
            total += float(d.get("pnl_usd") or 0.0)
        return total

    async def _live_buy(self, token: str, b: dict, stake_quote: float, price: float) -> dict | None:
        cfg = self.state.config
        try:
            bal = await rh_wallet.balance_wei()
            reserve = int(float(cfg.rh_gas_reserve_eth) * 1e18)
            quote_wei = int(stake_quote * 1e18)
            if bal - quote_wei < reserve:
                logger.warning(f"rh_live skip {b.get('symbol')}: balance {bal / 1e18:.5f} ETH < stake {stake_quote:.5f} + reserve")
                return None
            fill = await rh_live.buy(token, quote_wei, price, float(cfg.rh_live_slippage_pct))
            self.stats["live_buys"] = self.stats.get("live_buys", 0) + 1
            logger.warning(f"rh_live BUY {b.get('symbol')} {token[:10]} {fill['quote_wei'] / 1e18:.5f} ETH → {fill['tokens_raw'] / 1e18:,.0f} tokens tx={fill['tx'][:12]} ({fill['latency_s']}s)")
            return fill
        except Exception as e:
            self.stats["live_errors"] = self.stats.get("live_errors", 0) + 1
            self.last_live_error = f"buy {b.get('symbol')}: {e}"
            logger.error(f"rh_live buy failed {token[:10]}: {e}")
            return None

    async def _live_sell(self, token: str, t: dict, price: float) -> dict | None:
        cfg = self.state.config
        raw = int(t.get("entry_tokens_raw") or int(float(t["entry_tokens"]) * 1e18))
        try:
            on_chain = await rh_wallet.erc20_balance(token)
            if 0 < on_chain < raw:
                raw = on_chain
        except Exception:
            pass
        for attempt in range(3):
            try:
                fill = await rh_live.sell(token, raw, price, float(cfg.rh_live_slippage_pct) * (attempt + 1))
                self.stats["live_sells"] = self.stats.get("live_sells", 0) + 1
                logger.warning(f"rh_live SELL {t.get('symbol')} {token[:10]} {fill['quote_wei'] / 1e18:.5f} ETH tx={fill['tx'][:12]} ({fill['latency_s']}s)")
                return fill
            except Exception as e:
                self.last_live_error = f"sell {t.get('symbol')}: {e}"
                logger.error(f"rh_live sell attempt {attempt + 1} failed {token[:10]}: {e}")
                await asyncio.sleep(1.0 + attempt)
        return None

    async def _check_live_kill(self):
        cfg = self.state.config
        if not getattr(cfg, "rh_live_trading", False):
            return
        pnl = await self.live_pnl_today_usd()
        if pnl <= -abs(float(cfg.rh_daily_kill_switch_usd)):
            self.live_kill_tripped = True
            cfg.rh_live_trading = False
            await self.state.save_config()
            logger.critical(f"RH LIVE KILL SWITCH: today's live RH P/L {pnl:+.2f} $ ≤ -{cfg.rh_daily_kill_switch_usd:g} — rh_live_trading OFF")
            await hub.broadcast("rh_live_kill", {"pnl_today_usd": pnl, "limit": cfg.rh_daily_kill_switch_usd})

    # ---------- exits ----------
    def _decide_exit(self, pos: dict, b: dict, now: float) -> str | None:
        cfg = self.state.config
        t = pos["trade"]
        price = b["last_price_quote"] or pos["_last_price"]
        pos["_last_price"] = price
        entry = t["entry_price_quote"] or 0.0
        if entry <= 0 or price <= 0:
            return None
        if price > pos["peak_price"]:
            pos["peak_price"] = price
        pnl_pct = (price - entry) / entry * 100.0
        peak_pct = (pos["peak_price"] - entry) / entry * 100.0
        dd_from_peak = (pos["peak_price"] - price) / pos["peak_price"] * 100.0 if pos["peak_price"] > 0 else 0.0
        if b.get("graduated"):
            return "graduated"
        # Buy-momentum gate (buyers-only on RH — quote assets differ): defer
        # SL/TP while >= exit_momentum_min_buyers distinct wallets bought in
        # the window, bounded by max_defer_s and the hard SL floor.
        def _mom_holds(kind: str) -> bool:
            if not getattr(cfg, "exit_momentum_gate_enabled", True):
                return False
            if kind == "sl" and pnl_pct <= -float(getattr(cfg, "exit_momentum_hard_sl_pct", 60.0)):
                return False
            cutoff = now - float(getattr(cfg, "exit_momentum_window_s", 10))
            buyers = {w for ts, _q, w in b.get("buy_events", ()) if ts >= cutoff}
            key = f"_mom_defer_{kind}"
            if len(buyers) < int(getattr(cfg, "exit_momentum_min_buyers", 3)):
                pos.pop(key, None)
                return False
            started = pos.setdefault(key, now)
            return now - started < float(getattr(cfg, "exit_momentum_max_defer_s", 20))
        if pnl_pct >= cfg.take_profit_pct and _mom_holds("tp"):
            return None
        if pnl_pct <= -cfg.stop_loss_pct and _mom_holds("sl"):
            return None
        if (
            cfg.no_momentum_exit_enabled
            and not pos.get("_nm_checked")
            and now - pos["opened"] >= cfg.no_momentum_after_s
        ):
            pos["_nm_checked"] = True
            if peak_pct < cfg.no_momentum_min_mfe_pct:
                return "no_momentum"
        if pnl_pct >= cfg.take_profit_pct:
            return "take_profit"
        if pnl_pct <= -cfg.stop_loss_pct:
            return "stop_loss"
        if peak_pct >= cfg.trailing_arm_pct and dd_from_peak >= cfg.trailing_stop_pct:
            return "trailing_stop"
        if now - pos["opened"] >= cfg.hold_max_seconds:
            return "max_hold"
        return None

    async def _monitor(self, now: float):
        for token, pos in list(self.positions.items()):
            if pos.get("_exiting"):
                continue
            b = self.state.rh_discovery.tracking.get(token)
            if not b:
                pos["_exiting"] = True
                asyncio.create_task(self.exit(token, "tracking_lost"))
                continue
            reason = self._decide_exit(pos, b, now)
            if reason:
                pos["_exiting"] = True
                asyncio.create_task(self.exit(token, reason))

    # ---------- event-driven exits (per curve trade, block-accurate) ----------
    def latency_blocks(self) -> int:
        return max(1, int(round(self.state.config.paper_exit_latency_ms / 1000.0 / RH_BLOCK_TIME_S)))

    def on_trade(self, token: str, b: dict, tr: dict, now: float):
        """Called by rh_discovery for EVERY CurveBuy/CurveSell applied to a
        token we hold, in block order. Evaluates SL/TP/trail at the exact
        trade that breaches, then schedules the fill `latency_blocks` later
        instead of waiting for the 1s tick (by which time the dump has
        usually finished)."""
        pos = self.positions.get(token)
        if not pos or pos.get("_exiting"):
            return
        reason = self._decide_exit(pos, b, now)
        if not reason:
            return
        pos["_exiting"] = True
        pos["exit_trigger"] = {
            "reason": reason,
            "block": tr["block"],
            "price": tr["price"] or b["last_price_quote"],
            "fill_block": tr["block"] + self.latency_blocks(),
        }

    def resolve_pending(self, head: int):
        """After each poll: fill any triggered exit whose fill block has
        landed, at the last curve price seen at/before that block."""
        for token, pos in list(self.positions.items()):
            trig = pos.get("exit_trigger")
            if not trig or pos.get("_fill_task"):
                continue
            if head < trig["fill_block"]:
                continue
            b = self.state.rh_discovery.tracking.get(token)
            fill = trig["price"]
            if b:
                for blk, px in reversed(b.get("block_prices", ())):
                    if blk <= trig["fill_block"] and px > 0:
                        fill = px
                        break
            pos["_fill_task"] = True
            asyncio.create_task(self.exit(token, trig["reason"], fill_price=fill, trigger=trig))

    async def exit(self, token: str, reason: str, fill_price: float | None = None, trigger: dict | None = None):
        pos = self.positions.get(token)
        if not pos:
            return
        pos["_exiting"] = True
        cfg = self.state.config
        t = pos["trade"]
        try:
            if fill_price is None:
                await asyncio.sleep(max(0, cfg.paper_exit_latency_ms) / 1000.0)
                b = self.state.rh_discovery.tracking.get(token)
                price = (b["last_price_quote"] if b else 0.0) or pos["_last_price"] or t["entry_price_quote"]
            else:
                price = fill_price
            quote_usd = self._quote_usd(t.get("quote_symbol") or "ETH") or 0.0
            if quote_usd <= 0:
                quote_usd = t["entry_usd"] / t["entry_quote"] if t.get("entry_quote") else 0.0
            fee = CURVE_FEE_BPS / 10_000.0
            bk = self.state.rh_discovery.tracking.get(token) or {}
            if not t.get("symbol") and bk.get("symbol"):
                t["symbol"], t["name"] = bk.get("symbol"), bk.get("name")
            live_fill = None
            if t.get("mode") == "live":
                live_fill = await self._live_sell(token, t, price)
                if live_fill is None:
                    # stranded: keep the position open for the next tick rather than book a phantom exit
                    pos.pop("_exiting", None)
                    pos.pop("_fill_task", None)
                    t["exit_error"] = self.last_live_error
                    return
                proceeds_quote = live_fill["quote_wei"] / 1e18
                gas_usd = live_fill["gas_cost_wei"] / 1e18 * quote_usd
                price = proceeds_quote / t["entry_tokens"] if t.get("entry_tokens") else price
                exit_usd = max(0.0, proceeds_quote * quote_usd - gas_usd)
                t.update({"exit_sig": live_fill["tx"], "exit_gas_usd": gas_usd, "exit_latency_s": live_fill["latency_s"]})
            else:
                proceeds_quote = t["entry_tokens"] * price * (1.0 - fee)
                exit_usd = max(0.0, proceeds_quote * quote_usd - RH_GAS_USD)
            pnl_usd = exit_usd - t["entry_usd"]
            pnl_pct = (pnl_usd / t["entry_usd"] * 100.0) if t["entry_usd"] > 0 else 0.0
            t.update({
                "status": "closed", "exit_time": now_utc().isoformat(), "exit_reason": reason,
                "exit_usd": round(exit_usd, 6), "exit_quote": proceeds_quote, "exit_price_quote": price,
                "pnl_usd": round(pnl_usd, 6), "pnl_pct": round(pnl_pct, 4),
                "fees_usd": round((t.get("fees_usd") or 0.0) + ((live_fill["fee_wei"] + live_fill["gas_cost_wei"]) / 1e18 * quote_usd if live_fill
                                                                 else proceeds_quote / (1 - fee) * fee * quote_usd + RH_GAS_USD), 6),
                "peak_price_quote": pos["peak_price"],
                "exit_mode": "event" if trigger else "tick",
            })
            if trigger:
                t.update({
                    "exit_trigger_block": trigger["block"],
                    "exit_fill_block": trigger["fill_block"],
                    "exit_trigger_price_quote": trigger["price"],
                })
            await self.state.db.trades.update_one({"_id": t["id"]}, {"$set": t}, upsert=True)
            launch_update = {"pin_exited": True, "exit_pnl_pct": t["pnl_pct"], "exit_reason": reason}
            await self.state.db.launches.update_one({"_id": t.get("launch_id")}, {"$set": launch_update})
            await hub.broadcast("launch_update", {"id": t.get("launch_id"), "mint": token, **launch_update})
            await hub.broadcast("trade_exit", {**t, "entry_time": _iso(t.get("entry_time"))})
            self.stats["exits"] += 1
            self._watch_after_exit(token, t, price, self.state.rh_discovery.tracking.get(token), time.time())
            if live_fill:
                await self._check_live_kill()
            logger.info(f"rh_paper EXIT {t.get('symbol')} {token[:10]} {reason} pnl={pnl_pct:+.1f}% (${pnl_usd:+.3f})")
            self.positions.pop(token, None)
        except asyncio.CancelledError:
            pos.pop("_exiting", None)
            pos.pop("_fill_task", None)
            raise
        except Exception as e:
            logger.warning(f"rh_paper exit failed {token[:10]}: {e}")
            self.positions.pop(token, None)

    # ---------- API helpers ----------
    def augment_trade(self, d: dict):
        pos = self.positions.get(d.get("mint"))
        if not pos:
            return
        entry = d.get("entry_price_quote") or 0.0
        cur = pos["_last_price"] or entry
        if entry > 0 and cur > 0:
            d["current_price_quote"] = cur
            d["unrealized_pnl_pct"] = round((cur - entry) / entry * 100.0, 2)
            d["peak_price_quote"] = pos["peak_price"]
            if pos["peak_price"] > 0:
                d["drawdown_from_peak_pct"] = round((pos["peak_price"] - cur) / pos["peak_price"] * 100.0, 1)
        b = self.state.rh_discovery.tracking.get(d.get("mint")) or {}
        d["live_curve_fill_pct"] = b.get("curve_fill_pct") or 0
        d["live_usd_market_cap"] = b.get("usd_market_cap") or 0

    def augment_launch(self, row: dict):
        pos = self.positions.get(row.get("mint"))
        if not pos:
            return
        entry = pos["trade"].get("entry_price_quote") or 0.0
        cur = pos["_last_price"] or entry
        if entry > 0 and cur > 0:
            row["live_pnl_pct"] = round((cur - entry) / entry * 100.0, 1)
            if pos["peak_price"] > 0:
                row["live_drawdown_from_peak_pct"] = round((pos["peak_price"] - cur) / pos["peak_price"] * 100.0, 1)

    def status(self) -> dict:
        return {**self.stats, "active": self._active(), "open_positions": len(self.positions),
                "positions": [{"mint": m, "symbol": p["trade"].get("symbol"), "entry_price_quote": p["trade"].get("entry_price_quote"),
                               "last_price": p["_last_price"], "peak_price": p["peak_price"]} for m, p in self.positions.items()]}
