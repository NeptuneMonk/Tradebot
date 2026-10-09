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
from tick_store import EVENT_KEEP
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import httpx

from models import Launch
from solana_client import LAMPORTS_PER_SOL
from pumpfun import LAUNCH_BASELINE_PRICE_SOL
import pumpswap
from ws_hub import hub

if TYPE_CHECKING:
    from bot import BotState

logger = logging.getLogger("discovery")

PUMPFUN_API = "https://frontend-api-v3.pump.fun"
HOLDERS_REFRESH_S = 120      # Helius DAS getTokenAccounts per graduated token (≤ 3 pages of 1000)
DISCOVERY_INTERVAL_S = 60     # backstop only: the Helius tape (stream_floor.py) seeds live; this catches pre-start tokens
REFRESH_INTERVAL_S = 60      # how often to re-poll MC for already-tracked discovered tokens
COLD_POOL_REFRESH_S = 300    # graduated pools far below the seasoned MC gate: reserves re-read every 5 min, not every cycle
MC_SAMPLE_KEEP = 12          # 12 × 60s = 12min of MC samples for velocity calc
COINS_PER_CYCLE = 50
HTTP_TIMEOUT = 12.0
GRADUATED_INTERVAL_S = 60    # recently-graduated feed poll cadence (Seasoned supply)
GRADUATED_PAGE_SIZE = 100
GRADUATED_PER_CYCLE = 30
# Window discovery (alive = inflow): Pump.fun's coin index tells us WHICH tokens in the age band traded recently, not how
# much. DexScreener's batched pair stats (30 mints / call, pump.fun curves + PumpSwap pools) give volume and buy/sell
# counts per window — buy inflow ≈ volume × buy share. Only tokens whose inflow over the scanner inflow window clears
# the bot's own `scanner_min_recent_inflow_sol` get seeded; the rest never reach the tracker or the dashboard.
DEX_BATCH = 30
DEX_URL = "https://api.dexscreener.com/tokens/v1/solana/{mints}"


