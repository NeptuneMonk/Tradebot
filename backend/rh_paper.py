"""
rh_paper — Robinhood Chain (PONS) PAPER trader. Phase B.

Consumes the watch-only buckets kept by `rh_discovery` and simulates entries
and exits with pessimistic realism:
  - PONS curve fee (1%) on both legs, launch-window snipe tax, flat RH gas,
  - `paper_entry_latency_ms` / `paper_exit_latency_ms` (fills use the price
    observed AFTER the delay, not the decision price),
  - standard TP / SL / trailing / max-hold from BotConfig. A held token that
    graduates keeps riding the same ladder on the Uniswap v4 pool (`rh_dex`):
    prices come from PoolManager Swap events, fills from the V4Quoter / router.

Isolation: positions live in `RHPaperTrader.positions`, never in
`BotState.active_trades`; nothing here can send a Solana or EVM transaction.
Runs only while the bot is Running AND `rh_paper_enabled` is on — open
positions keep being monitored regardless so exits always land.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from models import Trade, now_utc
from ws_hub import hub
from reentry_policy import ReentryLedger
from reentry_logic import (decide_reentry, hot_walk_away_reason, recent_buyers_and_inflow, trigger_context,
                           update_swings, update_watch_price)
import rh_dex
import rh_live
import rh_wallet
from pymongo.errors import DuplicateKeyError
from flush import dip_forensics, is_flush
import flow
from exits import is_manual_hold

if TYPE_CHECKING:
    from bot import BotState

logger = logging.getLogger("rh_paper")

CHAIN = "rh"
PROTOCOL = "pons"
ENTRY_ACTION = "rh_pons_paper"
CURVE_FEE_BPS = 100
FAST_FAIL_ADD_AT_PCT = 5.0   # hardwired: the second half of the planned size is bought at the first +5 %
POOL_FEE_FRACTION = rh_dex.POOL_FEE_BPS / 10_000.0  # hook take on the post-graduation v4 pool (paper fallback)
SNIPE_TAX_START_BPS = 9900
SNIPE_TAX_SECONDS = 3
RH_GAS_USD = 0.09  # fallback per-side gas; live fills refine it (observed ~$0.08 buy / ~$0.10 sell)
LIVE_SELL_RETRY_COOLDOWN_S = 10.0  # after 3 failed on-chain sells, wait before the next exit attempt
EXIT_ERROR_RETRY_S = 5.0        # unexpected exit failure: keep the position, retry after this
ZERO_QUOTE_RETRY_S = 5.0  # paper exit met a zero quote with tokens held (dead venue) — retry, never book -100%
TRACKING_LOST_GRACE_S = 120.0  # held position with no bucket (restart): try to rehydrate from the v4 pool before giving up
REHYDRATE_RETRY_S = 15.0
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
        self.hot_history: deque = deque(maxlen=20)   # hot tokens we walked away from (reason + stats)
        self._last_fresh_entry_ts = 0.0
        self._task: asyncio.Task | None = None
        self._pending_entries: set[str] = set()
        self._enter_inflight: set[str] = set()
        self._pending_since: dict[str, float] = {}
        self.pending_buys: dict[str, dict] = {}
        self.reentry = ReentryLedger()   # closed tokens are back in play once the gates pass — under the reentry_* controls
        self.stats = {"entries": 0, "exits": 0, "skipped": 0, "last_scan_ts": 0.0}

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    def _active(self) -> bool:
        cfg = self.state.config
        return bool(getattr(cfg, "enabled", False)) and (
            bool(getattr(cfg, "rh_paper_enabled", False)) or bool(getattr(cfg, "rh_live_trading", False)))

    def counted_open(self) -> int:
        """Open RH positions that consume an rh_max_positions slot — manual holds don't."""
        return sum(1 for p in self.positions.values() if not is_manual_hold(p.get("trade")))

    def _may_open(self) -> bool:
        """New RH entries need the RH book armed AND no graceful stop in flight (a stop must drain, not keep re-filling)."""
        return self._active() and not getattr(self.state, "stopping_gracefully", False)

    async def tick(self, now: float):
        if self._may_open():
            self._scan_entries(now)
            self._scan_reentries(now)
        await self._monitor(now)

    async def _loop(self):
        await asyncio.sleep(4.0)
        await self._restore()
        while True:
            try:
                await self.tick(time.time())
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
                                         "_last_price": d.get("entry_price_quote") or 0.0, "opened": time.time(),
                                         "_r_trail_peak": d.get("entry_price_quote") or 0.0 if d.get("r_trail") else 0.0}

    # ---------- entries ----------
    def _gates(self, token: str, b: dict, now: float) -> str | None:
        cfg = self.state.config
        if token in self.positions or token in self._pending_entries:
            return "already-entered"
        blk, _ = self.reentry.check(token, cfg, now)      # SL cooldown first, then the reentry_* controls inside the window
        if blk:
            return blk
        if self.counted_open() + len(self._pending_entries) >= cfg.rh_max_positions:
            return "max-positions"
        eb = b.get("entry_block")
        if eb and now < eb[1]:
            return eb[0]                                  # a post-gate skip (cost-gate, r-size, …) holds its verdict briefly
        cg = b.get("creator_gate")
        if cg and now - cg[1] < 300.0:
            return cg[0]                                  # deployer-solvency verdict cached for the balance TTL
        if b.get("manual"):
            return "ladder-only"                          # operator-pinned established token: the Graduate Ladder is its only book
        seasoned = bool(b.get("graduated"))
        if seasoned:
            # post-sweep: the curve is gone. Enter only on a LIVE v4 pool with a fresh print, priced from pool swaps.
            if not b.get("pool_live"):
                return "rh-grad-no-pool"
            if now - float(b.get("last_pool_swap_ts") or 0) > float(getattr(cfg, "seasoned_max_last_trade_s", 20.0) or 20.0):
                return "rh-seasoned-stale"
            if now - float(b.get("graduated_at") or b["start"]) > float(getattr(cfg, "rh_seasoned_max_age_min", 60.0) or 60.0) * 60:
                return "rh-seasoned-age"
            # live entries here go through rh_dex.buy (quote → token on the v4 pool); ERC-20 quotes need rh_live_erc20_quotes
            # and a wallet that already holds the quote (checked in _live_buy), so nothing extra to refuse here
        blk = getattr(self.state, "search_regime_block", lambda: None)()
        if blk:
            return blk
        quote_usd = self._quote_usd(b["quote_symbol"])
        if quote_usd <= 0:
            return "unpriced-quote"
        age = now - b["start"]
        if not seasoned and (age < cfg.rh_min_age_s or age > cfg.rh_max_age_min * 60):
            return "age"
        from book_params import regime_gate_mult
        rm = regime_gate_mult(cfg, "rh_pons", self._launch_rate(now))   # quiet/busy hours scale the gates
        ld = getattr(self.state, "live_doctor", None)
        if ld is not None:
            rm *= ld.book_adjust("rh_pons", bool(getattr(cfg, "rh_live_trading", False)))[1]
        if not seasoned and not (cfg.rh_min_curve_pct <= b["curve_fill_pct"] <= cfg.rh_max_curve_pct):
            return "curve"
        mc = b["usd_market_cap"]
        if mc <= 0 or mc < cfg.rh_min_mc_usd * rm or mc > cfg.rh_max_mc_usd:
            return "mc"
        if len(b["buyers"]) < cfg.rh_min_unique_buyers * rm:
            return "buyers"
        cur, first = b["last_price_quote"], b["first_price_quote"]
        growth = ((cur - first) / first * 100.0) if first > 0 and cur > 0 else 0.0
        if growth < cfg.rh_min_growth_pct * rm:
            return "growth"
        if growth >= float(getattr(cfg, "rh_max_growth_pct", 400.0) or 400.0):
            return "chased"
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
        if inflow * quote_usd < cfg.rh_min_inflow_usd * rm:
            return "inflow"
        last_ms = b["last_trade_ms"]
        if not last_ms or now - last_ms / 1000.0 > cfg.rh_max_last_trade_age_s:
            return "stale"
        return None

    def _scan_entries(self, now: float):
        self.stats["last_scan_ts"] = now
        focus_block = self._focus_blocks_fresh(now)
        ld = getattr(self.state, "live_doctor", None)
        benched = bool(ld is not None and ld.book_benched("rh_pons", bool(getattr(self.state.config, "rh_live_trading", False))))
        for token, b in list(self.state.rh_discovery.tracking.items()):
            reason = self._gates(token, b, now)
            if reason is None and benched:
                reason = "doctor-breaker"   # would have entered — rh_pons is benched by the live-doctor breaker (LIFT to override)
            if reason is None and focus_block:
                reason = focus_block   # would have entered — deferred by hot focus
                self.stats["focus_deferred"] = self.stats.get("focus_deferred", 0) + 1
            verdict = "pass" if reason is None else reason
            tally = self.stats.setdefault("skip_reasons", {})
            key = ("seasoned:" if b.get("graduated") else "curve:") + verdict
            tally[key] = tally.get(key, 0) + 1
            if b.get("gate_reason") != verdict:
                b["gate_reason"] = verdict
                self.state.rh_discovery._dirty.add(token)      # verdict change → launch_update → feed
            if reason is None:
                self._pending_entries.add(token)
                self._last_fresh_entry_ts = now
                focus_block = self._focus_blocks_fresh(now)   # one fresh entry per cooldown while focused
                self._ledger(b, now, "entered")
                _, rmult = self.reentry.check(token, self.state.config, now)
                if rmult is not None:
                    asyncio.create_task(self._enter(token, size_mult=rmult, reentry="gates"))   # gates passed again inside the window
                else:
                    asyncio.create_task(self._enter(token))
            else:
                self.stats["skipped"] += 1
                self._ledger(b, now, reason)

    @staticmethod
    def _ledger(b: dict, now: float, reason: str):
        """Decision ledger: record each gate verdict transition (ts, reason, price) so the replay can score
        every gate by what the token did afterwards. Only transitions are kept — ~12 per token max."""
        if reason in ("already-entered", "sl-cooldown", "reentry-wait", "reentry-max", "reentry-off", "max-positions", "unpriced-quote", "stale", "graduated", "doctor-breaker"):
            return
        log = b.setdefault("decisions", [])
        if log and log[-1][1] == reason:
            return
        if len(log) >= 12:
            return
        log.append((round(now, 1), reason, float(b.get("last_price_quote") or 0.0)))

    def _block_entry(self, token: str, b: dict, reason: str, now: float, ttl_s: float = 30.0, detail: str | None = None) -> None:
        """A skip that fires AFTER the momentum gates passed: show it on the feed instead of a stale 'gate ✓'
        and hold the verdict for `ttl_s` so the scan does not re-run the whole entry path every second."""
        b["gate_reason"] = reason
        b["gate_detail"] = detail
        b["entry_block"] = (reason, now + ttl_s)
        tally = self.stats.setdefault("skip_reasons", {})
        key = ("seasoned:" if b.get("graduated") else "curve:") + reason
        tally[key] = tally.get(key, 0) + 1
        self.state.rh_discovery._dirty.add(token)

    def _quote_usd(self, sym: str) -> float:
        return self.state.rh_discovery._quote_usd(sym)

    @staticmethod
    def _quote_asset(b_or_t: dict) -> tuple[str, int]:
        """(pair_token, decimals) of the quote a bucket / trade is denominated in — native ETH when unknown."""
        pair = b_or_t.get("pair_token")
        if pair:
            return pair, int(b_or_t.get("quote_decimals") or 18)
        from rh_discovery import quote_of
        return quote_of(b_or_t.get("quote_symbol"))

    @classmethod
    def _qscale(cls, b_or_t: dict) -> float:
        return float(10 ** cls._quote_asset(b_or_t)[1])

    def _launch_rate(self, now: float) -> float:
        from rh_discovery import launch_rate_per_h
        return launch_rate_per_h((b.get("start") for b in self.state.rh_discovery.tracking.values()), now)

    def _paper_gas_usd(self) -> float:
        """Per-side gas the paper book charges — half the measured live round trip, else the fallback."""
        eng = getattr(self.state, "bankroll", None)
        rt = float((getattr(eng, "rh_fee_floor", None) or {}).get("gas_round_trip_usd") or 0.0)
        return rt / 2.0 if rt > 0 else RH_GAS_USD

    # ---------- re-entry watch (winners only; same knobs as the SOL watcher) ----------
    def _watch_after_exit(self, token: str, t: dict, price: float, b: dict | None, now: float):
        cfg = self.state.config
        prev = self.watch.get(token)
        n_lows = int(getattr(cfg, "hot_walk_lower_lows_n", 2))
        if (t.get("pnl_pct") or 0) <= 0:
            flushed = bool((t.get("dip_forensics") or {}).get("flush")) and t.get("exit_reason") in ("stop_loss", "trailing_stop")
            if prev and prev.get("hot"):
                if flushed:
                    # a single-seller flush is not the token failing — no strike, keep the hot watch armed
                    prev["last_exit_time"], prev["last_exit_was_sl"] = now, False
                    prev["exit_price_quote"] = price if price > 0 else prev.get("exit_price_quote")
                    prev["flush_exits"] = int(prev.get("flush_exits") or 0) + 1
                    logger.info(f"rh_paper HOT {t.get('symbol')} stopped by a flush — no strike, re-entry armed on recovery")
                    return
                # HOT: a losing leg is one lower-low strike, not the end — walk away after n in a row
                prev["strikes"] = int(prev.get("strikes") or 0) + 1
                prev["last_exit_time"], prev["last_exit_was_sl"] = now, True
                prev["exit_price_quote"] = price if price > 0 else prev.get("exit_price_quote")
                if prev["strikes"] >= n_lows:
                    self._drop_hot(token, prev, "losing_legs", now)
                else:
                    logger.info(f"rh_paper HOT {t.get('symbol')} losing leg — strike {prev['strikes']}/{n_lows}, still watching")
                return
            if flushed and getattr(cfg, "flush_reentry_enabled", True) and getattr(cfg, "reentry_enabled", True) and b and price > 0:
                # we sold a flush: watch for the recovery (breakout path — price back ≥ breakout% above our exit with buyers)
                self.stats["flush_reentry_watches"] = self.stats.get("flush_reentry_watches", 0) + 1
                self.watch[token] = {
                    "hot": False, "flush": True, "attempts": 0, "strikes": 0, "swings": None, "hot_since": None,
                    "last_exit_time": now, "last_exit_was_sl": False, "trough_after_peak": price, "trough_ts": now,
                    "mint": token, "chain": CHAIN, "name": t.get("name"), "symbol": t.get("symbol"),
                    "exit_price_quote": price, "exit_price_sol": 0.0, "quote_symbol": t.get("quote_symbol"), "exit_time": now,
                    "max_attempts": 1, "window_s": int(cfg.reentry_window_seconds), "pullback_pct": float(cfg.reentry_pullback_pct),
                    "size_multiplier": float(cfg.reentry_size_multiplier), "original_pnl_usd": t.get("pnl_usd") or 0.0,
                    "peak_price_after_exit": price, "creator": t.get("creator"),
                }
                logger.info(f"rh_paper {t.get('symbol')} stopped by a flush ({(t.get('dip_forensics') or {}).get('top_seller_share', 0)*100:.0f}% one seller) — recovery re-entry watch armed")
                return
            # A losing leg ends a normal watch — no chained retries off a stale peak.
            self.watch.pop(token, None)
            return
        if not getattr(cfg, "reentry_enabled", True) or price <= 0:
            return
        if not b:
            return                                        # v4-pool (graduated) buckets are watched too — priced from pool swaps
        hot = bool(prev and prev.get("hot")) or (t.get("pnl_pct") or 0) >= float(getattr(cfg, "hot_token_pnl_pct", 25.0))
        hot_mult = float(getattr(cfg, "hot_reentry_size_mult", 1.5)) if hot else 1.0
        if hot and not (prev and prev.get("hot")):
            self.stats["hot_tokens"] = self.stats.get("hot_tokens", 0) + 1
            logger.info(f"rh_paper HOT {t.get('symbol')} (+{t.get('pnl_pct'):.0f}%) — no attempt cap, ×{hot_mult:g} size; walk away when it goes stale")
        self.watch[token] = {
            "hot": hot,
            "attempts": max(int(prev.get("attempts") or 0) if prev else 0, self.reentry.attempts(token)),
            "strikes": 0,
            "swings": (prev or {}).get("swings") if hot else None,
            "hot_since": (prev or {}).get("hot_since") or now if hot else None,
            "last_exit_time": now, "last_exit_was_sl": False,
            "trough_after_peak": price, "trough_ts": now,
            "mint": token, "chain": CHAIN, "name": t.get("name"), "symbol": t.get("symbol"),
            "exit_price_quote": price, "exit_price_sol": 0.0, "quote_symbol": t.get("quote_symbol"),
            "exit_time": now,
            "max_attempts": None if hot else int(cfg.reentry_max_attempts),          # hot: uncapped
            "window_s": None if hot else int(cfg.reentry_window_seconds),            # hot: no clock
            "pullback_pct": float(cfg.reentry_pullback_pct),
            "size_multiplier": float(cfg.reentry_size_multiplier) * hot_mult, "original_pnl_usd": t.get("pnl_usd") or 0.0,
            "peak_price_after_exit": price, "creator": t.get("creator"),
        }

    def _drop_hot(self, token: str, w: dict, reason: str, now: float):
        self.watch.pop(token, None)
        self.stats["hot_walk_aways"] = self.stats.get("hot_walk_aways", 0) + 1
        self.hot_history.appendleft({"mint": token, "symbol": w.get("symbol"), "reason": reason, "ts": now,
                                     "attempts": int(w.get("attempts") or 0), "strikes": int(w.get("strikes") or 0),
                                     "played_s": round(now - float(w.get("hot_since") or w.get("exit_time") or now)),
                                     "original_pnl_usd": w.get("original_pnl_usd")})
        logger.warning(f"rh_paper HOT {w.get('symbol')} walk away — {reason} after {int(w.get('attempts') or 0)} re-entries")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        asyncio.create_task(hub.broadcast("rh_hot_dropped", {"mint": token, "symbol": w.get("symbol"), "reason": reason, "chain": CHAIN}))

    def hot_in_play(self) -> list[str]:
        """Symbols keeping the bot in FOCUS: hot watches + positions that are riding / up ≥ hot threshold."""
        cfg = self.state.config
        thr = float(getattr(cfg, "hot_token_pnl_pct", 25.0))
        out = [w.get("symbol") or m[:8] for m, w in self.watch.items() if w.get("hot")]
        for m, p in self.positions.items():
            t = p["trade"]
            up = ((p.get("_last_price") or 0) / t["entry_price_quote"] - 1.0) * 100.0 if t.get("entry_price_quote") else 0.0
            if p.get("_riding") or up >= thr:
                out.append(t.get("symbol") or m[:8])
        return out

    def focus_state(self, now: float) -> dict:
        cfg = self.state.config
        mode = str(getattr(cfg, "hot_focus_mode", "slow") or "slow")
        hot = self.hot_in_play()
        active = mode != "off" and bool(hot)
        cooldown = float(getattr(cfg, "hot_focus_fresh_cooldown_s", 90))
        left = max(0.0, cooldown - (now - self._last_fresh_entry_ts)) if active and mode == "slow" else 0.0
        return {"active": active, "mode": mode, "hot": hot, "cooldown_left_s": round(left),
                "reserved_slots": int(getattr(cfg, "hot_focus_reserve_slots", 1)) if active else 0}

    def _focus_blocks_fresh(self, now: float) -> str | None:
        f = self.focus_state(now)
        if not f["active"]:
            return None
        if f["mode"] == "pause":
            return "focus_pause"
        cfg = self.state.config
        if f["cooldown_left_s"] > 0:
            return "focus_cooldown"
        if self.counted_open() + len(self._pending_entries) >= int(cfg.rh_max_positions) - f["reserved_slots"]:
            return "focus_reserved_slot"
        return None

    def _scan_reentries(self, now: float):
        cfg = self.state.config
        bounce_confirm = float(getattr(cfg, "reentry_bounce_confirm_pct", 3.0))
        for token, w in list(self.watch.items()):
            hot = bool(w.get("hot"))
            if not hot and (now - w["exit_time"] > w["window_s"] or w["attempts"] >= w["max_attempts"]):
                self.watch.pop(token, None)
                continue
            b = self.state.rh_discovery.tracking.get(token)
            if not b or (b.get("graduated") and not b.get("pool_live")):
                if hot:
                    self._drop_hot(token, w, "no_pool" if b else "tracking_lost", now)
                else:
                    self.watch.pop(token, None)
                continue
            price = b["last_price_quote"]
            if price <= 0:
                continue
            if hot:
                prev_trough = w.get("trough_after_peak")
                update_watch_price(w, price)
                if w.get("trough_after_peak") != prev_trough:
                    w["trough_ts"] = now
                update_swings(w, price, bounce_confirm)
                reason = hot_walk_away_reason(w, b, price, now, cfg)
                if reason and token not in self.positions:
                    self._drop_hot(token, w, reason, now)
                    continue
            if token in self.positions or token in self._pending_entries:
                continue
            if self.reentry.check(token, cfg, now)[0] in ("sl-cooldown", "reentry-wait", "reentry-off"):
                continue                                  # the universal controls (SL cooldown first) hold the watch trigger too
            window_s = float(getattr(cfg, "exit_momentum_window_s", 10))
            n_buyers, inflow_q = recent_buyers_and_inflow(b["buy_events"], now, window_s)
            quote_usd = self._quote_usd(b["quote_symbol"])
            inflow_ok = inflow_q * quote_usd >= float(getattr(cfg, "rh_min_inflow_usd", 0) or 0) * 0.5
            trigger = decide_reentry(w, price, now, n_buyers, inflow_ok, cfg)
            if trigger is None:
                continue
            w["last_ctx"] = trigger_context(w, price, n_buyers, trigger)
            w["attempts"] += 1
            self.reentry.record_attempt(token)
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
        if b.get("graduated") and not b.get("pool_live"):
            return {"ok": False, "reason": "graduated — curve closed, v4 pool not live yet"}
        if token in self.positions or token in self._pending_entries:
            return {"ok": False, "reason": "already in an active position"}
        # manual holds live outside rh_max_positions (they never consume a scanner slot)
        if self._quote_usd(b["quote_symbol"]) <= 0:
            return {"ok": False, "reason": f"quote asset {b['quote_symbol']} has no USD price — can't size the paper stake"}
        if (b.get("last_price_quote") or 0) <= 0:
            return {"ok": False, "reason": "no curve price yet (waiting for first trade)"}
        self.entered.discard(token)
        self._pending_entries.add(token)
        await self._enter(token, manual=True)
        if token in self.positions:
            return {"ok": True, "mint": token, "symbol": b.get("symbol"), "mode": "live" if self.live_ok(b) else "paper", "chain": CHAIN}
        if token in self.pending_buys:
            return {"ok": True, "mint": token, "symbol": b.get("symbol"), "mode": "paper", "chain": CHAIN,
                    "queued": True, "fill_block": self.pending_buys[token]["fill_block"]}
        return {"ok": False, "reason": "paper entry did not open (unpriced quote or no price yet)"}

    async def _enter(self, token: str, size_mult: float = 1.0, reentry: str | None = None,
                     manual: bool = False, reentry_ctx: dict | None = None):
        cfg = self.state.config
        self._enter_inflight.add(token)
        try:
            await asyncio.sleep(max(0, cfg.paper_entry_latency_ms) / 1000.0)
            if not await self._fence(f"rh entry {token[:10]}"):
                b0 = self.state.rh_discovery.tracking.get(token)
                if b0:
                    self._block_entry(token, b0, "not-leader", time.time())
                return
            if await self._active_row_exists(token):
                b0 = self.state.rh_discovery.tracking.get(token)
                if b0:
                    self._block_entry(token, b0, "already-entered", time.time(), ttl_s=10.0)
                return                                    # cross-pod idempotency: one active row per token
            b = self.state.rh_discovery.tracking.get(token)
            ld = getattr(self.state, "live_doctor", None)
            if not manual and ld is not None and ld.book_benched("rh_pons", bool(getattr(cfg, "rh_live_trading", False))):
                logger.info(f"rh_paper skip {token[:10]}: rh_pons paused by live-doctor breaker")
                if b:
                    self._block_entry(token, b, "doctor-breaker", time.time())
                return
            if not b or token in self.positions or (b.get("graduated") and not b.get("pool_live")):
                return                                    # seasoned buckets trade on their live v4 pool; no pool → nothing to buy
            price = b["last_price_quote"]
            quote_usd = self._quote_usd(b["quote_symbol"])
            if price <= 0 or quote_usd <= 0:
                self._block_entry(token, b, "unpriced-quote" if quote_usd <= 0 else "no-price", time.time(), ttl_s=5.0)
                return
            now = time.time()
            if not manual and getattr(cfg, "creator_solvency_enabled", True) and b.get("creator"):
                import creator_solvency
                creator_eth = await creator_solvency.balance(b["creator"], lambda a: rh_wallet.balance_wei(a))
                creator_eth = creator_eth / 1e18 if creator_eth is not None else None
                b["creator_eth"] = creator_eth
                cs_reason = creator_solvency.gate(cfg, "rh", creator_eth, b)
                if cs_reason:
                    tally = self.stats.setdefault("skip_reasons", {})
                    tally[f"curve:{cs_reason}"] = tally.get(f"curve:{cs_reason}", 0) + 1
                    b["gate_reason"] = cs_reason
                    b["creator_gate"] = (cs_reason, now)   # _gates returns this until the balance cache expires: no re-entry spam
                    logger.info(f"rh_paper skip {b['symbol']}: {cs_reason} — deployer {b['creator'][:10]} eth={creator_eth} sold={b.get('creator_sold_pct')}%")
                    return
            if not manual and getattr(cfg, "creator_audit_enabled", False) and b.get("creator"):
                import creator_audit
                audit = await creator_audit.audit(cfg, self.state.db, chain="rh", creator=b["creator"], mint=token,
                                                  deploy_ts=float(b.get("start") or now), deploy_block=b.get("deploy_block"),
                                                  name=b.get("name"), symbol=b.get("symbol"))
                b["creator_audit"] = audit
                if audit["verdict"] != "pass":
                    logger.info(f"rh_paper skip {b['symbol']}: {audit['reason']} — deployer {b['creator'][:10]}")
                    self._block_entry(token, b, "creator-audit", now, ttl_s=creator_audit.CACHE_TTL_S, detail=audit["reason"])
                    return
            _gov = getattr(self.state, "bankroll", None)
            base_stake = float(getattr(cfg, "rh_max_trade_usd", 5.0))
            if base_stake <= 0:
                logger.info(f"rh_paper skip {b['symbol']}: RH stake is 0 — bankroll too small for the fee floor")
                self._block_entry(token, b, "stake-zero", now)
                return
            # R sizing inside the RH operator cap + cost gate against the first target (1R)
            import cost_gate as _cg
            import r_sizer as _rs
            from book_params import book_exit_view as _bev
            bx0 = _bev(cfg, "rh_pons")
            bank_usd = float(getattr(cfg, "paper_bankroll_usd", 1000.0) or 0)
            if _gov:
                try:
                    bank_usd, _ = await _gov.bankroll_usd("rh")
                except Exception:
                    pass
            depth_usd = float(b.get("net_quote") or 0) * quote_usd
            tol_bps = int(float(getattr(cfg, "rh_live_slippage_pct", 8.0)) * 100)
            exit_slip_pct = _cg.expected_slip_pct(base_stake, depth_usd, tol_bps)
            measured_rt = None
            if b.get("graduated"):
                # post-sweep: ask the pool itself (V4Quoter buy → sell at our size) — hook take + our impact, not a model
                measured_rt = await self._pool_round_trip_pct(token, b, base_stake / quote_usd)
                exit_slip_pct = measured_rt / 2.0
            sz = _rs.size_trade(bankroll_usd=bank_usd, risk_per_trade_pct=float(cfg.risk_per_trade_pct), sl_pct=bx0["stop_loss_pct"],
                                exit_slip_pct=exit_slip_pct, book_mult=max(0.0, float(getattr(cfg, "book_rh_size_mult", 1.0) or 1.0)) * max(0.1, float(size_mult)),
                                doctor_mult=(ld.book_adjust("rh_pons", bool(getattr(cfg, "rh_live_trading", False)))[0] if ld is not None else 1.0),
                                governor_mult=(_gov.size_mult("rh") if _gov else 1.0),
                                min_trade_usd=min(base_stake, float(getattr(cfg, "min_trade_usd", 0.5))),
                                max_trade_usd=min(base_stake, float(getattr(cfg, "rh_discovery_clip_usd", base_stake) or base_stake)))
            if sz["skip"]:
                logger.info(f"rh_paper skip {b['symbol']}: r-size — {sz['reason']}")
                self._block_entry(token, b, "r-size", now, detail=str(sz["reason"]))
                return
            first_r = float(bx0["target_r"]) if bx0["target_r"] else float(bx0["take_profit_pct"]) / sz["sl_pct_with_slip"]   # RH: TP% is the first cash-out
            q = _cg.quote(size_usd=sz["size_usd"], r_usd=sz["r_usd"], first_target_r=first_r, protocol="rh", entry_slip_bps=tol_bps,
                          exit_slip_bps=tol_bps, fee_usd_round_trip=2 * self._paper_gas_usd(), ladder=False, depth_usd=depth_usd,
                          measured_round_trip_pct=measured_rt)
            if not q["cost_gate_pass"]:
                self.stats["cost_gate_skips"] = self.stats.get("cost_gate_skips", 0) + 1
                logger.info(f"rh_paper skip {b['symbol']}: cost-gate — {q['cost_gate_reason']}" + (f" (pool round trip {measured_rt:.2f}%)" if measured_rt is not None else ""))
                self._block_entry(token, b, "cost-gate", now,
                                  detail=f"{q['cost_gate_reason']} · size ${sz['size_usd']:.2f} · slip {q['cost_breakdown']['slip_pct']:.1f}% fee {q['cost_breakdown']['fee_pct']:.1f}% proto {q['cost_breakdown']['protocol_pct']:.1f}%")
                return
            # Hardwired fast-fail sizing: half now, the other half after the first +5 % (see _fast_fail_add)
            stake_usd = max(float(getattr(cfg, "min_trade_usd", 1.5) or 0), sz["size_usd"] * 0.5)
            stake_quote = stake_usd / quote_usd
            ctx = {"reentry": reentry, "reentry_ctx": reentry_ctx, "manual": manual, "seasoned": bool(b.get("graduated")),
                   "ff_remaining_usd": max(0.0, sz["size_usd"] - stake_usd),
                   "plan": {"r_usd": sz["r_usd"], "r_usd_nominal": sz["r_usd_nominal"], "size_usd": sz["size_usd"], "size_clamped": sz["size_clamped"],
                            "sl_pct": bx0["stop_loss_pct"], "sl_pct_with_slip": sz["sl_pct_with_slip"], "target_r": bx0["target_r"] or None,
                            "expected_cost_pct": q["expected_cost_pct"], "expected_cost_usd": q["expected_cost_usd"],
                            "expected_target_pct": q["expected_target_pct"], "cost_gate_pass": True, "doctor_decision": "full",
                            "pool_round_trip_pct": measured_rt, "creator_eth": b.get("creator_eth"), "creator_sold_pct": b.get("creator_sold_pct")}}
            claim = getattr(self.state, "claim_entry_lock", None)
            if claim is not None and not await claim(token, CHAIN):
                logger.warning(f"rh_paper {token[:10]}: another pod holds the entry lock — aborted before send")
                self._block_entry(token, b, "entry-lock", now)
                return
            if self.live_ok(b):
                live_fill = await self._live_buy(token, b, stake_quote, price)
                if live_fill is None:
                    self._block_entry(token, b, "live-buy-failed", now)
                    return
                await self._open_position(token, b, price, stake_quote, quote_usd, now, live_fill, ctx)
                return
            rd = self.state.rh_discovery
            if float(rd.stats.get("head_advanced_ts") or 0.0) > 0 and not rd.alive():
                self._block_entry(token, b, "head-stalled", now, ttl_s=10.0)   # a paper fill lands on a later block: no head, no fill
                return
            # PAPER: a real buy would land `latency_blocks` after the chain head we last saw, at THAT block's
            # price — not at the (possibly seconds-stale) last polled price. Queue it and fill from the poll.
            head = int(self.state.rh_discovery.stats.get("head") or b.get("last_block") or 0)
            self.pending_buys[token] = {"decision_block": head, "fill_block": head + self.latency_blocks(),
                                        "decision_price": price, "stake_quote": stake_quote, "ts": now, "ctx": ctx}
            self._pending_entries.add(token)   # keeps the slot reserved until the fill resolves
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"rh_paper enter failed {token[:10]}: {e}")
        finally:
            self._enter_inflight.discard(token)
            # every early return above (fence, lock, gates, unpriced quote…) must release the reserved slot —
            # only a queued paper buy keeps it until resolve_pending_buys() settles it
            if token not in self.pending_buys:
                self._pending_entries.discard(token)

    PENDING_BUY_TTL_S = 120.0

    def expire_pending_buys(self, now: float) -> int:
        """A queued paper buy whose fill block never lands (feed off, head stalled, bucket gone) must not hold a
        max-positions slot forever."""
        n = 0
        for token, pb in list(self.pending_buys.items()):
            if now - float(pb.get("ts") or now) > self.PENDING_BUY_TTL_S:
                self.pending_buys.pop(token, None)
                self._pending_entries.discard(token)
                self.stats["entries_expired"] = self.stats.get("entries_expired", 0) + 1
                n += 1
                b = self.state.rh_discovery.tracking.get(token)
                if b:
                    self._block_entry(token, b, "fill-expired", now, ttl_s=30.0)
                logger.info(f"rh_paper pending buy EXPIRED {token[:10]}: fill block never landed within {self.PENDING_BUY_TTL_S:.0f}s")
        # a pending reservation with neither a position nor a queued buy behind it is a leak — release it
        for token in list(self._pending_entries):
            if token not in self.pending_buys and token not in self.positions and token not in getattr(self, "_enter_inflight", ()):
                if now - self._pending_since.setdefault(token, now) > 30.0:
                    self._pending_entries.discard(token)
                    self._pending_since.pop(token, None)
                    self.stats["slots_released"] = self.stats.get("slots_released", 0) + 1
                    logger.warning(f"rh_paper released a stale max-positions reservation for {token[:10]}")
        for token in list(self._pending_since):
            if token not in self._pending_entries:
                self._pending_since.pop(token, None)
        return n

    def resolve_pending_buys(self, head: int):
        """Fill queued paper buys once their fill block has landed, mirroring what a live
        buy would have done: reject if the curve graduated first or the price ran past the
        live slippage tolerance; otherwise fill at the fill block's price including our own impact."""
        cfg = self.state.config
        for token, pb in list(self.pending_buys.items()):
            if head < pb["fill_block"]:
                continue
            self.pending_buys.pop(token, None)
            b = self.state.rh_discovery.tracking.get(token)
            reason = None
            if not b or (b.get("graduated") and not pb["ctx"].get("seasoned")):
                reason = "graduated before fill"
            else:
                fill = pb["decision_price"]
                for blk, px in reversed(b.get("block_prices", ())):
                    if blk <= pb["fill_block"] and px > 0:
                        fill = px
                        break
                tol = float(getattr(cfg, "rh_live_slippage_pct", 8.0)) / 100.0
                if fill > pb["decision_price"] * (1.0 + tol):
                    reason = f"price ran +{(fill / pb['decision_price'] - 1) * 100:.0f}% before fill (> {tol * 100:.0f}% slippage)"
            if reason:
                self._pending_entries.discard(token)
                self.stats["entries_rejected"] = self.stats.get("entries_rejected", 0) + 1
                if b:
                    self._block_entry(token, b, "fill-rejected", time.time(), ttl_s=0.0)   # label only — the next scan may re-decide at the new price
                logger.info(f"rh_paper entry REJECTED {b['symbol'] if b else token[:10]}: {reason}")
                continue
            asyncio.create_task(self._open_position(token, b, fill, pb["stake_quote"], self._quote_usd(b["quote_symbol"]),
                                                    time.time(), None, pb["ctx"], fill_block=pb["fill_block"],
                                                    decision_price=pb["decision_price"]))

    async def _open_position(self, token: str, b: dict, price: float, stake_quote: float, quote_usd: float,
                             now: float, live_fill: dict | None, ctx: dict, fill_block: int | None = None,
                             decision_price: float | None = None):
        try:
            if token in self.positions:
                return
            fee = fee_fraction(now - b["start"])
            reentry, reentry_ctx, manual = ctx.get("reentry"), ctx.get("reentry_ctx"), ctx.get("manual")
            if live_fill:
                mode = "live"
                stake_quote = live_fill["quote_wei"] / self._qscale(b)
                tokens = live_fill["tokens_raw"] / 1e18
                price = stake_quote / tokens if tokens > 0 else price
            else:
                mode = "paper"
                k0 = b.get("curve_k0")
                if b.get("graduated"):
                    # pool fill: the hook take, not the curve fee; the round trip was priced from the quoter at decision time
                    fee = POOL_FEE_FRACTION
                    tokens = stake_quote * (1.0 - fee) / price
                elif k0 and b.get("curve_a"):
                    # exact curve at the fill price: our own buy moves the price too
                    a = (price * k0) ** 0.5
                    a2 = a + stake_quote * (1.0 - fee)
                    tokens = k0 / a - k0 / a2
                    price = stake_quote * (1.0 - fee) / tokens if tokens > 0 else price
                else:
                    tokens = stake_quote * (1.0 - fee) / price
            if tokens <= 0:
                return
            stake_usd = stake_quote * quote_usd
            trade = Trade(
                mint=token, creator=b["creator"], name=b["name"], symbol=b["symbol"],
                mode=mode, entry_usd=stake_usd, entry_tokens=tokens, book="rh_pons",
                **((ctx or {}).get("plan") or {}),
                protocol=PROTOCOL, classifier_action=ENTRY_ACTION, risk_score=50,
                chain=CHAIN, quote_symbol=b["quote_symbol"], entry_quote=stake_quote,
                entry_price_quote=price,
                fees_usd=(live_fill["fee_wei"] / self._qscale(b) * quote_usd + live_fill["gas_cost_wei"] / 1e18 * self._quote_usd("ETH")) if live_fill
                         else stake_quote * fee * quote_usd + self._paper_gas_usd(),
            )
            doc = trade.model_dump()
            doc["pair_token"], doc["quote_decimals"] = self._quote_asset(b)
            if b.get("graduated"):
                doc["venue"], doc["graduated_during_hold"] = "pool", False   # entered post-sweep: priced & exited on the v4 pool
            first = float(b.get("first_price_quote") or 0.0)
            doc["ff_remaining_usd"] = float((ctx or {}).get("ff_remaining_usd") or 0.0)
            doc["entry_ctx"] = {
                "growth_pct": ((price / first) - 1.0) * 100.0 if first > 0 else None,
                "inflow_usd": float(b.get("net_quote") or 0.0) * quote_usd,
                "unique_buyers": len(b.get("buyers") or ()),
                "curve_fill_pct": float(b.get("curve_fill_pct") or 0.0),
                "mc_usd": float(b.get("usd_market_cap") or 0.0),
                "age_s": now - float(b.get("start") or now),
                "quote_symbol": b.get("quote_symbol"),
                "launch_rate_per_h": self._launch_rate(now),
            }
            if live_fill:
                doc.update({"entry_sig": live_fill["tx"], "entry_tokens_raw": str(live_fill["tokens_raw"]), "curve": b.get("curve"),
                            "entry_gas_usd": live_fill["gas_cost_wei"] / 1e18 * self._quote_usd("ETH"),
                            "entry_latency_s": live_fill["latency_s"], "entry_block": live_fill["block"]})
            else:
                doc.update({"entry_block": fill_block, "entry_decision_price_quote": decision_price})
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
            try:
                await self.state.db.trades.update_one({"_id": trade.id}, {"$set": {**doc, "_id": trade.id}}, upsert=True)
            except DuplicateKeyError:
                logger.critical(f"rh_paper {token[:10]}: duplicate active row across pods — fill parked as exit_failed_terminal")
                doc.update(status="exit_failed_terminal", venue_stage="duplicate_fill", held_intent="exit",
                           held_exit_reason="duplicate active row across pods", exit_reason="duplicate active row — second pod filled the same token")
                await self.state.db.trades.update_one({"_id": trade.id}, {"$set": {**doc, "_id": trade.id}}, upsert=True)
                return
            try:
                mr = getattr(self.state, "market_regime", None)
                doc["regime_at_entry"] = mr()["regime"] if mr else None
                doc["expected_price"], doc["fill_price"] = float(b.get("last_price_quote") or price), float(price)
                doc["slippage_pct"] = round((float(price) / float(b.get("last_price_quote") or price) - 1) * 100, 4) if b.get("last_price_quote") else None
                doc["latency_ms"] = int(max(0, self.state.config.paper_entry_latency_ms))
                await self.state.db.trades.update_one({"_id": trade.id}, {"$set": {k: doc[k] for k in ("regime_at_entry", "expected_price", "fill_price", "slippage_pct", "latency_ms")}})
            except Exception:
                pass
            self.positions[token] = {"trade": doc, "peak_price": price, "_last_price": price, "opened": now}
            self.entered.add(token)
            if reentry == "gates":
                self.reentry.record_attempt(token)        # watch-triggered legs are counted by _scan_reentries
                if token in self.watch:
                    self.watch[token]["attempts"] += 1
            self.stats["entries"] += 1
            launch_update = {"entered": True, "entry_action": ENTRY_ACTION}
            await self.state.db.launches.update_one({"_id": b["launch_id"]}, {"$set": launch_update})
            await hub.broadcast("launch_update", {"id": b["launch_id"], "mint": token, **launch_update})
            await hub.broadcast("trade_enter", {**doc, "entry_time": _iso(doc["entry_time"])})
            logger.info(f"rh_paper ENTER {b['symbol']} {token[:10]} ${stake_usd:.2f} @ {price:.3e} {b['quote_symbol']} fee={fee*100:.2f}% [{mode}]")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"rh_paper open failed {token[:10]}: {e}")
        finally:
            self._pending_entries.discard(token)

    # ---------- live execution ----------
    def live_ok(self, b: dict) -> bool:
        cfg = self.state.config
        if not getattr(cfg, "rh_live_trading", False) or self.live_kill_tripped:
            return False
        sym = b.get("quote_symbol")
        if sym == "ETH":
            return True
        # ERC-20 quotes (USDG, tokenized stocks): opt-in, and only when the quote is known and USD-priced
        return bool(getattr(cfg, "rh_live_erc20_quotes", False)) and sym not in (None, "?") \
            and not rh_dex.is_native(b.get("pair_token")) and self._quote_usd(sym) > 0

    async def _pool_round_trip_pct(self, token: str, b: dict, stake_quote: float) -> float:
        """Real friction of a pool round trip at our size from the V4Quoter; falls back to the hook-take model."""
        pair, dec = self._quote_asset(b)
        try:
            rt = await asyncio.wait_for(rh_dex.round_trip(token, max(1, int(stake_quote * 10 ** dec)), pair), timeout=4.0)
            self.stats["pool_quotes"] = self.stats.get("pool_quotes", 0) + 1
            b["pool_round_trip_pct"] = rt["cost_pct"]
            return float(rt["cost_pct"])
        except Exception as e:
            self.stats["pool_quote_failures"] = self.stats.get("pool_quote_failures", 0) + 1
            logger.debug(f"rh_dex round-trip quote failed {token[:10]}: {e}")
            import cost_gate as _cg
            return 2 * POOL_FEE_FRACTION * 100.0 + _cg.ADVERSE_FILL_PCT

    async def live_pnl_today_usd(self) -> float:
        since = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        total = 0.0
        async for d in self.state.db.trades.find({"chain": CHAIN, "mode": "live", "status": "closed",
                                                  "exit_time": {"$gte": since}}, {"_id": 0, "pnl_usd": 1}):
            total += float(d.get("pnl_usd") or 0.0)
        return total

    def _maybe_fast_fail_add(self, token: str, pos: dict, b: dict, price: float, now: float) -> None:
        t = pos["trade"]
        rem = float(t.get("ff_remaining_usd") or 0)
        entry = float(t.get("entry_price_quote") or 0)
        if rem <= 0 or entry <= 0 or price <= 0 or pos.get("_ff_adding") or pos.get("_exiting") \
                or (price - entry) / entry * 100.0 < FAST_FAIL_ADD_AT_PCT:
            return
        pos["_ff_adding"] = True
        asyncio.create_task(self._fast_fail_add(token, pos, b, price, rem, now))

    async def _fast_fail_add(self, token: str, pos: dict, b: dict, price: float, usd: float, now: float) -> None:
        """Leg 2 of the hardwired fast-fail sizing: buy the other half once the position first prints +5 %."""
        t = pos["trade"]
        try:
            quote_usd = self._quote_usd(b["quote_symbol"])
            if quote_usd <= 0:
                return
            stake_quote = usd / quote_usd
            fee = POOL_FEE_FRACTION if t.get("venue") == "pool" else fee_fraction(now - b["start"])
            tokens = stake_quote * (1.0 - fee) / price
            live_fill = None
            if t.get("mode") == "live":
                live_fill = await self._live_buy(token, b, stake_quote, price)
                if not live_fill:
                    return
                stake_quote = live_fill["quote_wei"] / self._qscale(b)
                tokens = live_fill["tokens_raw"] / 1e18
                price = stake_quote / tokens if tokens > 0 else price
            old_tokens = float(t.get("entry_tokens") or 0)
            old_quote = float(t.get("entry_price_quote") or 0) * old_tokens
            t["entry_tokens"] = old_tokens + tokens
            t["entry_price_quote"] = (old_quote + stake_quote) / (old_tokens + tokens)
            t["entry_usd"] = float(t.get("entry_usd") or 0) + stake_quote * quote_usd
            t["ff_remaining_usd"] = 0.0
            t["fast_fail_add"] = {"at": now, "price_quote": price, "usd": stake_quote * quote_usd, "tx": (live_fill or {}).get("tx_hash")}
            await self.state.db.trades.update_one({"_id": t["id"]}, {"$set": {k: t[k] for k in ("entry_tokens", "entry_price_quote", "entry_usd", "ff_remaining_usd", "fast_fail_add")}})
            logger.info(f"rh_paper FAST-FAIL ADD {b.get('symbol')} +${stake_quote * quote_usd:.2f} at {price:.3e} → full size ${t['entry_usd']:.2f}")
        except Exception as e:
            logger.warning(f"rh fast-fail add failed for {b.get('symbol')} (stays at half size): {e}")
        finally:
            pos.pop("_ff_adding", None)

    async def _live_buy(self, token: str, b: dict, stake_quote: float, price: float) -> dict | None:
        cfg = self.state.config
        try:
            pair, dec = self._quote_asset(b)
            native = rh_dex.is_native(pair)
            bal = await rh_wallet.balance_wei()
            reserve = int(float(cfg.rh_gas_reserve_eth) * 1e18)
            quote_raw = int(stake_quote * 10 ** dec)
            if native and bal - quote_raw < reserve:
                logger.warning(f"rh_live skip {b.get('symbol')}: balance {bal / 1e18:.5f} ETH < stake {stake_quote:.5f} + reserve")
                return None
            if not native:
                # ERC-20 quote: the wallet must already hold it (no ETH→quote conversion here) and keep ETH for gas
                held = await rh_wallet.erc20_balance(pair)
                if held < quote_raw or bal < reserve:
                    self.stats["quote_balance_skips"] = self.stats.get("quote_balance_skips", 0) + 1
                    self.last_live_error = (f"buy {b.get('symbol')}: wallet holds {held / 10 ** dec:.4f} {b.get('quote_symbol')} "
                                            f"< stake {stake_quote:.4f} (or ETH below gas reserve)")
                    logger.warning(f"rh_live skip {self.last_live_error}")
                    return None
            if b.get("graduated"):
                # post-sweep: the curve is gone — buy on the v4 pool (quote → token; ERC-20 quotes go through Permit2)
                fill = await rh_dex.buy(token, quote_raw, float(cfg.rh_live_slippage_pct), quote=pair)
            else:
                fill = await rh_live.buy(b["curve"], quote_raw, price, float(cfg.rh_live_slippage_pct),
                                         quote_token=None if native else pair, quote_decimals=dec)
            self.stats["live_buys"] = self.stats.get("live_buys", 0) + 1
            logger.warning(f"rh_live BUY {b.get('symbol')} {token[:10]} {fill['quote_wei'] / 10 ** dec:.5f} {b.get('quote_symbol')} → {fill['tokens_raw'] / 1e18:,.0f} tokens tx={fill['tx'][:12]} ({fill['latency_s']}s)")
            return fill
        except Exception as e:
            self.stats["live_errors"] = self.stats.get("live_errors", 0) + 1
            self.last_live_error = f"buy {b.get('symbol')}: {e}"
            logger.error(f"rh_live buy failed {token[:10]}: {e}")
            return None

    async def _live_sell(self, token: str, t: dict, price: float) -> dict | None:
        cfg = self.state.config
        raw = int(t.get("entry_tokens_raw") or int(float(t["entry_tokens"]) * 1e18))
        b = self.state.rh_discovery.tracking.get(token) or {}
        curve = t.get("curve") or b.get("curve") or token
        pool = bool(b.get("graduated")) or t.get("venue") == "pool"
        pair, dec = self._quote_asset(b if b.get("pair_token") else t)
        try:
            on_chain = await rh_wallet.erc20_balance(token)
            if on_chain == 0:
                # nothing left to sell — the sell may have landed without being booked (restart mid-exit)
                fill = await (rh_dex.recover_sell(token, int(t.get("entry_block") or 0), pair, dec) if pool
                              else rh_live.recover_sell(curve, int(t.get("entry_block") or 0)))
                if fill:
                    self.stats["live_sells"] = self.stats.get("live_sells", 0) + 1
                    return fill
                self.last_live_error = f"sell {t.get('symbol')}: wallet holds 0 tokens and no on-chain sell found"
                logger.error(f"rh_live sell {token[:10]}: {self.last_live_error}")
                return None
            if on_chain < raw:
                raw = on_chain
        except Exception as e:
            logger.warning(f"rh_live balance check failed {token[:10]}: {e}")
        for attempt in range(3):
            try:
                slip = float(cfg.rh_live_slippage_pct) * (attempt + 1)
                if pool:
                    fill = await rh_dex.sell(token, raw, slip, quote=pair)
                else:
                    fill = await rh_live.sell(curve, raw, price, slip, token=token)
                self.stats["live_sells"] = self.stats.get("live_sells", 0) + 1
                logger.warning(f"rh_live SELL {t.get('symbol')} {token[:10]} {fill['quote_wei'] / 10 ** dec:.5f} {t.get('quote_symbol') or 'ETH'} tx={fill['tx'][:12]} "
                               f"({fill['latency_s']}s){' [pool]' if pool else ''}")
                return fill
            except Exception as e:
                self.last_live_error = f"sell {t.get('symbol')}: {e}"
                logger.error(f"rh_live sell attempt {attempt + 1} failed {token[:10]}: {e}")
                if not pool and attempt == 0:
                    # the curve may have been swept between our poll and this tx — pool initialised ⇒ graduated
                    try:
                        if await rh_dex.spot_price(token, pair, dec) > 0:
                            pool = True
                            b["graduated"] = True
                            t["venue"] = "pool"
                            logger.warning(f"rh_live {t.get('symbol')} curve closed — rerouting sell to the v4 pool")
                            continue
                    except Exception as e2:
                        logger.debug(f"rh_dex pool probe failed {token[:10]}: {e2}")
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
            await self.state.save_config(include_switches=True)      # safety switch flip must persist
            logger.critical(f"RH LIVE KILL SWITCH: today's live RH P/L {pnl:+.2f} $ ≤ -{cfg.rh_daily_kill_switch_usd:g} — rh_live_trading OFF")
            await hub.broadcast("rh_live_kill", {"pnl_today_usd": pnl, "limit": cfg.rh_daily_kill_switch_usd})

    # ---------- exits ----------
    def _switch_to_pool(self, pos: dict, b: dict, now: float):
        """The curve was swept into the v4 pool while we hold: keep the ladder running on pool prices."""
        t = pos["trade"]
        t["venue"] = "pool"
        t["graduated_during_hold"] = True
        # a curve-priced exit in flight is void: the venue changed under it — gates re-evaluate on pool prices next tick
        if pos.pop("exit_trigger", None) is not None:
            self.stats["grad_triggers_dropped"] = self.stats.get("grad_triggers_dropped", 0) + 1
        pos.pop("_fill_task", None)
        # hot sweep → ride it: R-based trail from the post-sweep peak, stop ratchets to breakeven+costs, no fixed TP, no clock
        t["r_trail"] = bool(getattr(self.state.config, "rh_grad_handoff_r_trail", True)) and not is_manual_hold(t)
        if t["r_trail"]:
            pos["_r_trail_peak"] = pos.get("_last_price") or 0.0
            self.stats["grad_handoffs"] = self.stats.get("grad_handoffs", 0) + 1
        t["graduated_at_pnl_pct"] = round(((pos["_last_price"] or 0) / t["entry_price_quote"] - 1.0) * 100.0, 2) if t.get("entry_price_quote") else None
        t["graduated_hold_s"] = round(now - pos["opened"], 1)
        pos["pool_since"] = now
        self.stats["graduated_holds"] = self.stats.get("graduated_holds", 0) + 1
        logger.warning(f"rh_paper {t.get('symbol')} GRADUATED while held ({t['graduated_at_pnl_pct']:+.1f}%) — riding on the v4 pool"
                       + (" · R-trail handoff: no fixed TP, giveback trail in R" if t.get("r_trail") else ""))
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # sync caller (tests) — nothing to persist from here
        async def _persist():   # Motor returns a Future, not a coroutine — create_task(update_one(...)) raises TypeError
            try:
                await self.state.db.trades.update_one({"_id": t["id"]}, {"$set": {
                    "venue": "pool", "graduated_during_hold": True, "graduated_at_pnl_pct": t["graduated_at_pnl_pct"],
                    "graduated_hold_s": t["graduated_hold_s"], "r_trail": t["r_trail"]}})
                await hub.broadcast("trade_update", {"id": t["id"], "mint": t["mint"], "chain": CHAIN, "venue": "pool",
                                                     "graduated_at_pnl_pct": t["graduated_at_pnl_pct"], "r_trail": t["r_trail"]})
            except Exception as e:
                logger.debug(f"rh_paper venue persist failed {t.get('mint', '')[:10]}: {e}")
        asyncio.create_task(_persist())

    async def _paper_pool_proceeds(self, token: str, t: dict, price: float) -> tuple[float, float]:
        """Paper fill on the pool: the real V4Quoter output for our size, else spot minus the hook take."""
        tokens = float(t.get("entry_tokens") or 0.0)
        fee = POOL_FEE_FRACTION
        pair, dec = self._quote_asset(t)
        try:
            out = await asyncio.wait_for(rh_dex.quote_sell(token, int(tokens * 1e18), pair), timeout=4.0)
            if out > 0:
                return out / 10 ** dec, fee
        except Exception as e:
            logger.debug(f"rh_dex paper quote failed {token[:10]}: {e}")
        return tokens * price * (1.0 - fee), fee

    def _decide_exit(self, pos: dict, b: dict, now: float, price_override: float | None = None) -> str | None:
        cfg = self.state.config
        t = pos["trade"]
        price = price_override or b["last_price_quote"] or pos["_last_price"]
        pos["_last_price"] = price
        entry = t["entry_price_quote"] or 0.0
        if entry <= 0 or price <= 0:
            return None
        if price > pos["peak_price"]:
            pos["peak_price"] = price
        if t.get("long_term_hold"):
            return None                                        # LTH: operator exit only
        if price > pos["peak_price"]:
            pos["peak_price"] = price
            pos["peak_ts"] = now
        from exits import search_dead_tape
        dead = search_dead_tape(cfg, "rh_pons", b, now, entry_ts=pos.get("opened"), trade=t)
        if dead is not None:
            return dead.reason
        if price < pos.get("trough_price", entry):
            pos["trough_price"] = price
            pos["trough_ts"] = now
        from book_params import book_exit_view, trade_regime
        bx = book_exit_view(cfg, "rh_pons", trade_regime(cfg, "rh_pons", t))   # RH's own ladder, per entry regime
        if bx.get("target_r"):
            bx["take_profit_pct"] = bx["target_r"] * float(t.get("sl_pct_with_slip") or bx["stop_loss_pct"])   # R target when set
        pnl_pct = (price - entry) / entry * 100.0
        peak_pct = (pos["peak_price"] - entry) / entry * 100.0
        dd_from_peak = (pos["peak_price"] - price) / pos["peak_price"] * 100.0 if pos["peak_price"] > 0 else 0.0
        if b.get("graduated") and t.get("venue") != "pool":
            self._switch_to_pool(pos, b, now)
        # Buy-momentum gate (buyers-only on RH — quote assets differ): defer
        # SL/TP while >= exit_momentum_min_buyers distinct wallets bought in
        # the window, bounded by max_defer_s and the hard SL floor.
        def _mom_holds(kind: str) -> bool:
            if not getattr(cfg, "exit_momentum_gate_enabled", True):
                return False
            key = f"_mom_defer_{kind}"
            if kind == "sl":
                # bounded deferral: never ride more than X points past the SL line
                if pnl_pct <= -(bx["stop_loss_pct"] + float(getattr(cfg, "exit_momentum_max_extra_loss_pct", 5.0))):
                    pos["_mom_defer_bounded"] = True
                    return False
            cutoff = now - float(getattr(cfg, "exit_momentum_window_s", 10))
            buyers = {w for ts, _q, w in b.get("buy_events", ()) if ts >= cutoff}
            buy_q = sum(q for ts, q, _w in b.get("buy_events", ()) if ts >= cutoff)
            sell_q = sum(q for ts, q, _w in b.get("sell_events", ()) if ts >= cutoff)
            # "momentum" means buyers AND net inflow — a dump with a few bot
            # buys sprinkled in must not hold an SL open
            fr = flow.flow_ratio_pct(b, now, float(getattr(cfg, "exit_momentum_window_s", 10)), sol=False)
            weak = (fr < float(getattr(cfg, "exit_momentum_min_flow_pct", 1.0))) if fr is not None else \
                (len(buyers) < int(getattr(cfg, "exit_momentum_min_buyers", 3)) or buy_q <= sell_q)
            if fr is not None:
                pos["_mom_flow_pct"] = fr
            if weak:
                pos.pop(key, None)
                return False
            started = pos.setdefault(key, now)
            if started == now:
                pos.setdefault("_mom_defer_log", []).append({"kind": kind, "at_pnl_pct": round(pnl_pct, 2), "ts": now})
            return now - started < float(getattr(cfg, "exit_momentum_max_defer_s", 20))
        if t.get("r_trail"):
            return self._decide_r_trail(pos, b, now, t, bx, price, entry, pnl_pct, peak_pct, dd_from_peak, _mom_holds)
        if pnl_pct >= bx["take_profit_pct"] and _mom_holds("tp"):
            return None
        if pnl_pct <= -bx["stop_loss_pct"] and _mom_holds("sl"):
            return None
        if (
            cfg.no_momentum_exit_enabled
            and not (b.get("pinned") or is_manual_hold(t))   # operator pin / manual buy = long hold: never a momentum kill
            and not pos.get("_nm_checked")
            and now - pos["opened"] >= cfg.no_momentum_after_s
        ):
            pos["_nm_checked"] = True
            if peak_pct < cfg.no_momentum_min_mfe_pct:
                from exits import is_recovering, price_ago, start_recovery_watch
                trough = float(pos.get("trough_price") or price)
                new_buyers = len({w for ts, _q, w in (b.get("buy_events") or ()) if now - ts <= 30.0})
                if (getattr(cfg, "recovery_watch_enabled", True) and pnl_pct < 0
                        and is_recovering(now, price, trough, pos.get("trough_ts"), price_ago(b.get("price_samples"), now, 30.0), new_buyers,
                                          net_flow=flow.net_flow(b, now, 30.0, sol=False)[0])):
                    pos["_recovery_watch"] = start_recovery_watch(cfg, now, entry, price, trough)
                    w = pos["_recovery_watch"]
                    logger.info(f"rh_paper RECOVERY WATCH {t.get('symbol')} {pnl_pct:+.1f}% recovering (peak {peak_pct:+.1f}%): stop {w['stop']:.3e}, "
                                f"reclaim {w['target']:.3e}, {int(w['deadline'] - now)}s — no-momentum kill deferred")
                else:
                    return "no_momentum"
        w = pos.get("_recovery_watch")
        if w:
            from exits import recovery_watch_step
            verdict, why = recovery_watch_step(w, now, price)
            if verdict == "exit":
                pos.pop("_recovery_watch", None)
                t["recovery_watch"] = "failed"
                return why
            if verdict == "reclaimed":
                pos.pop("_recovery_watch", None)
                t["recovery_watch"] = "recovered"
                pos["opened"] = now                 # fresh clock: the recovery is a new leg, not the dead probe it replaced
                logger.info(f"rh_paper RECOVERY WATCH {t.get('symbol')} reclaimed {price:.3e} — back on the ladder, clock restarted")
            elif pnl_pct > -bx["stop_loss_pct"]:
                return None                         # holding; the hard SL below still applies
        if pnl_pct >= bx["take_profit_pct"]:
            return "take_profit"
        if pnl_pct <= -bx["stop_loss_pct"]:
            return None if self._flush_holds(pos, b, now, "sl", pnl_pct, bx) else "stop_loss"
        from exits import ratchet_trail, RATCHET_TIERS
        trail = ratchet_trail(bx["trailing_stop_pct"], peak_pct)
        arm = min(bx["trailing_arm_pct"], RATCHET_TIERS[0][0]) if trail > 0 else bx["trailing_arm_pct"]
        if peak_pct >= arm and trail > 0 and dd_from_peak >= trail:
            return None if self._flush_holds(pos, b, now, "trail", pnl_pct, bx) else "trailing_stop"
        held = now - pos["opened"]
        if bx["hold_max_seconds"] > 0 and held >= bx["hold_max_seconds"] and not is_manual_hold(t):
            return "max_hold"
        return None

    def _decide_r_trail(self, pos, b, now, t, bx, price, entry, pnl_pct, peak_pct, dd_from_peak, _mom_holds) -> str | None:
        """Post-graduation ride (operator rule): no fixed TP, no clock. 1R = the trade's SL% (with slip).
        Stop: the entry SL until the peak clears +1R, then breakeven + exit costs (never back below). Trail: giveback
        from the post-sweep peak ≥ rh_grad_trail_r × 1R (floored at the book's trailing_stop_pct). Flush holds apply."""
        cfg = self.state.config
        one_r = float(t.get("sl_pct_with_slip") or bx["stop_loss_pct"] or 10.0)
        trail_pct = max(float(bx.get("trailing_stop_pct") or 0.0), float(getattr(cfg, "rh_grad_trail_r", 1.0) or 1.0) * one_r)
        peak = max(float(pos.get("_r_trail_peak") or 0.0), price)
        pos["_r_trail_peak"] = peak
        ride_peak_pct = (peak - entry) / entry * 100.0
        drop = (peak - price) / peak * 100.0 if peak > 0 else 0.0
        stop_pct = -bx["stop_loss_pct"]
        if ride_peak_pct >= one_r:
            stop_pct = max(stop_pct, float(t.get("expected_cost_pct") or 0.0))          # breakeven + what it costs to get out
        pos["_r_trail_stop_pct"], pos["_r_trail_trail_pct"] = round(stop_pct, 2), round(trail_pct, 2)
        if pnl_pct <= stop_pct:
            if stop_pct < 0 and _mom_holds("sl"):
                return None
            return None if self._flush_holds(pos, b, now, "sl", pnl_pct, bx) else ("stop_loss" if stop_pct < 0 else "r_trail_stop")
        if ride_peak_pct >= one_r and drop >= trail_pct:
            return None if self._flush_holds(pos, b, now, "trail", pnl_pct, bx) else "r_trail"
        return None

    def _flush_holds(self, pos: dict, b: dict, now: float, kind: str, pnl_pct: float, bx: dict) -> bool:
        """Hold an SL/trail exit while the dip looks like a single-seller flush (not distribution):
        bounded by flush_hold_s and an extra-drop floor below the trough seen when the hold started."""
        cfg = self.state.config
        t = pos["trade"]
        if not getattr(cfg, "flush_hold_enabled", True):
            return False
        token = t.get("mint") or ""
        in_scope = (str(getattr(cfg, "flush_hold_scope", "hot_reentry")) == "all" or bool(t.get("reentry"))
                    or bool((self.watch.get(token) or {}).get("hot")) or pos.get("_riding")
                    or (t.get("symbol") in self.hot_in_play()))
        if not in_scope:
            return False
        f = dip_forensics(b, pos, now, float(getattr(cfg, "flush_window_s", 30)))
        pos["_dip_forensics"] = {**f, "flush": is_flush(f, cfg), "kind": kind}
        if not pos["_dip_forensics"]["flush"]:
            return False
        since = pos.get("_flush_hold_since")
        if since is None:
            pos["_flush_hold_since"] = now
            pos["_flush_hold_trough"] = pos.get("trough_price") or pos["_last_price"]
            pos["_flush_hold_at_pnl_pct"] = round(pnl_pct, 2)
            self.stats["flush_holds"] = self.stats.get("flush_holds", 0) + 1
            logger.warning(f"rh_paper FLUSH? {t.get('symbol')} {kind} at {pnl_pct:+.1f}%: {f['n_sellers']} seller(s), top {f['top_seller_share']*100:.0f}% "
                           f"of {f['sold_quote']:.4f} sold, {f['buyers']} buyers still in — holding up to {getattr(cfg, 'flush_hold_s', 10)}s")
            if f.get("top_seller"):
                try:
                    asyncio.get_running_loop()
                    asyncio.create_task(self._probe_seller(pos, token, f["top_seller"], f["top_seller_quote"]))
                except RuntimeError:
                    pass
            since = now
        floor = float(pos.get("_flush_hold_trough") or 0) * (1.0 - flow.flush_floor_pct(cfg, b.get("price_samples"), now) / 100.0)
        if pos["_last_price"] <= floor:
            pos["_dip_forensics"]["hold_broke_floor"] = True
            return False                      # it kept falling — that was distribution after all
        return now - since < float(getattr(cfg, "flush_hold_s", 10))

    async def _probe_seller(self, pos: dict, token: str, seller: str, sold_quote: float):
        """Did the flusher empty their bag? remaining tokens (at the current price) vs what they just sold."""
        try:
            left = await rh_wallet.erc20_balance(token, owner=seller)
            price = float(pos.get("_last_price") or 0)
            left_quote = left / 1e18 * price
            pos.setdefault("_dip_forensics", {}).update({"seller_left_tokens": left / 1e18, "seller_left_quote": round(left_quote, 6),
                                                          "seller_sold_all": bool(sold_quote > 0 and left_quote < 0.1 * sold_quote)})
        except Exception as e:
            logger.debug(f"flush seller probe failed {token[:10]}: {e}")

    async def _fence(self, what: str) -> bool:
        fence = getattr(self.state, "leader_fence", None)
        return True if fence is None else await fence(what)

    async def _active_row_exists(self, token: str) -> bool:
        try:
            return bool(await self.state.db.trades.find_one({"mint": token, "chain": CHAIN, "status": "active"}, {"_id": 1}))
        except Exception:
            return False

    async def _rehydrate_from_pool(self, token: str, pos: dict, now: float) -> bool:
        """A held position whose bucket is gone (restart): if the v4 pool prices it, rebuild the bucket on the pool
        and keep riding — a lost bucket is not an exit. False when there is no pool (or the read failed)."""
        if now < pos.get("_rehydrate_next", 0.0):
            return False
        pos["_rehydrate_next"] = now + REHYDRATE_RETRY_S
        t = pos["trade"]
        pair, dec = self._quote_asset(t)
        try:
            spot = await asyncio.wait_for(rh_dex.spot_price(token, pair, dec), timeout=6.0)
        except Exception as e:
            logger.debug(f"rh rehydrate probe failed {token[:10]}: {e}")
            return False
        if not spot or spot <= 0:
            return False
        disc = self.state.rh_discovery
        b = disc._new_bucket({"token": token, "curve": t.get("curve") or token, "deployer": t.get("creator") or "0x" + "0" * 40,
                              "pair_token": pair, "quote_symbol": t.get("quote_symbol") or "ETH", "quote_decimals": dec,
                              "graduation_threshold": 0.0, "block": int(t.get("entry_block") or 0)}, start=pos.get("opened") or now)
        b.update(symbol=t.get("symbol"), name=t.get("name"), graduated=True, graduated_at=now, curve_fill_pct=100.0,
                 first_price_quote=spot, last_price_quote=spot, rehydrated=True, published=True)
        disc.tracking[token] = b
        disc._curve_to_token[b["curve"]] = token
        pos["_last_price"] = spot
        pos.pop("_tracking_lost_since", None)
        if t.get("venue") != "pool":
            self._switch_to_pool(pos, b, now)
        self.stats["rehydrated"] = self.stats.get("rehydrated", 0) + 1
        logger.warning(f"rh_paper {t.get('symbol')} {token[:10]} REHYDRATED from the v4 pool @ {spot:.3e} — bucket was lost, position stays active")
        return True

    async def _monitor(self, now: float):
        self.expire_pending_buys(now)
        for token, pos in list(self.positions.items()):
            if pos.get("_exiting") or pos.get("_live_sell_inflight"):
                continue
            b = self.state.rh_discovery.tracking.get(token)
            if not b:
                if await self._rehydrate_from_pool(token, pos, now):
                    continue
                if now - pos.setdefault("_tracking_lost_since", now) < TRACKING_LOST_GRACE_S:
                    continue
                pos["_exiting"] = True
                asyncio.create_task(self.exit(token, "tracking_lost"))
                continue
            reason = self._decide_exit(pos, b, now)
            self._push_pnl(token, pos, now)
            if reason:
                pos["_exiting"] = True
                asyncio.create_task(self.exit(token, reason))
            elif pos.get("_riding") and not pos.get("_pyramiding"):
                self._maybe_pyramid(token, pos, b, now)

    # ---------- pyramid into a riding winner ----------
    def _maybe_pyramid(self, token: str, pos: dict, b: dict, now: float):
        cfg = self.state.config
        if not getattr(cfg, "pyramid_enabled", True) or b.get("graduated"):
            return  # no pool buys (exits only post-graduation)
        t = pos["trade"]
        price = float(b.get("last_price_quote") or 0.0)
        adds = int(t.get("pyramids") or 0)
        if price <= 0 or adds >= int(getattr(cfg, "pyramid_max_adds", 3)):
            return
        step = float(getattr(cfg, "pyramid_step_pct", 10.0)) / 100.0
        nxt = pos.get("_pyramid_next") or pos["peak_price"] * (1.0 + step)   # first add needs a fresh higher-high
        if price < nxt:
            return
        pos["_pyramiding"] = True
        asyncio.create_task(self._pyramid(token, pos, b, price, now))

    async def _pyramid(self, token: str, pos: dict, b: dict, price: float, now: float):
        cfg = self.state.config
        t = pos["trade"]
        try:
            quote_usd = self._quote_usd(b["quote_symbol"])
            base_quote = float(t.get("base_entry_quote") or t.get("entry_quote") or 0.0)
            add_quote = base_quote * float(getattr(cfg, "pyramid_add_frac", 0.5))
            if add_quote <= 0 or quote_usd <= 0:
                return
            fee = fee_fraction(now - b["start"])
            if t.get("mode") == "live":
                fill = await self._live_buy(token, b, add_quote, price)
                if fill is None:
                    return
                add_quote = fill["quote_wei"] / self._qscale(b)
                tokens = fill["tokens_raw"] / 1e18
                t["entry_tokens_raw"] = str(int(t.get("entry_tokens_raw") or 0) + fill["tokens_raw"])
                t["fees_usd"] = float(t.get("fees_usd") or 0) + fill["fee_wei"] / self._qscale(b) * quote_usd + fill["gas_cost_wei"] / 1e18 * self._quote_usd("ETH")
            else:
                k0 = b.get("curve_k0")
                if k0 and b.get("curve_a"):
                    a = (price * k0) ** 0.5
                    tokens = k0 / a - k0 / (a + add_quote * (1.0 - fee))
                else:
                    tokens = add_quote * (1.0 - fee) / price
                t["fees_usd"] = float(t.get("fees_usd") or 0) + add_quote * fee * quote_usd + self._paper_gas_usd()
            t.setdefault("base_entry_quote", t.get("entry_quote"))
            t["entry_quote"] = float(t.get("entry_quote") or 0) + add_quote
            t["entry_usd"] = float(t.get("entry_usd") or 0) + add_quote * quote_usd
            t["entry_tokens"] = float(t.get("entry_tokens") or 0) + tokens
            t["entry_price_quote"] = t["entry_quote"] / t["entry_tokens"] if t["entry_tokens"] > 0 else price
            t["pyramids"] = int(t.get("pyramids") or 0) + 1
            t.setdefault("pyramid_adds", []).append({"price": price, "quote": add_quote, "usd": add_quote * quote_usd, "ts": now})
            pos["_pyramid_next"] = price * (1.0 + float(getattr(cfg, "pyramid_step_pct", 10.0)) / 100.0)
            self.stats["pyramids"] = self.stats.get("pyramids", 0) + 1
            await self.state.db.trades.update_one({"_id": t["id"]}, {"$set": {k: t[k] for k in
                ("entry_quote", "entry_usd", "entry_tokens", "entry_price_quote", "pyramids", "pyramid_adds", "fees_usd", "base_entry_quote")
                if k in t}})
            logger.info(f"rh_paper PYRAMID #{t['pyramids']} {t.get('symbol')} +${add_quote * quote_usd:.2f} @ {price:.3e} → avg {t['entry_price_quote']:.3e} [{t.get('mode')}]")
        except Exception as e:
            logger.warning(f"rh_paper pyramid failed {token[:10]}: {e}")
        finally:
            pos.pop("_pyramiding", None)

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
        self._maybe_fast_fail_add(token, pos, b, tr["price"] or b["last_price_quote"], now)
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

    def on_feed_tick(self, token: str, b: dict, est_price: float, seq: int, now: float, kind: str):
        """Sequencer feed saw an ordered buy/sell on a curve we hold; evaluate
        SL/TP/trail on the ESTIMATED post-trade price (~5s before the poll).
        The fill still resolves on real block prices ≥ seq + latency."""
        pos = self.positions.get(token)
        if not pos or pos.get("_exiting") or est_price <= 0:
            return
        self.stats["feed_ticks"] = self.stats.get("feed_ticks", 0) + 1
        reason = self._decide_exit(pos, b, now, price_override=est_price)
        if not reason:
            return
        pos["_exiting"] = True
        pos["exit_trigger"] = {
            "reason": reason,
            "block": seq,
            "price": est_price,
            "fill_block": seq + self.latency_blocks(),
            "source": "feed",
            "feed_kind": kind,
        }
        self.stats["feed_exits"] = self.stats.get("feed_exits", 0) + 1

    def on_rug_alert(self, token: str, b: dict, seq: int, now: float, est_price: float | None = None):
        """Sequencer feed saw a big sell for a curve we hold: exit NOW (bypasses
        the momentum gate). The sell is already ordered ahead of us, so the
        paper fill still lands `latency_blocks` after it — honest, just ~2s
        sooner than the poll would have reacted."""
        pos = self.positions.get(token)
        if not pos or pos.get("_exiting"):
            return
        pos["_exiting"] = True
        pos["exit_trigger"] = {
            "reason": "rug_detected",
            "block": seq,
            "price": est_price or b.get("last_price_quote") or pos.get("_last_price"),
            "fill_block": seq + self.latency_blocks(),
            "source": "feed",
        }
        self.stats["rug_exits"] = self.stats.get("rug_exits", 0) + 1

    def resolve_pending(self, head: int):
        """After each poll: fill any triggered exit whose fill block has
        landed, at the last curve price seen at/before that block."""
        self.resolve_pending_buys(head)
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
        if fill_price is None and time.time() < pos.get("_zero_quote_retry_after", 0.0):
            pos.pop("_exiting", None)
            return
        if not await self._fence(f"rh exit {token[:10]} ({reason})"):
            pos.pop("_exiting", None)
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
            if bk.get("graduated") and t.get("venue") != "pool":
                # the curve was swept while we hold: PONS → v4 is a venue change. Any exit reason raised on curve
                # prices no longer applies — switch venue, keep the position, let SL/TP/trail re-evaluate on the pool
                # (only an operator sell goes through regardless).
                self._switch_to_pool(pos, bk, time.time())
                if not str(reason).startswith("manual"):
                    pos.pop("_exiting", None)
                    pos.pop("_fill_task", None)
                    logger.warning(f"rh_paper {t.get('symbol')} {token[:10]} graduated mid-exit ({reason}) — trigger dropped, gates re-evaluate on pool prices")
                    return
            live_fill = None
            if t.get("mode") == "live":
                if pos.get("_live_sell_inflight"):
                    return  # another exit path is already selling this position on-chain
                if time.time() < pos.get("_live_retry_after", 0.0):
                    pos.pop("_exiting", None)
                    pos.pop("_fill_task", None)
                    return  # still cooling down from the last failed sell
                pos["_live_sell_inflight"] = True
                try:
                    live_fill = await self._live_sell(token, t, price)
                finally:
                    pos.pop("_live_sell_inflight", None)
                if live_fill is None:
                    # stranded: keep the position open for the next tick rather than book a phantom exit
                    pos["_live_retry_after"] = time.time() + LIVE_SELL_RETRY_COOLDOWN_S
                    pos.pop("_exiting", None)
                    pos.pop("_fill_task", None)
                    t["exit_error"] = self.last_live_error
                    return
                proceeds_quote = live_fill["quote_wei"] / self._qscale(t)
                gas_usd = live_fill["gas_cost_wei"] / 1e18 * self._quote_usd("ETH")
                price = proceeds_quote / t["entry_tokens"] if t.get("entry_tokens") else price
                exit_usd = max(0.0, proceeds_quote * quote_usd - gas_usd)
                t.update({"exit_sig": live_fill["tx"], "exit_gas_usd": gas_usd, "exit_latency_s": live_fill["latency_s"]})
            else:
                if t.get("venue") == "pool":
                    proceeds_quote, fee = await self._paper_pool_proceeds(token, t, price)
                    price = proceeds_quote / (t["entry_tokens"] * (1.0 - fee)) if t.get("entry_tokens") else price
                else:
                    proceeds_quote = t["entry_tokens"] * price * (1.0 - fee)
                if proceeds_quote <= 0 < float(t.get("entry_tokens") or 0):
                    # a zero quote with tokens still held is a dead venue, not a -100% fill: stay active, retry next tick
                    pos["_zero_quote_retry_after"] = time.time() + ZERO_QUOTE_RETRY_S
                    pos.pop("_exiting", None)
                    pos.pop("_fill_task", None)
                    t["exit_error"] = f"{reason}: zero quote on {t.get('venue') or 'curve'} — holding"
                    logger.warning(f"rh_paper {t.get('symbol')} {token[:10]} exit '{reason}' got a zero quote on {t.get('venue') or 'curve'} — position stays active")
                    return
                exit_usd = max(0.0, proceeds_quote * quote_usd - self._paper_gas_usd())
            # live: entry gas is a real cost of the round trip — book it against the trade
            pnl_usd = exit_usd - t["entry_usd"] - (float(t.get("entry_gas_usd") or 0.0) if live_fill else 0.0)
            pnl_pct = (pnl_usd / t["entry_usd"] * 100.0) if t["entry_usd"] > 0 else 0.0
            t.update({
                "status": "closed", "exit_time": now_utc().isoformat(), "exit_reason": reason,
                "exit_usd": round(exit_usd, 6), "exit_quote": proceeds_quote, "exit_price_quote": price,
                "pnl_usd": round(pnl_usd, 6), "pnl_pct": round(pnl_pct, 4),
                "fees_usd": round((t.get("fees_usd") or 0.0) + ((live_fill["fee_wei"] / self._qscale(t) * quote_usd + live_fill["gas_cost_wei"] / 1e18 * self._quote_usd("ETH")) if live_fill
                                                                 else proceeds_quote / (1 - fee) * fee * quote_usd + self._paper_gas_usd()), 6),
                "peak_price_quote": pos["peak_price"],
                "trough_price_quote": pos.get("trough_price", t["entry_price_quote"]),
                "rode_winner": bool(pos.get("_riding")), "ride_started_pnl_pct": pos.get("_ride_started_pnl_pct"),
                "peak_ts": pos.get("peak_ts"), "trough_ts": pos.get("trough_ts"),
                "peak_hold_s": (pos["peak_ts"] - pos["opened"]) if pos.get("peak_ts") else None,
                "exit_mode": "event" if trigger else "tick",
                "exit_deferrals": pos.get("_mom_defer_log") or None,
                "exit_deferred_s": round(time.time() - min(d["ts"] for d in pos["_mom_defer_log"]), 1) if pos.get("_mom_defer_log") else None,
                "exit_defer_bounded": bool(pos.get("_mom_defer_bounded")),
                "rug_alert": pos.get("_rug_alert"),
                "exit_venue": "pool" if (t.get("venue") == "pool" or (live_fill or {}).get("venue") == "pool") else "curve",
                "pool_hold_s": round(time.time() - pos["pool_since"], 1) if pos.get("pool_since") else None,
                "dip_forensics": pos.get("_dip_forensics"),
                "flush_held": bool(pos.get("_flush_hold_since")),
                "flush_hold_at_pnl_pct": pos.get("_flush_hold_at_pnl_pct"),
                "flush_held_s": round(time.time() - pos["_flush_hold_since"], 1) if pos.get("_flush_hold_since") else None,
            })
            if trigger:
                ep = t.get("entry_price_quote") or 0
                t.update({
                    "exit_trigger_block": trigger["block"],
                    "exit_fill_block": trigger["fill_block"],
                    "exit_trigger_price_quote": trigger["price"],
                    "exit_trigger_pnl_pct": round((trigger["price"] / ep - 1.0) * 100.0, 2) if (ep and trigger["price"]) else None,
                    "exit_latency_blocks": trigger["fill_block"] - trigger["block"],
                    "exit_trigger_source": trigger.get("source", "poll"),
                })
            await self.state.db.trades.update_one({"_id": t["id"]}, {"$set": t}, upsert=True)
            launch_update = {"pin_exited": True, "exit_pnl_pct": t["pnl_pct"], "exit_reason": reason}
            await self.state.db.launches.update_one({"_id": t.get("launch_id")}, {"$set": launch_update})
            await hub.broadcast("launch_update", {"id": t.get("launch_id"), "mint": token, **launch_update})
            await hub.broadcast("trade_exit", {**t, "entry_time": _iso(t.get("entry_time"))})
            self.stats["exits"] += 1
            try:
                import search_ledger
                asyncio.create_task(search_ledger.refresh(self.state.db, cfg))
            except Exception:
                pass
            self._watch_after_exit(token, t, price, self.state.rh_discovery.tracking.get(token), time.time())
            if live_fill:
                await self._check_live_kill()
            logger.info(f"rh_paper EXIT {t.get('symbol')} {token[:10]} {reason} pnl={pnl_pct:+.1f}% (${pnl_usd:+.3f})")
            self.positions.pop(token, None)
            self.reentry.record_exit(token, pnl_pct, cfg, was_sl=reason == "stop_loss")
        except asyncio.CancelledError:
            pos.pop("_exiting", None)
            pos.pop("_fill_task", None)
            raise
        except Exception as e:
            # never orphan a held position: the Mongo row stays `active`, so dropping it from memory would leave it
            # unmonitored (no SL / TP) until the next restart re-hydrates it — retry on a later tick instead
            logger.exception(f"rh_paper exit failed {token[:10]} ({reason}) — position stays active, retrying in {EXIT_ERROR_RETRY_S:.0f}s: {e}")
            pos.pop("_exiting", None)
            pos.pop("_fill_task", None)
            pos["_zero_quote_retry_after"] = time.time() + EXIT_ERROR_RETRY_S

    # ---------- API helpers ----------
    def _push_pnl(self, token: str, pos: dict, now: float):
        """Live P/L tick for the Active Trades row over WS (throttled 2 s/position) — the UI never polls while the WS is up."""
        if now - float(pos.get("_pnl_push_ts") or 0) < 2.0:
            return
        pos["_pnl_push_ts"] = now
        d = {"id": pos["trade"]["id"], "mint": token, "chain": CHAIN, "entry_price_quote": pos["trade"].get("entry_price_quote")}
        self.augment_trade(d)
        if "unrealized_pnl_pct" in d:
            asyncio.create_task(hub.broadcast("trade_update", d))

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
        w = pos.get("_recovery_watch")
        if w and entry > 0:
            d["recovery_watch"] = {"stop_pct": round((w["stop"] / entry - 1.0) * 100.0, 1), "target_pct": round((w["target"] / entry - 1.0) * 100.0, 1),
                                   "left_s": max(0, int(w["deadline"] - time.time())), "from_pct": w.get("from_pct")}
        if pos["trade"].get("r_trail"):
            d["r_trail"] = True
            d["r_trail_stop_pct"], d["r_trail_trail_pct"] = pos.get("_r_trail_stop_pct"), pos.get("_r_trail_trail_pct")
            if entry > 0 and pos.get("_r_trail_peak"):
                d["r_trail_peak_pct"] = round((pos["_r_trail_peak"] - entry) / entry * 100.0, 1)

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

    def hot_board(self) -> list[dict]:
        now = time.time()
        out = []
        for token, w in self.watch.items():
            hot = bool(w.get("hot"))
            left = None if hot else float(w.get("window_s") or 0) - (now - float(w.get("exit_time") or now))
            if left is not None and left <= 0:
                continue
            z = w.get("swings") or {}
            out.append({"mint": token, "symbol": w.get("symbol"), "hot": hot, "flush": bool(w.get("flush")),
                        "flush_exits": int(w.get("flush_exits") or 0),
                        "attempts": int(w.get("attempts") or 0),
                        "attempts_left": None if hot else max(0, int(w.get("max_attempts") or 0) - int(w.get("attempts") or 0)),
                        "strikes": int(w.get("strikes") or 0),
                        "lows": len(z.get("lows") or []), "highs": len(z.get("highs") or []),
                        "played_s": int(now - float(w.get("hot_since") or w.get("exit_time") or now)),
                        "size_multiplier": w.get("size_multiplier"), "seconds_left": None if left is None else int(left),
                        "last_trigger": w.get("last_trigger"), "original_pnl_usd": w.get("original_pnl_usd")})
        out.sort(key=lambda r: (not r["hot"], -(r["original_pnl_usd"] or 0)))
        return out

    def status(self) -> dict:
        now = time.time()
        return {**self.stats, "active": self._active(), "open_positions": len(self.positions), "hot_board": self.hot_board(),
                "pending_entries": sorted(self._pending_entries), "pending_buys": len(self.pending_buys),
                "slots_used": len(self.positions) + len(self._pending_entries), "max_positions": int(self.state.config.rh_max_positions),
                "focus": self.focus_state(now), "hot_dropped": list(self.hot_history),
                "positions": [{"mint": m, "symbol": p["trade"].get("symbol"), "entry_price_quote": p["trade"].get("entry_price_quote"),
                               "last_price": p["_last_price"], "peak_price": p["peak_price"], "riding": bool(p.get("_riding")),
                               "venue": p["trade"].get("venue") or "curve",
                               "pyramids": p["trade"].get("pyramids") or 0} for m, p in self.positions.items()]}
