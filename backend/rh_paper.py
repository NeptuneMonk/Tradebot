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
MONITOR_INTERVAL_S = 1.0


def snipe_tax_bps(elapsed_s: float) -> int:
    """PONS: snipeTaxStartBps >> ((elapsed*14) // snipeTaxSeconds), zero once elapsed >= snipeTaxSeconds."""
    if elapsed_s >= SNIPE_TAX_SECONDS:
        return 0
    return SNIPE_TAX_START_BPS >> int((max(0.0, elapsed_s) * 14) // SNIPE_TAX_SECONDS)


def fee_fraction(elapsed_s: float) -> float:
    return min(0.999, (CURVE_FEE_BPS + snipe_tax_bps(elapsed_s)) / 10_000.0)


class RHPaperTrader:
    def __init__(self, state: "BotState"):
        self.state = state
        self.positions: dict[str, dict] = {}
        self.entered: set[str] = set()
        self._task: asyncio.Task | None = None
        self._pending_entries: set[str] = set()
        self.stats = {"entries": 0, "exits": 0, "skipped": 0, "last_scan_ts": 0.0}

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    def _active(self) -> bool:
        cfg = self.state.config
        return bool(getattr(cfg, "enabled", False)) and bool(getattr(cfg, "rh_paper_enabled", False))

    async def _loop(self):
        await asyncio.sleep(4.0)
        await self._restore()
        while True:
            try:
                now = time.time()
                if self._active():
                    self._scan_entries(now)
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

    async def _enter(self, token: str):
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
            stake_usd = float(cfg.max_trade_usd)
            stake_quote = stake_usd / quote_usd
            tokens = stake_quote * (1.0 - fee) / price
            trade = Trade(
                mint=token, creator=b["creator"], name=b["name"], symbol=b["symbol"],
                mode="paper", entry_usd=stake_usd, entry_tokens=tokens,
                protocol=PROTOCOL, classifier_action=ENTRY_ACTION, risk_score=50,
                chain=CHAIN, quote_symbol=b["quote_symbol"], entry_quote=stake_quote,
                entry_price_quote=price, fees_usd=stake_quote * fee * quote_usd + RH_GAS_USD,
            )
            doc = trade.model_dump()
            # entry_time stays a datetime (bot.py stores BSON dates; a string
            # here would sort below every SOL trade in /trades/history).
            doc["launch_id"] = b["launch_id"]
            await self.state.db.trades.update_one({"_id": trade.id}, {"$set": {**doc, "_id": trade.id}}, upsert=True)
            self.positions[token] = {"trade": doc, "peak_price": price, "_last_price": price, "opened": now}
            self.entered.add(token)
            self.stats["entries"] += 1
            launch_update = {"entered": True, "entry_action": ENTRY_ACTION}
            await self.state.db.launches.update_one({"_id": b["launch_id"]}, {"$set": launch_update})
            await hub.broadcast("launch_update", {"id": b["launch_id"], "mint": token, **launch_update})
            await hub.broadcast("trade_enter", doc)
            logger.info(f"rh_paper ENTER {b['symbol']} {token[:10]} ${stake_usd:.2f} @ {price:.3e} {b['quote_symbol']} fee={fee*100:.2f}%")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"rh_paper enter failed {token[:10]}: {e}")
        finally:
            self._pending_entries.discard(token)

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

    async def exit(self, token: str, reason: str):
        pos = self.positions.get(token)
        if not pos:
            return
        pos["_exiting"] = True
        cfg = self.state.config
        t = pos["trade"]
        try:
            await asyncio.sleep(max(0, cfg.paper_exit_latency_ms) / 1000.0)
            b = self.state.rh_discovery.tracking.get(token)
            price = (b["last_price_quote"] if b else 0.0) or pos["_last_price"] or t["entry_price_quote"]
            quote_usd = self._quote_usd(t.get("quote_symbol") or "ETH") or 0.0
            if quote_usd <= 0:
                quote_usd = t["entry_usd"] / t["entry_quote"] if t.get("entry_quote") else 0.0
            fee = CURVE_FEE_BPS / 10_000.0
            proceeds_quote = t["entry_tokens"] * price * (1.0 - fee)
            exit_usd = max(0.0, proceeds_quote * quote_usd - RH_GAS_USD)
            pnl_usd = exit_usd - t["entry_usd"]
            pnl_pct = (pnl_usd / t["entry_usd"] * 100.0) if t["entry_usd"] > 0 else 0.0
            t.update({
                "status": "closed", "exit_time": now_utc().isoformat(), "exit_reason": reason,
                "exit_usd": round(exit_usd, 6), "exit_quote": proceeds_quote, "exit_price_quote": price,
                "pnl_usd": round(pnl_usd, 6), "pnl_pct": round(pnl_pct, 4),
                "fees_usd": round((t.get("fees_usd") or 0.0) + proceeds_quote / (1 - fee) * fee * quote_usd + RH_GAS_USD, 6),
                "peak_price_quote": pos["peak_price"],
            })
            await self.state.db.trades.update_one({"_id": t["id"]}, {"$set": t}, upsert=True)
            launch_update = {"pin_exited": True, "exit_pnl_pct": t["pnl_pct"], "exit_reason": reason}
            await self.state.db.launches.update_one({"_id": t.get("launch_id")}, {"$set": launch_update})
            await hub.broadcast("launch_update", {"id": t.get("launch_id"), "mint": token, **launch_update})
            await hub.broadcast("trade_exit", {"id": t["id"], "mint": token, "symbol": t.get("symbol"), "reason": reason})
            self.stats["exits"] += 1
            logger.info(f"rh_paper EXIT {t.get('symbol')} {token[:10]} {reason} pnl={pnl_pct:+.1f}% (${pnl_usd:+.3f})")
            self.positions.pop(token, None)
        except asyncio.CancelledError:
            pos.pop("_exiting", None)
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
