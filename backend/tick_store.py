"""Tick store — persists the price path + buy flow of EVERY tracked token (both chains), not just the
ones we traded, so the Doctor can replay entry gates against the whole universe and see what happened
after we exited. One Mongo doc per (chain, token), appended every FLUSH_S, TTL 48h.
"""
from __future__ import annotations

import asyncio
import logging
import time

from pymongo import UpdateOne

logger = logging.getLogger("tick_store")

FLUSH_S = 10.0
TTL_S = 48 * 3600
MAX_SAMPLES = 3000
MAX_BUYS = 1500
COL = "tick_paths"


def _bucket_view(chain: str, mint: str, b: dict) -> dict:
    if chain == "rh":
        return {"symbol": b.get("symbol"), "start": b.get("start"), "first_price": b.get("first_price_quote") or 0.0,
                "quote_symbol": b.get("quote_symbol") or "ETH", "graduated": bool(b.get("graduated")),
                "curve_fill_pct": b.get("curve_fill_pct") or 0.0, "mc_usd": b.get("usd_market_cap") or 0.0,
                "creator": b.get("creator"), "buy_scale": 1.0}
    return {"symbol": b.get("symbol"), "start": b.get("start"), "first_price": b.get("first_seen_price_sol") or 0.0,
            "quote_symbol": "SOL", "graduated": bool(b.get("graduated_at")), "curve_fill_pct": b.get("curve_fill_pct") or 0.0,
            "mc_usd": b.get("usd_market_cap") or 0.0, "creator": b.get("creator"), "buy_scale": 1e-9}


class TickStore:
    def __init__(self, state):
        self.state = state
        self.db = state.db
        self._cur: dict[str, tuple[float, float]] = {}   # doc id → (last sample ts, last buy ts)
        self._task: asyncio.Task | None = None
        self.stats = {"flushes": 0, "docs": 0, "samples": 0, "last_error": ""}

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def _loop(self):
        try:
            await self.db[COL].create_index("updated_at", expireAfterSeconds=TTL_S)
            await self.db[COL].create_index([("chain", 1), ("start", -1)])
        except Exception as e:
            logger.warning(f"tick_store index: {e}")
        while True:
            try:
                await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.stats["last_error"] = str(e)[:200]
                logger.warning(f"tick_store flush error: {e}")
            await asyncio.sleep(FLUSH_S)

    def _sources(self) -> list[tuple[str, dict]]:
        out = []
        rh = getattr(self.state, "rh_discovery", None)
        if rh is not None:
            out.append(("rh", rh.tracking))
        sol = getattr(self.state, "tracking", None)
        if isinstance(sol, dict):
            out.append(("sol", sol))
        return out

    def _ops(self, now: float) -> list[UpdateOne]:
        ops = []
        for chain, buckets in self._sources():
            for mint, b in list(buckets.items()):
                samples = b.get("price_samples")
                if not samples:
                    continue
                did = f"{chain}:{mint}"
                last_s, last_b = self._cur.get(did, (0.0, 0.0))
                new_s_raw = [(ts, px) for ts, px in samples if ts > last_s and px]
                new_s = [[round(ts, 2), px] for ts, px in new_s_raw]
                view = _bucket_view(chain, mint, b)
                new_b_raw = [ev for ev in (b.get("buy_events") or ()) if ev[0] > last_b]
                new_b = [[round(ev[0], 2), float(ev[1]) * view["buy_scale"], str(ev[2])[-8:]] for ev in new_b_raw]
                if not new_s and not new_b:
                    continue
                self._cur[did] = (max([last_s] + [s[0] for s in new_s_raw]), max([last_b] + [ev[0] for ev in new_b_raw]))
                push = {}
                if new_s:
                    push["samples"] = {"$each": new_s, "$slice": -MAX_SAMPLES}
                if new_b:
                    push["buys"] = {"$each": new_b, "$slice": -MAX_BUYS}
                meta = {k: v for k, v in view.items() if k != "buy_scale"}
                ops.append(UpdateOne({"_id": did}, {"$set": {**meta, "chain": chain, "mint": mint, "updated_at": now},
                                                    "$push": push}, upsert=True))
                self.stats["samples"] += len(new_s)
        return ops

    async def flush(self):
        now = time.time()
        ops = self._ops(now)
        if ops:
            await self.db[COL].bulk_write(ops, ordered=False)
            self.stats["docs"] = len(self._cur)
        self.stats["flushes"] += 1
        # forget cursors for tokens no longer tracked
        live = {f"{c}:{m}" for c, bk in self._sources() for m in bk}
        for k in [k for k in self._cur if k not in live]:
            self._cur.pop(k, None)

    async def load(self, chain: str, since_ts: float, limit: int = 800) -> list[dict]:
        cur = self.db[COL].find({"chain": chain, "start": {"$gte": since_ts}}, {"_id": 0}).sort("start", -1).limit(limit)
        return await cur.to_list(limit)

    async def post_exit_peak_pct(self, chain: str, mint: str, exit_ts: float, exit_price: float, window_s: float = 600.0) -> float | None:
        """How far the token ran above our exit price in the window after we left (None = no data)."""
        if exit_price <= 0:
            return None
        doc = await self.db[COL].find_one({"_id": f"{chain}:{mint}"}, {"samples": 1})
        if not doc:
            return None
        pts = [px for ts, px in doc.get("samples") or () if exit_ts < ts <= exit_ts + window_s]
        if not pts:
            return None
        return (max(pts) / exit_price - 1.0) * 100.0
