"""
Stream-driven floor: the Helius logsSubscribe tape already carries every Pump.fun curve trade, so "does this token
clear the inflow floor?" is answered live instead of by polling Pump.fun/DexScreener every 2 min.

Every launch we see gets a Pulse: a tiny ring of buy-inflow buckets covering the scanner inflow window (default
5 min) plus the identity the seeder needs. A 3 s tick seeds any pulse that is inside the operator's age gate (via
`scope_reason`) and whose rolling buy inflow ≥ `scanner_min_recent_inflow_sol`. Pulses older than the gate are dropped.
Tokens that launched before this process started have no pulse — the 60 s Pump.fun pull remains the backstop.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot import BotState

logger = logging.getLogger("stream_floor")

LAMPORTS_PER_SOL = 1_000_000_000
TICK_S = 3.0
N_BUCKETS = 30
MAX_PULSES = 6000
SEED_PER_TICK = 10


class Pulse:
    __slots__ = ("created", "last_ts", "buys", "buckets", "head", "creator", "name", "symbol", "bonding_curve", "vsr", "vtr", "seeded_at")

    def __init__(self, created: float, width: float, creator: str, name, symbol, bonding_curve: str):
        self.created = created
        self.last_ts = created
        self.buys = 0
        self.buckets = [0.0] * N_BUCKETS
        self.head = int(created // width)     # ring position (seconds // bucket width) of the newest bucket
        self.creator = creator
        self.name = name
        self.symbol = symbol
        self.bonding_curve = bonding_curve
        self.vsr = 0
        self.vtr = 0
        self.seeded_at = 0.0


class StreamFloor:
    def __init__(self, state: "BotState"):
        self.state = state
        self.pulses: dict[str, Pulse] = {}
        self.stats = {"seeded": 0, "dropped": 0}
        self._seeded_ts: deque = deque()
        self._task: asyncio.Task | None = None

    # ---------- window geometry ----------
    def _window_s(self) -> float:
        return float(int(getattr(self.state.config, "scanner_recent_inflow_window_s", 300) or 300))

    def _width(self) -> float:
        return max(1.0, self._window_s() / N_BUCKETS)

    def _slot(self, p: Pulse, ts: float) -> int | None:
        """Ring slot for `ts`, rolling the ring forward (zeroing skipped buckets) when time has advanced."""
        w = self._width()
        idx = int(ts // w)
        if idx < p.head:
            return None if p.head - idx >= N_BUCKETS else (idx % N_BUCKETS)
        if idx > p.head:
            steps = idx - p.head
            if steps >= N_BUCKETS:
                p.buckets = [0.0] * N_BUCKETS
            else:
                for k in range(1, steps + 1):
                    p.buckets[(p.head + k) % N_BUCKETS] = 0.0
            p.head = idx
        return idx % N_BUCKETS

    def inflow_sol(self, mint: str, now: float | None = None) -> float:
        p = self.pulses.get(mint)
        if p is None:
            return 0.0
        self._slot(p, time.time() if now is None else now)       # roll forward so stale buckets are zeroed
        return sum(p.buckets)

    # ---------- tape hooks (hot path: dict lookup + float add) ----------
    def on_launch(self, launch_data: dict, now: float | None = None) -> None:
        mint = launch_data.get("mint")
        if not mint or mint in self.pulses:
            return
        now = time.time() if now is None else now
        if len(self.pulses) >= MAX_PULSES:
            oldest = min(self.pulses, key=lambda m: self.pulses[m].created)
            self.pulses.pop(oldest, None)
            self.stats["dropped"] += 1
        self.pulses[mint] = Pulse(now, self._width(), launch_data.get("creator") or "", launch_data.get("name"), launch_data.get("symbol"),
                                  launch_data.get("bonding_curve") or "")

    def on_trade(self, trade_data: dict, now: float | None = None) -> None:
        p = self.pulses.get(trade_data.get("mint"))
        if p is None:
            return
        now = time.time() if now is None else now
        p.last_ts = now
        vsr, vtr = trade_data.get("virtual_sol_reserves") or 0, trade_data.get("virtual_token_reserves") or 0
        if vsr and vtr:
            p.vsr, p.vtr = int(vsr), int(vtr)
        if trade_data.get("is_buy"):
            slot = self._slot(p, now)
            if slot is not None:
                p.buckets[slot] += int(trade_data.get("sol_amount") or 0) / LAMPORTS_PER_SOL
            p.buys += 1

    # ---------- seeding ----------
    def _coin(self, mint: str, p: Pulse, sol_usd: float) -> dict:
        mc_usd = (p.vsr / p.vtr * 1_000_000 * sol_usd) if (p.vsr and p.vtr and sol_usd > 0) else 0.0
        return {"mint": mint, "creator": p.creator, "name": p.name, "symbol": p.symbol, "bonding_curve": p.bonding_curve,
                "virtual_sol_reserves": p.vsr, "virtual_token_reserves": p.vtr, "complete": False,
                "created_timestamp": int(p.created * 1000), "last_trade_timestamp": int(p.last_ts * 1000),
                "usd_market_cap": mc_usd, "buy_count": p.buys}

    async def tick(self, now: float | None = None) -> int:
        st = self.state
        cfg = st.config
        now = time.time() if now is None else now
        floor = float(getattr(cfg, "scanner_min_recent_inflow_sol", 0.0) or 0.0)
        hi_s = float(cfg.band_new_max_age_min) * 60.0
        seeded = 0
        sol_usd = 0.0
        for mint, p in list(self.pulses.items()):
            age = now - p.created
            if age > hi_s + 60.0:
                self.pulses.pop(mint, None)
                continue
            if mint in st.tracking or mint in st.active_trades or mint in st.entered_mints:
                continue
            if st.scope_reason(mint, {"protocol": "pumpfun", "start": p.created}, now):
                continue
            if floor > 0 and self.inflow_sol(mint, now) < floor:
                continue
            if seeded >= SEED_PER_TICK:
                break
            if not sol_usd:
                try:
                    from solana_client import get_sol_usd_price
                    sol_usd = float(await get_sol_usd_price() or 0.0)
                except Exception:
                    sol_usd = 0.0
            try:
                await st.discovery._seed_token(self._coin(mint, p, sol_usd), p.created, False)
                b = st.tracking.get(mint)
                if b is not None:
                    b["alive_inflow_sol"] = round(self.inflow_sol(mint, now), 3)
                    b["stream_seeded"] = True
                    b["last_trade_ts"] = p.last_ts
                p.seeded_at = now
                seeded += 1
            except Exception as e:
                logger.debug(f"stream seed failed for {mint}: {e}")
        if seeded:
            self.stats["seeded"] += seeded
            self._seeded_ts.extend([now] * seeded)
        while self._seeded_ts and now - self._seeded_ts[0] > 60.0:
            self._seeded_ts.popleft()
        return seeded

    def snapshot(self) -> dict:
        return {"pulses": len(self.pulses), "seeded_1m": len(self._seeded_ts), "seeded_total": self.stats["seeded"]}

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"stream floor tick failed: {e}")
            await asyncio.sleep(TICK_S)
