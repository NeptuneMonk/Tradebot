"""
Pump.fun token discovery — bring already-existing tokens into the scanner.

The mempool listener only sees tokens created AFTER the bot started.
This module polls Pump.fun's public coins API every ~2min, finds tokens
in the [now - max_age, now - min_age] window, and seeds them into
BotState.tracking. The existing scanner gates then evaluate them
just like organically-observed launches.

Once seeded, live trades for those mints flow through the existing
Helius logsSubscribe listener automatically (it subscribes to the whole
Pump program, not per-mint).
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import TYPE_CHECKING

import httpx

from models import Launch
from solana_client import LAMPORTS_PER_SOL
from ws_hub import hub

if TYPE_CHECKING:
    from bot import BotState

logger = logging.getLogger("discovery")

PUMPFUN_API = "https://frontend-api-v3.pump.fun"
DISCOVERY_INTERVAL_S = 120
COINS_PER_CYCLE = 50
HTTP_TIMEOUT = 12.0


class PumpfunDiscovery:
    def __init__(self, state: "BotState"):
        self.state = state
        self._task: asyncio.Task | None = None

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def _loop(self):
        # Stagger first run by 5s so listener has time to settle
        await asyncio.sleep(5.0)
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"discovery loop error: {e}")
            await asyncio.sleep(DISCOVERY_INTERVAL_S)

    async def run_once(self) -> int:
        """Returns the number of newly seeded tokens."""
        st = self.state
        cfg = st.config
        max_age_s = cfg.scanner_window_hours * 3600
        min_age_s = cfg.scanner_min_age_minutes * 60
        if max_age_s <= min_age_s:
            return 0
        now = time.time()
        lo_ts = now - max_age_s   # earliest creation time we care about
        hi_ts = now - min_age_s   # latest creation time (must be at least min_age old)

        coins = await self._fetch_aged_coins(lo_ts, hi_ts)
        if not coins:
            logger.info(f"discovery: 0 coins in band [{min_age_s/60:.0f}m, {max_age_s/60:.0f}m]")
            return 0

        seeded = 0
        for c in coins:
            if seeded >= COINS_PER_CYCLE:
                break
            mint = c.get("mint")
            if not mint:
                continue
            if mint in st.tracking or mint in st.active_trades or mint in st.entered_mints:
                continue
            created_ms = c.get("created_timestamp") or 0
            created_s = created_ms / 1000.0
            # _fetch_aged_coins already filtered to the band, but double-check
            if not (lo_ts <= created_s <= hi_ts):
                continue
            # Skip tokens whose curve has already completed (LP deployed)
            if c.get("complete"):
                continue
            try:
                await self._seed_token(c, created_s)
                seeded += 1
            except Exception as e:
                logger.debug(f"seed failed for {mint}: {e}")

        logger.info(f"discovery: {len(coins)} in band, seeded {seeded}")
        if seeded:
            await hub.broadcast("discovery", {"seeded": seeded, "ts": now})
        return seeded

    async def _fetch_aged_coins(self, lo_ts: float, hi_ts: float) -> list[dict]:
        """Pull actively-traded tokens (sorted by last_trade_timestamp DESC) and
        filter to the [lo_ts, hi_ts] creation-time band. Sorting by trade time
        rather than creation time naturally surfaces tokens with momentum and
        bypasses Pump.fun's ~1000-offset creation-pagination cap."""
        url = f"{PUMPFUN_API}/coins"
        PAGE_SIZE = 240
        MAX_PAGES = 5  # 5×240 = ~1200 actively-traded tokens; plenty of overlap with the band
        out: list[dict] = []
        seen: set[str] = set()
        offset = 0
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            for _ in range(MAX_PAGES):
                params = {
                    "offset": offset,
                    "limit": PAGE_SIZE,
                    "sort": "last_trade_timestamp",
                    "order": "DESC",
                    "includeNsfw": "true",
                }
                try:
                    r = await client.get(url, params=params, headers={"accept": "application/json"})
                    r.raise_for_status()
                    page = r.json()
                    if isinstance(page, dict):
                        page = page.get("data") or page.get("coins") or []
                    if not isinstance(page, list) or not page:
                        break
                except Exception as e:
                    logger.warning(f"pumpfun coins API page offset={offset} failed: {e}")
                    break
                for c in page:
                    mint = c.get("mint")
                    if not mint or mint in seen:
                        continue
                    seen.add(mint)
                    ts_s = (c.get("created_timestamp") or 0) / 1000.0
                    if lo_ts <= ts_s <= hi_ts:
                        out.append(c)
                offset += PAGE_SIZE
                # Be a polite client between pages
                await asyncio.sleep(0.25)
        return out

    async def _seed_token(self, coin: dict, created_s: float):
        st = self.state
        mint = coin["mint"]
        vsr = int(coin.get("virtual_sol_reserves") or 0)
        vtr = int(coin.get("virtual_token_reserves") or 0)
        cur_price = (vsr / vtr / LAMPORTS_PER_SOL) if (vsr and vtr) else 0.0

        bucket = {
            "launch_id": f"disc-{mint[:8]}",
            "creator": coin.get("creator") or "",
            "start": created_s,  # use real creation time so seasoning math is correct
            "buyers": set(),
            "buy_events": deque(maxlen=500),
            "sol_inflow_lamports": 0,
            "buy_count": 0,
            "curve_fill_pct": min(100.0, max(0.0, (vsr - 30_000_000_000) / 85_000_000_000 * 100)) if vsr else 0.0,
            "social_score": 0,
            "social_sources": {},
            "last_persist": 0.0,
            "name": coin.get("name"),
            "symbol": coin.get("symbol"),
            "creator_rugs": 0,
            "first_seen_price_sol": cur_price,
            "last_price_sol": cur_price,
            "last_vsr_lamports": vsr,
            "scanner_eligible": True,
            "scanner_last_attempt": 0.0,
            "discovered": True,  # flag so UI / API can distinguish
        }
        st.tracking[mint] = bucket
        # Also push a synthetic launch into the recent feed so the UI shows it
        synthetic = Launch(
            mint=mint,
            creator=bucket["creator"],
            bonding_curve=coin.get("bonding_curve") or "",
            name=bucket["name"],
            symbol=bucket["symbol"],
        )
        synthetic.id = bucket["launch_id"]
        synthetic.classifier_action = "discovered"
        doc = synthetic.model_dump()
        doc["detected_at"] = doc["detected_at"].isoformat()
        doc["discovered"] = True
        st.recent_launches.insert(0, doc)
        st.recent_launches = st.recent_launches[:50]
        await hub.broadcast("launch", doc)