class PumpfunDiscovery:
    def __init__(self, state: "BotState"):
        self.state = state
        self._task: asyncio.Task | None = None
        self._refresh_task: asyncio.Task | None = None
        self._graduated_task: asyncio.Task | None = None
        self._graduated_seen: set[str] = set()

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(self._refresh_loop())
        if self._graduated_task is None or self._graduated_task.done():
            self._graduated_task = asyncio.create_task(self._graduated_loop())

    async def _graduated_loop(self):
        """Seasoned-band supply: poll Pump.fun's recently-graduated list every
        GRADUATED_INTERVAL_S and seed those mints as `pumpswap` buckets. The
        creation-time band in `run_once` only ever catches the ~1% of tokens
        that graduate AND were created in-band, which starved the band."""
        await asyncio.sleep(10.0)
        while True:
            try:
                if getattr(self.state.config, "scanner_graduated_feed_enabled", True):
                    await self.graduated_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"graduated feed error: {e}")
            await asyncio.sleep(GRADUATED_INTERVAL_S)

    async def fetch_recent_graduated(self, limit: int = GRADUATED_PAGE_SIZE) -> list[dict]:
        """Most recently CREATED tokens that have `complete=true` — a close
        proxy for 'recently graduated' since graduation follows creation by
        minutes-to-hours. Each row carries `pool_address` (PumpSwap pool)."""
        params = {
            "offset": 0, "limit": limit, "sort": "created_timestamp",
            "order": "DESC", "includeNsfw": "true", "complete": "true",
        }
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            r = await client.get(f"{PUMPFUN_API}/coins", params=params,
                                 headers={"accept": "application/json"})
            r.raise_for_status()
            page = r.json()
        if isinstance(page, dict):
            page = page.get("data") or page.get("coins") or []
        return [c for c in (page or []) if isinstance(c, dict) and c.get("mint") and c.get("complete")]

    async def graduated_once(self) -> int:
        """Seed newly-graduated tokens into tracking as pumpswap buckets.
        Returns the number seeded. Helius-aware: skips the cycle when the
        kill-switch is paused (seeding fetches pool state via RPC)."""
        st = self.state
        try:
            from helius_gate import is_helius_paused
            if is_helius_paused():
                return 0
        except Exception:
            pass
        coins = await self.fetch_recent_graduated()
        now = time.time()
        if st.scope_reason("", {"protocol": "pumpswap", "graduated_at": now}, now):
            return 0                                     # seasoned entries / hunt book off: nothing to seed
        band_max_s = float(getattr(st.config, "band_seasoned_max_age_min", 240.0)) * 60.0
        # Drop feed tokens that aged out of the Seasoned band — otherwise the
        # refresh loop keeps spending a pool-state RPC per minute on each.
        for mint in [m for m, b in st.tracking.items()
                     if b.get("graduated_feed") and not b.get("pinned") and m not in st.active_trades
                     and now - (b.get("graduated_at") or now) > band_max_s + 300]:
            st.tracking.pop(mint, None)
        max_created_age_s = band_max_s + 6 * 3600.0
        seeded = 0
        todo: list[tuple[dict, float]] = []
        for c in coins:
            if len(todo) >= GRADUATED_PER_CYCLE:
                break
            mint = c["mint"]
            if not c.get("complete"):
                continue
            if mint in st.tracking or mint in st.active_trades or mint in st.entered_mints:
                continue
            if mint in self._graduated_seen:
                continue
            created_s = (c.get("created_timestamp") or 0) / 1000.0
            if created_s and now - created_s > max_created_age_s:
                continue
            if not (c.get("pool_address") or c.get("pump_swap_pool")):
                continue
            todo.append((c, created_s))
        # one batched reserves read for the whole cycle (was 2 RPC calls per seeded token)
        try:
            states = await pumpswap.fetch_pool_states_batch([c.get("pump_swap_pool") or c.get("pool_address") for c, _ in todo])
        except Exception as e:
            logger.debug(f"graduated batch pool read failed: {e}")
            states = {}
        for c, created_s in todo:
            mint = c["mint"]
            try:
                await self._seed_token(c, created_s or now, True, pool_state=states.get(c.get("pump_swap_pool") or c.get("pool_address")))
                st.tracking[mint]["graduated_feed"] = True
                self._graduated_seen.add(mint)
                seeded += 1
            except Exception as e:
                logger.debug(f"graduated seed failed for {mint}: {e}")
            await asyncio.sleep(0.1)
        if len(self._graduated_seen) > 5000:
            self._graduated_seen = set(list(self._graduated_seen)[-2500:])
        logger.info(f"graduated feed: {len(coins)} recent graduates, seeded {seeded}")
        if seeded:
            await hub.broadcast("discovery", {"seeded": seeded, "source": "graduated", "ts": now})
        return seeded

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

    async def _refresh_loop(self):
        """Re-poll Pump.fun's /coins API every REFRESH_INTERVAL_S to update
        live signals for already-tracked discovered tokens — MC, last_trade,
        and price (via virtual reserves for bonding curve, pool reserves for
        PumpSwap). Appends to rolling `mc_samples` and `price_samples` deques
        so the seasoned-band velocity gates have real data to work with.

        Without this loop, `mc_samples` was always empty → MC velocity always
        computed as 0% → seasoned tokens never passed the velocity gate.
        """
        await asyncio.sleep(REFRESH_INTERVAL_S)
        while True:
            try:
                if getattr(getattr(self.state, "lite", None), "active", False):
                    logger.debug("discovery refresh skipped — lite mode")
                else:
                    await self._refresh_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"discovery refresh loop error: {e}")
            await asyncio.sleep(REFRESH_INTERVAL_S)

    async def _refresh_once(self):
        """Iterate tracked discovered mints, pull fresh MC + price, append
        samples. Batches Pump.fun API calls (per-mint endpoint) up to 25 at a
        time with throttling to stay polite."""
        st = self.state
        # Snapshot mint list — discovered tokens (always need refresh for MC
        # samples) PLUS mempool-tracked tokens that are close to graduation
        # so we can catch the protocol flip and stamp `graduated_at` in real
        # time (otherwise a graduated curve sits in tracking as "pumpfun"
        # indefinitely and never appears in the Seasoned band).
        targets = []
        for mint, b in st.tracking.items():
            # held tokens stay in the refresh (their Seasoned card used to freeze the moment we bought) — the
            # monitor's per-tick pool read is reused below instead of a second RPC read
            if b.get("discovered"):
                targets.append((mint, b))
            elif (
                b.get("protocol") == "pumpfun"
                and float(b.get("curve_fill_pct") or 0.0) >= 80.0
            ):
                # Near-graduation: poll Pump.fun API for the up-to-date
                # complete flag. Tokens at <80% fill are noise; skip them.
                targets.append((mint, b))
        if not targets:
            return
        now = time.time()
        url = f"{PUMPFUN_API}/coins"
        # SOL price cached per-cycle so we don't refetch per-mint.
        try:
            from solana_client import get_sol_usd_price
            sol_usd = await get_sol_usd_price()
        except Exception:
            sol_usd = 0.0
        # One batched reserves read for every graduated pool we already know (was 2 RPC calls per token per
        # minute — the single largest getAccountInfo consumer). Pools resolved mid-loop fall back to a single read.
        pool_states: dict[str, dict] = {}
        cold_pools: set[str] = set()     # far below the seasoned MC gate → reserves re-read every COLD_POOL_REFRESH_S only
        mc_floor = float(getattr(st.config, "scanner_min_mc_usd_seasoned", 30000.0) or 0) * 0.8
        hot: list[str] = []
        for mint, b in targets:
            pool = b.get("pumpswap_pool")
            if b.get("protocol") != "pumpswap" or not pool:
                continue
            slot_cache = (st.active_trades.get(mint) or {}).get("_pool_cache")
            if slot_cache and slot_cache.get("pool") == pool:
                pool_states[pool] = slot_cache      # open position: the monitor already read this pool this tick
                continue
            cold = 0 < float(b.get("usd_market_cap") or 0) < mc_floor and now - float(b.get("_pool_read_ts") or 0) < COLD_POOL_REFRESH_S
            (cold_pools.add if cold else hot.append)(pool)
        try:
            from helius_gate import is_helius_paused
            if not is_helius_paused() and hot:
                pool_states.update(await pumpswap.fetch_pool_states_batch(hot))
        except Exception as e:
            logger.debug(f"discovery batched pool read failed: {e}")
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            for mint, bucket in targets:
                # Pump.fun's per-mint coin endpoint returns HTTP 200 with an
                # EMPTY BODY for graduated tokens. Treat that as "no fresh
                # API data" — DO NOT bail out (the previous behaviour caused
                # a permanent silent skip → mc_samples never grew → MC
                # velocity stayed 0% forever → PumpSwap candidates never
                # passed the seasoned gate → "PumpSwap never gets tapped").
                # Fall through to pool-state-based metrics below.
                c: dict = {}
                try:
                    r = await client.get(f"{url}/{mint}", headers={"accept": "application/json"})
                    if r.status_code == 200 and r.content:
                        try:
                            c = r.json() or {}
                        except Exception:
                            c = {}
                except Exception as e:
                    logger.debug(f"refresh fetch failed for {mint}: {e}")
                usd_mc = float(c.get("usd_market_cap") or 0.0)
                last_trade_ms = int(c.get("last_trade_timestamp") or 0)
                # `complete` flag from the API is authoritative when we have
                # a response; otherwise infer from the bucket's current
                # protocol (set at seed time / by the bot's tracker on
                # graduation observation).
                is_graduated = (
                    bool(c.get("complete"))
                    if c
                    else bucket.get("protocol") == "pumpswap"
                )
                # Resolve current price per protocol
                cur_price = 0.0
                # Helius kill-switch — pool state fetch hits PumpSwap via
                # RPC (Helius credits). Skip when paused; the rest of the
                # refresh (Pump.fun HTTP API, MC update from cached values)
                # is free and continues to run.
                helius_ok = True
                try:
                    from helius_gate import is_helius_paused
                    helius_ok = not is_helius_paused()
                except Exception:
                    pass
                if is_graduated and helius_ok:
                    pool = bucket.get("pumpswap_pool") or c.get("pump_swap_pool") or ""
                    if pool and pool not in cold_pools:
                        try:
                            ps_state = pool_states.get(pool) or await pumpswap.fetch_pool_state(pool)
                            bucket["_pool_read_ts"] = now
                            if ps_state:
                                if not ps_state.get("quote_is_sol", True):
                                    bucket["quote_mint"], bucket["quote_symbol"] = ps_state.get("quote_mint"), ps_state.get("quote_symbol")
                                cur_price = pumpswap.price_sol_per_raw_token(ps_state)
                                # PumpSwap quote_reserves IS the actual WSOL
                                # in the pool — no virtual offset like Pump.fun.
                                # Store as real_sol (not virtual) so the scanner
                                # band gate sees the correct liquidity.
                                bucket["last_real_sol_lamports"] = ps_state["quote_reserves"]
                                bucket["last_vsr_lamports"] = ps_state["quote_reserves"]  # legacy compat
                                # Fallback MC computation when Pump.fun API gave
                                # us nothing (the usual case for graduated tokens).
                                # Pump.fun's standard MC formula is `price * 1B`
                                # (the full token supply). Compute it directly
                                # from pool reserves so the seasoned gate has
                                # live USD data instead of a stale seed value.
                                #   MC_SOL  = (quote / base) * total_supply_raw
                                #           = (quote/base) * 1e15 / 1e9 lamports/SOL
                                #           = quote * 1e6 / base                 (in SOL)
                                #   MC_USD  = MC_SOL * sol_usd
                                # Always prefer the live pool MC: the Pump.fun API's usd_market_cap for graduated coins
                                # lags / freezes, and mixing the two sources made MC velocity read -2 %/5m on a +100 %/h
                                # token (operator report 2026-10-08).
                                if sol_usd > 0:
                                    base = float(ps_state.get("base_reserves") or 0)
                                    quote = float(ps_state.get("quote_reserves") or 0)
                                    if base > 0 and quote > 0:
                                        mc_sol = quote * 1e6 / base
                                        usd_mc = mc_sol * sol_usd
                                        bucket["mc_source"] = "pool"
                                # holder count: PumpSwap swaps never hit the Pump.fun tape, so `buyers` stays empty for
                                # graduated coins — count token accounts via Helius DAS every HOLDERS_REFRESH_S instead
                                if now - float(bucket.get("_holders_ts") or 0) >= HOLDERS_REFRESH_S:
                                    bucket["_holders_ts"] = now
                                    asyncio.create_task(self._refresh_holders(mint, bucket))
                                # Same proxy for `last_trade_timestamp` — when
                                # the API gives us nothing we use NOW (we
                                # successfully fetched live pool state, so
                                # the token is reachable). Inaccurate by up
                                # to the refresh interval (~60s) but bounded.
                                if last_trade_ms <= 0:
                                    last_trade_ms = int(now * 1000)
                        except Exception as e:
                            logger.debug(f"refresh pool fetch failed for {mint}: {e}")
                else:
                    vsr = int(c.get("virtual_sol_reserves") or 0)
                    vtr = int(c.get("virtual_token_reserves") or 0)
                    real_sol = int(c.get("real_sol_reserves") or 0)
                    if vsr and vtr:
                        cur_price = vsr / vtr / LAMPORTS_PER_SOL
                        bucket["last_vsr_lamports"] = vsr
                        # Pump.fun returns real_sol_reserves directly in the
                        # coin doc — prefer it over the legacy vsr-30 estimate.
                        if real_sol > 0:
                            bucket["last_real_sol_lamports"] = real_sol
                # Update bucket — ONLY overwrite when we have a meaningful
                # value. The Pump.fun API returns 0 for usd_market_cap on
                # graduated tokens (or empty body, parsed as 0); never
                # overwrite a healthy seed value with 0.
                if usd_mc > 0:
                    bucket["usd_market_cap"] = usd_mc
                if last_trade_ms > 0:
                    bucket["last_trade_ms"] = last_trade_ms
                # Graduation transition — if the bucket is still tagged
                # pumpfun but the API now reports complete=True, migrate it
                # to pumpswap and stamp `graduated_at` so the scanner's
                # Seasoned band can start counting age from this moment.
                # (Tokens DISCOVERED post-graduation already have these set
                # at seed time — this branch handles tokens we observed
                # pre-grad that flipped mid-tracking.)
                if is_graduated and bucket.get("protocol") != "pumpswap":
                    bucket["protocol"] = "pumpswap"
                    bucket["graduated_at"] = now
                    bucket["curve_fill_pct"] = 100.0
                    # Resolve a pool address now so the scanner doesn't need
                    # to fetch it on the next pass.
                    pool_addr = bucket.get("pumpswap_pool") or c.get("pump_swap_pool") or ""
                    if not pool_addr:
                        try:
                            pool_addr = await pumpswap.find_pool_for_mint(mint) or ""
                        except Exception:
                            pool_addr = ""
                    if pool_addr:
                        bucket["pumpswap_pool"] = pool_addr
                    # Persist `graduated_at` on the launch doc so analytics
                    # and the post-restart scanner have a consistent origin.
                    try:
                        from datetime import datetime, timezone as _tz
                        await st.db.launches.update_one(
                            {"mint": mint, "graduated_at": {"$in": [None, ""]}},
                            {"$set": {
                                "graduated_at": datetime.fromtimestamp(now, _tz.utc).isoformat(),
                            }},
                        )
                    except Exception:
                        pass
                # Refresh social proof fields ONLY when API gave us data.
                # Without this guard, graduated tokens (empty API response)
                # would have their seed socials silently nuked to "" / 0.
                if c:
                    bucket["reply_count"] = int(c.get("reply_count") or 0)
                    bucket["twitter"] = (c.get("twitter") or "").strip()
                    bucket["telegram"] = (c.get("telegram") or "").strip()
                    bucket["website"] = (c.get("website") or "").strip()
                    # Cumulative buyer count from the Pump.fun coin endpoint
                    # (only refreshed when API responded — empty body would
                    # have zeroed this out for graduated tokens). PumpSwap
                    # pools don't generate Helius mempool events, so the
                    # in-memory `buyers` set stays empty — `buy_count` is
                    # one signal we have for "how many people are buying"
                    # on seasoned tokens, though Pump.fun's API doesn't
                    # track post-graduation buys (the value plateaus at
                    # the graduation snapshot).
                    if c.get("buy_count") is not None:
                        bucket["buy_count"] = int(c["buy_count"])
                if cur_price > 0:
                    bucket["last_price_sol"] = cur_price
                # Append rolling MC sample. Skip zero values so the velocity
                # window doesn't get poisoned with a "MC dropped to 0" data
                # point when Pump.fun's API has a transient failure.
                if usd_mc > 0:
                    mc_samples = bucket.setdefault("mc_samples", deque(maxlen=MC_SAMPLE_KEEP))
                    mc_samples.append((now, usd_mc))
                # Append rolling price sample (used by the entry-velocity gate)
                if cur_price > 0:
                    price_samples = bucket.setdefault("price_samples", deque(maxlen=120))
                    price_samples.append((now, cur_price))
                # Be polite to Pump.fun's per-mint endpoint
                await asyncio.sleep(0.15)
        self._evict_quiet(now)
        logger.debug(f"discovery refresh: updated {len(targets)} tokens")

    def _evict_quiet(self, now: float) -> int:
        """Alive-only tracker: a discovered CURVE token with no print for 6× the inflow window (≥ 30 min) is dropped —
        the next discovery cycle re-seeds it the moment it has inflow again. Held / pinned tokens and PumpSwap pools
        (whose last-trade stamp the curve API no longer tracks) are never evicted here."""
        st = self.state
        quiet_s = max(1800.0, 6.0 * float(getattr(st.config, "scanner_recent_inflow_window_s", 300) or 300))
        gone = 0
        for mint, b in list(st.tracking.items()):
            if not b.get("discovered") or b.get("pinned") or mint in st.active_trades or b.get("protocol") == "pumpswap":
                continue
            last = max(float(b.get("last_trade_ms") or 0) / 1000.0, float(b.get("start") or 0))
            if last and now - last > quiet_s:
                st.tracking.pop(mint, None)
                gone += 1
        if gone:
            self.evicted_quiet = getattr(self, "evicted_quiet", 0) + gone
            logger.info(f"discovery: evicted {gone} quiet discovered tokens (no print for {quiet_s / 60:.0f} min)")
        return gone

    async def run_once(self) -> int:
        """Returns the number of newly seeded tokens."""
        st = self.state
        cfg = st.config
        # The pull window is the operator's New-band gate (minus the short pre-band wait the tracker tolerates), widened
        # by the legacy scanner window so nothing the old settings covered is lost.
        max_age_s = max(cfg.scanner_window_hours * 3600, float(cfg.band_new_max_age_min) * 60)
        min_age_s = min(cfg.scanner_min_age_minutes * 60, max(0.0, float(cfg.band_new_min_age_min) * 60 - 120.0))
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
        skipped_idle = 0
        skipped_scope = 0
        max_idle_ms = cfg.scanner_discovery_max_idle_minutes * 60 * 1000
        now_ms = now * 1000
        cands: list[tuple[dict, float, bool]] = []
        for c in coins:
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
            # Graduated tokens trade on PumpSwap AMM. We still want them — they're
            # often the biggest movers — so just tag the protocol.
            is_pumpswap = bool(c.get("complete"))
            # Operator scope: a token outside its band's time gate, on a switched-off book, or with burnt re-entries
            # would be evicted the moment it was seeded — don't seed it.
            if st.scope_reason(mint, {"protocol": "pumpswap" if is_pumpswap else "pumpfun", "start": created_s,
                                      "graduated_at": now if is_pumpswap else None}, now):
                skipped_scope += 1
                continue
            # Freshness pre-filter (cheap): skip tokens whose last curve trade is too stale.
            # Pump.fun's `last_trade_timestamp` only tracks bonding-curve trades — it goes
            # stale the moment the token graduates, so graduated tokens skip this gate.
            if not is_pumpswap and max_idle_ms > 0:
                last_trade_ms = c.get("last_trade_timestamp") or 0
                if not last_trade_ms or now_ms - last_trade_ms > max_idle_ms:
                    skipped_idle += 1
                    continue
            cands.append((c, created_s, is_pumpswap))
        alive_coins, alive_stats = await self._alive_by_inflow([c for c, _, _ in cands])
        alive_mints = {c["mint"] for c in alive_coins}
        for c, created_s, is_pumpswap in cands:
            if seeded >= COINS_PER_CYCLE:
                break
            if c["mint"] not in alive_mints:
                continue
            try:
                await self._seed_token(c, created_s, is_pumpswap)
                b = st.tracking.get(c["mint"])
                if b is not None and "_alive_inflow_sol" in c:
                    b["alive_inflow_sol"] = round(c["_alive_inflow_sol"], 3)
                seeded += 1
            except Exception as e:
                logger.debug(f"seed failed for {c['mint']}: {e}")

        self.last_stats = {"ts": now, "in_band": len(coins), "candidates": len(cands), "seeded": seeded, "skipped_idle": skipped_idle,
                           "skipped_scope": skipped_scope, **alive_stats}
        logger.info(f"discovery: {len(coins)} in band, {len(cands)} candidates, alive {alive_stats['alive']} "
                    f"(inflow ≥ {alive_stats['floor_sol']:g} SOL / {alive_stats['window']}), below floor {alive_stats['below_floor']}, "
                    f"no pair {alive_stats['no_pair']}, seeded {seeded}, skipped_idle {skipped_idle}, out of scope {skipped_scope}")
        if seeded:
            await hub.broadcast("discovery", {"seeded": seeded, "ts": now, **alive_stats})
        return seeded

    async def _alive_by_inflow(self, coins: list[dict]) -> tuple[list[dict], dict]:
        """Alive = buy inflow over the scanner inflow window ≥ `scanner_min_recent_inflow_sol` (the existing gate floor).
        DexScreener pair stats, batched 30 mints per call; a DexScreener outage fails OPEN (legacy freshness-only seeding)."""
        cfg = self.state.config
        win = "m5" if int(getattr(cfg, "scanner_recent_inflow_window_s", 300) or 300) <= 600 else "h1"
        floor_sol = float(getattr(cfg, "scanner_min_recent_inflow_sol", 0.0) or 0.0)
        stats = {"alive": 0, "below_floor": 0, "no_pair": 0, "window": win, "floor_sol": floor_sol, "dex_errors": 0}
        if not coins or floor_sol <= 0:
            stats["alive"] = len(coins)
            return coins, stats
        try:
            from solana_client import get_sol_usd_price
            sol_usd = float(await get_sol_usd_price() or 0.0)
        except Exception:
            sol_usd = 0.0
        if sol_usd <= 0:
            stats["alive"] = len(coins)
            return coins, stats
        by_mint = {c["mint"]: c for c in coins}
        mints = list(by_mint)
        alive: list[dict] = []
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            for i in range(0, len(mints), DEX_BATCH):
                chunk = mints[i:i + DEX_BATCH]
                try:
                    r = await client.get(DEX_URL.format(mints=",".join(chunk)), headers={"accept": "application/json"})
                    r.raise_for_status()
                    pairs = r.json() or []
                except Exception as e:
                    stats["dex_errors"] += 1
                    logger.warning(f"discovery: DexScreener batch failed ({e}); seeding {len(chunk)} on freshness only")
                    alive.extend(by_mint[m] for m in chunk)
                    stats["alive"] += len(chunk)
                    continue
                inflow_usd: dict[str, float] = {}
                for p in pairs if isinstance(pairs, list) else []:
                    m = ((p.get("baseToken") or {}).get("address"))
                    if m not in by_mint:
                        continue
                    tx = (p.get("txns") or {}).get(win) or {}
                    buys, sells = int(tx.get("buys") or 0), int(tx.get("sells") or 0)
                    vol = float((p.get("volume") or {}).get(win) or 0.0)
                    inflow_usd[m] = inflow_usd.get(m, 0.0) + (vol * buys / (buys + sells) if buys + sells else 0.0)
                for m in chunk:
                    if m not in inflow_usd:
                        stats["no_pair"] += 1          # no indexed pair → no volume evidence → not alive
                        continue
                    sol = inflow_usd[m] / sol_usd
                    by_mint[m]["_alive_inflow_sol"] = sol
                    if sol >= floor_sol:
                        alive.append(by_mint[m])
                        stats["alive"] += 1
                    else:
                        stats["below_floor"] += 1
                if i + DEX_BATCH < len(mints):
                    await asyncio.sleep(0.2)
        return alive, stats

    async def _fetch_aged_coins(self, lo_ts: float, hi_ts: float) -> list[dict]:
        """Pull tokens via TWO sort orders and merge:
          1. `last_trade_timestamp DESC` — surfaces actively-traded tokens
             (covers most NEW band candidates).
          2. `market_cap DESC`           — surfaces high-MC and graduated
             tokens whose bonding-curve `last_trade_timestamp` has gone stale
             since they moved to the PumpSwap AMM.
        Filtered to the [lo_ts, hi_ts] creation-time band. Sorting via two
        orders bypasses Pump.fun's ~1000-offset creation-pagination cap and
        ensures the seasoned band sees both active movers AND big-cap names."""
        url = f"{PUMPFUN_API}/coins"
        PAGE_SIZE = 240
        MAX_PAGES_PER_SORT = 5  # 5×240 = ~1200 tokens per sort order
        out: list[dict] = []
        seen: set[str] = set()
        sort_orders = [
            ("last_trade_timestamp", "DESC"),
            ("market_cap", "DESC"),
        ]
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            for sort_field, order in sort_orders:
                offset = 0
                for _ in range(MAX_PAGES_PER_SORT):
                    params = {
                        "offset": offset,
                        "limit": PAGE_SIZE,
                        "sort": sort_field,
                        "order": order,
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
                        logger.warning(f"pumpfun coins API page sort={sort_field} offset={offset} failed: {e}")
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

    async def _refresh_holders(self, mint: str, bucket: dict) -> None:
        """Holder count for a graduated coin via Helius DAS `getTokenAccounts` (paged, capped at 3000)."""
        try:
            from solana_client import rpc_call
            total = 0
            for page in range(1, 4):
                res = await rpc_call("getTokenAccounts", {"mint": mint, "limit": 1000, "page": page, "options": {"showZeroBalance": False}})
                accts = ((res or {}).get("result") or {}).get("token_accounts") or []
                total += len(accts)
                if len(accts) < 1000:
                    break
            if total > 0:
                bucket["holder_count"] = total
                bucket["holder_count_capped"] = total >= 3000
        except Exception as e:
            logger.debug(f"holder count failed for {mint}: {e}")

    async def _seed_token(self, coin: dict, created_s: float, is_pumpswap: bool = False, pool_state: dict | None = None):
        st = self.state
        mint = coin["mint"]
        vsr = int(coin.get("virtual_sol_reserves") or 0)
        vtr = int(coin.get("virtual_token_reserves") or 0)
        usd_mc = float(coin.get("usd_market_cap") or 0.0)
        last_trade_ms = int(coin.get("last_trade_timestamp") or 0)
        pool_quote: tuple = (None, None)                 # (quote_mint, quote_symbol) for non-SOL-paired pools
        pool_address = coin.get("pump_swap_pool") or coin.get("pool_address") or ""

        # Resolve current price per protocol
        if is_pumpswap:
            # Pump.fun API may carry the pool address directly; fall back to
            # finding it via on-chain lookup.
            if not pool_address:
                try:
                    found = await pumpswap.find_pool_for_mint(mint)
                    if found:
                        pool_address = found
                except Exception:
                    pool_address = ""
            cur_price = 0.0
            real_sol_lamports = 0
            if pool_address:
                try:
                    ps_state = pool_state or await pumpswap.fetch_pool_state(pool_address)
                    if ps_state:
                        cur_price = pumpswap.price_sol_per_raw_token(ps_state)
                        real_sol_lamports = ps_state["quote_reserves"]
                        if not ps_state.get("quote_is_sol", True):
                            pool_quote = (ps_state.get("quote_mint"), ps_state.get("quote_symbol"))
                except Exception as e:
                    logger.debug(f"pumpswap pool fetch failed for {mint}: {e}")
        else:
            cur_price = (vsr / vtr / LAMPORTS_PER_SOL) if (vsr and vtr) else 0.0
            # Pump.fun API exposes real_sol_reserves directly; prefer it over
            # the vsr-based estimate so the band gate sees the true SOL pool.
            real_sol_lamports = int(coin.get("real_sol_reserves") or 0) or vsr

        bucket = {
            "launch_id": f"disc-{mint[:8]}",
            "creator": coin.get("creator") or "",
            "start": created_s,
            # For PumpSwap-discovered tokens we don't observe the actual
            # graduation moment — `created_s` is the launch timestamp, which
            # can be many hours/days before the graduation. The Seasoned
            # band's age clock would be massively inflated. Fall back to
            # "now" so freshly discovered post-grad tokens enter the band
            # at age 0 (the high-EV window starts when we SEE them).
            "graduated_at": time.time() if is_pumpswap else None,
            "buyers": set(),
            "buy_events": deque(maxlen=EVENT_KEEP),
            "sol_inflow_lamports": 0,
            # Cumulative buy count from Pump.fun API when it still returns one (the v3 API dropped the field —
            # None = unknown, and the seasoned buyers gate must not treat "unknown" as zero).
            "buy_count": int(coin["buy_count"]) if coin.get("buy_count") is not None else None,
            "curve_fill_pct": (100.0 if is_pumpswap else
                               (min(100.0, max(0.0, (vsr - 30_000_000_000) / 85_000_000_000 * 100)) if vsr else 0.0)),
            "social_score": 0,
            "last_persist": 0.0,
            "name": coin.get("name"),
            "symbol": coin.get("symbol"),
            "creator_rugs": 0,
            # Anchor "first seen" at the universal Pump launch baseline so that
            # growth_pct reflects true chart growth from launch.
            "first_seen_price_sol": LAUNCH_BASELINE_PRICE_SOL,
            "last_price_sol": cur_price,
            # Authoritative real-SOL liquidity (no virtual offset). The
            # scanner band gate reads this directly; falls back to
            # last_vsr_lamports-30 only if missing.
            "last_real_sol_lamports": real_sol_lamports,
            "last_vsr_lamports": real_sol_lamports,
            "quote_mint": pool_quote[0], "quote_symbol": pool_quote[1],
            # Throttled price samples for entry-velocity gate. Discovered
            # tokens populate this via the discovery refresh loop (not the
            # mempool listener, which doesn't reach PumpSwap pools).
            "price_samples": deque(maxlen=120),
            "last_price_sample_ts": 0.0,
            "scanner_eligible": True,
            "scanner_last_attempt": 0.0,
            "discovered": True,
            "bonding_curve": coin.get("bonding_curve") or "",
            "seen_at": time.time(),                        # seed time: a just-seeded token is alive until proven quiet
            "usd_market_cap": usd_mc,
            "last_trade_ms": last_trade_ms,
            "protocol": "pumpswap" if is_pumpswap else "pumpfun",
            "pumpswap_pool": pool_address if is_pumpswap else "",
            # Rolling MC samples (used by scanner._mc_velocity for the
            # seasoned-band gate). Pre-seeded below so the first refresh
            # cycle produces a non-zero velocity instead of having to
            # wait for sample #2.
            "mc_samples": deque(maxlen=MC_SAMPLE_KEEP),
            # Social proof fields (used by gate_socials_required entry gate)
            "reply_count": int(coin.get("reply_count") or 0),
            "twitter": (coin.get("twitter") or "").strip(),
            "telegram": (coin.get("telegram") or "").strip(),
            "website": (coin.get("website") or "").strip(),
        }
        # Seed the rolling sample deques with the values we already have
        # at seed time. Without this, the refresh loop has to do at least
        # 2 cycles (~2 minutes) before `growth_pct_rolling` /
        # `mc_velocity_5m_pct` produce non-zero readings — meaning newly
        # discovered seasoned tokens silently stall for the first 2 mins
        # of life (the user-reported "PumpSwap never gets tapped" symptom).
        if usd_mc > 0:
            bucket["mc_samples"].append((time.time(), usd_mc))
        if cur_price > 0:
            bucket["price_samples"].append((time.time(), cur_price))
        st.tracking[mint] = bucket
        if is_pumpswap:                                  # holders right away — the row would otherwise read "0 buyers" for up to 2 min
            bucket["_holders_ts"] = time.time()
            asyncio.create_task(self._refresh_holders(mint, bucket))
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
        if bucket.get("start"):
            synthetic.detected_at = datetime.fromtimestamp(float(bucket["start"]), timezone.utc)   # real launch time, not when we noticed it
        doc = synthetic.model_dump()
        doc["detected_at"] = doc["detected_at"].isoformat()
        doc["discovered"] = True
        # Skip re-broadcasting if this mint is already in the recent feed.
        # A discovered token can be re-seeded over time (LRU eviction from
        # `tracking` → next discovery cycle re-adds it), and each re-seed
        # would otherwise emit another `launch` WS event with the SAME
        # `disc-{mint8}` id. Frontend dedupes by id now, but skipping the
        # send is cheaper and avoids client churn.
        already_in_feed = any(r.get("mint") == mint for r in st.recent_launches)
        if not already_in_feed:
            st.recent_launches.insert(0, doc)
            st.recent_launches = st.recent_launches[:50]
            await hub.broadcast("launch", doc)
