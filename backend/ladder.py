"""Graduate Ladder — an age-less watchlist of graduated tokens (Sol PumpSwap + RH v4 pools) that keep making
higher market-cap highs, with a paper position that is held and added to on confirmed steps.

Qualification (medium): ≥3 higher-highs, each ≥ +15 % over the previous high and in a distinct 4 h window,
with every pullback between highs ≤ 40 % and holders still growing. Trade: starter at promotion, one add per
confirmed step (pullback ≥ 10 % then reclaim), bank the starter at the first add, per-leg ratchet trail, and a
structure stop (MC < last confirmed high × 0.75, or holders shrinking) that closes everything and puts the token
back to `watching`. Re-entry goes through the universal ReentryLedger (SL cooldown first). Persisted in
`ladder_tokens`; legs are `trades` rows with book="ladder" so history / P&L see them."""
import asyncio
import logging
import time
from datetime import datetime, timezone

from models import Trade
from reentry_policy import ReentryLedger
from ws_hub import hub
import sol_holders

logger = logging.getLogger(__name__)

TICK_S = 5.0
STEP_PCT = 15.0            # a new high must beat the last confirmed high by this much
STEP_WINDOW_S = 4 * 3600   # …and sit in a different 4 h window
MAX_PULLBACK_PCT = 40.0    # deeper than this between highs = pump, staircase resets
ADD_PULLBACK_PCT = 10.0    # an add needs a real dip before the reclaim
STRUCTURE_STOP_PCT = 25.0  # MC below last confirmed high by this much closes the ladder
STEPS_TO_QUALIFY = 3
MANUAL_MAX = 25                # operator-pinned RH tokens (bounded RPC load)
MANUAL_SPOT_EVERY_S = 15.0     # pool spot poll for pinned tokens with no recent swap
STALE_AFTER_S = 600.0          # no price movement/print for this long → row shows "stale" (never removed)
SOURCE_LOST_S = 1800.0         # graduate unseen by either feed for this long (no legs) → dead, dropped from the watch
RUG_DROP_PCT = 85.0            # graduate collapsed this far from its high (no legs) → dead ("rugged"); manual pins are never reaped
PUSH_EVERY_S = 5.0
LADDER_MIN_POOL_SOL = 10.0     # PumpSwap pools shallower than this are dust (a 0.5 SOL pool "prices" a $200M MC) — not a graduate
HOLDERS_EVERY_S = 600.0        # real holder counts (DAS getTokenAccounts) per Sol ladder token
MAX_LEGS = 4
MIN_MC_USD = 50_000.0
LIVE_AFTER_PAPER_LEGS = 15   # ladder legs go through the live executor only once this many paper legs have closed
SAMPLE_EVERY_S = 900.0
PAPER_SLIP_PCT = 1.0
TRAIL = ((60.0, 8.0), (30.0, 12.0), (0.0, 20.0))   # (leg gain ≥ x % → trail y % from the leg peak)


def _win(ts: float) -> int:
    return int(ts // STEP_WINDOW_S)


def _trail_pct(gain_pct: float) -> float:
    for lvl, tr in TRAIL:
        if gain_pct >= lvl:
            return tr
    return TRAIL[-1][1]


class LadderBook:
    def __init__(self, state):
        self.state = state
        self.tokens: dict[str, dict] = {}     # key → ladder doc (in-memory mirror of ladder_tokens)
        self.reentry = ReentryLedger()
        self._last_push, self._last_sig = 0.0, None
        self._idle_since = None
        self._task = None
        self._dirty: set[str] = set()
        self._last_persist: dict[str, float] = {}
        self.stats = {"ticks": 0, "legs_opened": 0, "legs_closed": 0, "paper_legs_closed": 0, "live_legs": 0}
        self._paper_closed_ts = 0.0

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def restore(self):
        try:
            async for d in self.state.db.ladder_tokens.find({"state": {"$ne": "dead"}}):
                d.pop("_id", None)
                self.tokens[d["key"]] = d
        except Exception as e:
            logger.debug(f"ladder restore failed: {e}")
        for d in list(self.tokens.values()):                       # operator pins survive restarts
            if d.get("manual"):
                try:
                    if d["chain"] == "rh":
                        await self.state.rh_discovery.pin_manual(d["mint"], d.get("price_unit"))
                    else:
                        await self.pin_manual_sol(d["mint"], d.get("pool") or None)
                except Exception as e:
                    logger.warning(f"ladder re-pin {d.get('symbol') or d['mint']} failed: {e}")

    async def _loop(self):
        await self.restore()
        while True:
            try:
                await asyncio.sleep(TICK_S)
                if not getattr(self.state.config, "ladder_enabled", True) or not self.state.config.enabled:
                    self._idle_since = self._idle_since or time.time()
                    continue
                now = time.time()
                if self._idle_since and now - self._idle_since > 60.0:
                    for d in self.tokens.values():                  # a stop/restart is not a rug: every graduate gets a fresh 30-min window
                        d["last_seen"] = max(float(d.get("last_seen") or 0), now)
                self._idle_since = None
                await self._refresh_paper_count(now)
                await self._refresh_manual_spots(now)
                await self._refresh_sol_holders(now)
                for key, src in self._sources():
                    await self.observe(key, src, now)
                await self._reap(now)
                await self._persist(now)
                await self._push(now)
                self.stats["ticks"] += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"ladder tick error: {e}")

    # ------------------------------------------------------------------ feeds
    def _sources(self):
        """(key, snapshot) for every graduated token both feeds currently price."""
        st = self.state
        for mint, b in list(getattr(st, "tracking", {}).items()):
            if (b.get("protocol") or "pumpfun") != "pumpswap":
                continue
            mc = float(b.get("usd_market_cap") or 0.0)
            if mc <= 0:
                continue
            depth = float(b.get("last_real_sol_lamports") or 0) / 1e9
            if 0 < depth < LADDER_MIN_POOL_SOL and not b.get("manual"):
                continue                                            # dust pool → fake MC; skip until real liquidity shows up
            yield f"sol:{mint}", {"chain": "sol", "protocol": "pumpswap", "mint": mint, "symbol": b.get("symbol"), "name": b.get("name"),
                                  "mc": mc, "holders": b.get("holders_real"), "pool": b.get("pumpswap_pool") or "", "manual": bool(b.get("manual")),
                                  "last_print_ts": float(b.get("last_spot_ts") or b.get("_pool_read_ts") or 0) or None,
                                  "price": float(b.get("last_price_sol") or 0.0), "price_unit": "SOL"}
        rd = getattr(st, "rh_discovery", None)
        for token, b in list((getattr(rd, "tracking", None) or {}).items()):
            if not b.get("graduated") or not b.get("pool_live"):
                continue
            mc = float(b.get("usd_market_cap") or 0.0)
            if mc <= 0:
                continue
            yield f"rh:{token}", {"chain": "rh", "protocol": "pons", "mint": token, "symbol": b.get("symbol"), "name": b.get("name"),
                                  "mc": mc, "holders": len(b.get("buyers") or ()), "pool": "", "manual": bool(b.get("manual")),
                                  "last_print_ts": max(float(b.get("last_pool_swap_ts") or 0), float(b.get("last_spot_ts") or 0)),
                                  "price": float(b.get("last_price_quote") or 0.0), "price_unit": b.get("quote_symbol") or "ETH"}

    async def _refresh_manual_spots(self, now: float):
        rd = getattr(self.state, "rh_discovery", None)
        for token, b in list((getattr(rd, "tracking", None) or {}).items()) if rd is not None else []:
            if not b.get("pinned"):
                continue
            last = max(float(b.get("last_pool_swap_ts") or 0), float(b.get("last_spot_ts") or 0))
            if now - last >= MANUAL_SPOT_EVERY_S:
                await rd.refresh_manual_spot(token, now)
        for mint, b in list(getattr(self.state, "tracking", {}).items()):
            if b.get("pinned") and now - float(b.get("last_spot_ts") or 0) >= MANUAL_SPOT_EVERY_S:
                await self._refresh_sol_pool(mint, b, now)

    async def _refresh_sol_pool(self, mint: str, b: dict, now: float) -> bool:
        """PumpSwap pool read for a pinned Sol mint: price, decimals-aware MC (real supply), depth, print timestamp."""
        import pumpswap, solana_client
        pool = b.get("pumpswap_pool")
        if not pool:
            return False
        try:
            st = await pumpswap.fetch_pool_state(pool)
            sol_usd = await solana_client.get_sol_usd_price()
        except Exception as e:
            logger.debug(f"sol pin pool read {mint[:8]}: {e}")
            return False
        if not st:
            return False
        price = pumpswap.price_sol_per_raw_token(st)
        if price <= 0:
            return False
        supply = await sol_holders.token_supply(mint) or 1_000_000_000.0
        b["last_price_sol"], b["last_spot_ts"], b["_pool_read_ts"] = price, now, now
        b["last_real_sol_lamports"] = b["last_vsr_lamports"] = int(st.get("quote_reserves") or 0)
        if sol_usd > 0:
            b["usd_market_cap"] = price * (10 ** int(st.get("base_decimals") or 6)) * supply * sol_usd
        return True

    async def _refresh_sol_holders(self, now: float):
        """Real holder counts for every Sol token on the ladder board (10-min cadence, ≤ 5 DAS pages each)."""
        for d in list(self.tokens.values()):
            if d["chain"] != "sol" or now - float(d.get("_holders_ts") or 0) < HOLDERS_EVERY_S:
                continue
            d["_holders_ts"] = now
            res = await sol_holders.holder_count(d["mint"], now)
            if res is None:
                continue
            count, capped = res
            d["holders"], d["holders_capped"] = count, capped
            b = getattr(self.state, "tracking", {}).get(d["mint"])
            if b is not None:
                b["holders_real"] = count

    # ------------------------------------------------------------------ operator pins
    def manual_count(self) -> int:
        return sum(1 for d in self.tokens.values() if d.get("manual"))

    @staticmethod
    def chain_of(address: str) -> str:
        a = address.strip()
        if a.lower().startswith("0x") and len(a) == 42:
            return "rh"
        if 32 <= len(a) <= 44 and all(c in "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz" for c in a):
            return "sol"
        raise ValueError("not an EVM address (0x…) or a Solana mint (base58)")

    async def pin_manual_sol(self, mint: str, pool: str | None = None) -> dict:
        """Operator pins a PumpSwap token: locate (or take) its pool, seed a pinned bucket in the Sol tracker, price it now."""
        import pumpswap, httpx
        from collections import deque
        from solders.pubkey import Pubkey
        Pubkey.from_string(mint)
        if pool:
            Pubkey.from_string(pool)
            st = await pumpswap.fetch_pool_state(pool)
            if not st:
                raise ValueError("that PumpSwap pool address does not decode / has no reserves")
            if st.get("base_mint") != mint:
                raise ValueError(f"pool base mint is {st.get('base_mint', '?')[:8]}…, not this token")
        else:
            try:
                pool = await asyncio.wait_for(pumpswap.find_pool_for_mint(mint), timeout=12.0)
            except Exception:
                pool = None
            if not pool:
                raise ValueError("no PumpSwap pool found for this mint (pass the pool address if you know it)")
        tracking = self.state.tracking
        b = tracking.get(mint)
        now = time.time()
        if b is None:
            b = tracking[mint] = {
                "launch_id": None, "creator": "", "start": now, "protocol": "pumpswap", "pumpswap_pool": pool,
                "graduated_at": now, "buyers": set(), "buy_events": deque(maxlen=500), "sol_inflow_lamports": 0,
                "buy_count": None, "curve_fill_pct": 100.0, "social_score": 0, "project_score": 0, "project_flags": {},
                "last_persist": 0.0, "name": None, "symbol": None, "creator_rugs": 0, "first_seen_price_sol": 0.0,
                "last_price_sol": 0.0, "price_samples": deque(maxlen=120), "last_price_sample_ts": 0.0,
                "scanner_eligible": False, "scanner_last_attempt": 0.0, "manual_seed": True,
            }
        b.update(protocol="pumpswap", pumpswap_pool=pool, manual=True, pinned=True, pinned_at=b.get("pinned_at") or now, scanner_eligible=False)
        if not await self._refresh_sol_pool(mint, b, now):
            raise ValueError("pool has no price yet")
        if not b.get("symbol"):
            try:
                async with httpx.AsyncClient(timeout=6.0) as c:
                    pairs = ((await c.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}")).json() or {}).get("pairs") or []
                if pairs:
                    b["symbol"], b["name"] = pairs[0]["baseToken"].get("symbol"), pairs[0]["baseToken"].get("name")
            except Exception as e:
                logger.debug(f"dexscreener meta {mint[:8]}: {e}")
        return {"token": mint, "symbol": b.get("symbol"), "name": b.get("name"), "quote_symbol": "SOL", "pair_token": pool,
                "price_quote": b["last_price_sol"], "usd_market_cap": b.get("usd_market_cap") or 0.0, "quote_priced": True,
                "pool_depth_sol": float(b.get("last_real_sol_lamports") or 0) / 1e9}

    async def add_manual(self, address: str, quote: str | None = None) -> dict:
        """Pin an established token (RH 0x… or Solana mint): no age gate, no MC gate, staircase + retry exactly like graduates.
        `quote` = RH pool quote (ETH / stock symbol / pair address) or, for Solana, the PumpSwap pool address."""
        chain = self.chain_of(address)
        addr = address.strip().lower() if chain == "rh" else address.strip()
        key = f"{chain}:{addr}"
        if key not in self.tokens and self.manual_count() >= MANUAL_MAX:
            raise ValueError(f"manual ladder list is full ({MANUAL_MAX})")
        info = await (self.pin_manual_sol(addr, quote or None) if chain == "sol" else self.state.rh_discovery.pin_manual(addr, quote))
        now = time.time()
        src = next((s for k, s in self._sources() if k == key), None)
        if src is None:                                             # quote has no USD price yet → still register, MC fills in later
            src = {"chain": chain, "protocol": "pumpswap" if chain == "sol" else "pons", "mint": info["token"], "symbol": info["symbol"], "name": info["name"],
                   "mc": 0.0, "holders": None, "pool": info["pair_token"] if chain == "sol" else "", "manual": True, "last_print_ts": now,
                   "price": info["price_quote"], "price_unit": info["quote_symbol"]}
        await self.observe(key, src, now)
        d = self.tokens[key]
        d["manual"], d["pinned_at"] = True, d.get("pinned_at") or now
        if chain == "sol":
            res = await sol_holders.holder_count(d["mint"], now)     # real count right away, not on the next 10-min tick
            if res is not None:
                d["holders"], d["holders_capped"], d["_holders_ts"] = res[0], res[1], now
        self._dirty.add(key)
        await self._persist(now)
        return {**info, "key": key, "state": d["state"], "mc": d["mc"]}

    async def remove_manual(self, key: str) -> dict:
        d = self.tokens.get(key)
        if d is None or not d.get("manual"):
            return {"removed": False, "reason": "not a manual token"}
        if d.get("legs"):
            return {"removed": False, "reason": f"{len(d['legs'])} leg(s) open — close them first"}
        if d["chain"] == "rh":
            self.state.rh_discovery.unpin_manual(d["mint"])
        else:
            b = getattr(self.state, "tracking", {}).get(d["mint"])
            if b:
                b["pinned"] = b["manual"] = False
        self.tokens.pop(key, None)
        await self.state.db.ladder_tokens.update_one({"key": key}, {"$set": {"state": "dead", "manual": False}})
        return {"removed": True}

    # ------------------------------------------------------------------ staircase
    async def observe(self, key: str, src: dict, now: float):
        d = self.tokens.get(key)
        if d is None:
            if src["mc"] < MIN_MC_USD and not src.get("manual"):
                return
            d = self.tokens[key] = {"key": key, **{k: src[k] for k in ("chain", "protocol", "mint", "symbol", "name", "pool", "price_unit")},
                                    "manual": bool(src.get("manual")),
                                    "state": "watching", "first_seen": now, "samples": [], "steps": [], "base": {"ts": now, "mc": src["mc"]},
                                    "high": src["mc"], "high_ts": now,
                                    "min_since_high": src["mc"], "holders_at_first_step": None, "holders": src.get("holders") or 0, "legs": [],
                                    "closed_legs": 0, "realized_usd": 0.0, "reentry": {"attempts": 0, "last_exit_ts": None, "last_exit_reason": None,
                                                                                        "peak_after_exit": None, "trough_after_exit": None}}
        mc = src["mc"]
        if src.get("last_print_ts"):
            d["last_print_ts"] = src["last_print_ts"]
        if mc <= 0:                                                 # pinned token whose quote has no USD price yet — wait, never drop
            d["last_seen"] = now
            return
        d.setdefault("base", {"ts": float(d.get("first_seen") or now), "mc": float(d.get("high") or mc)})   # docs persisted before `base` existed
        if not d.get("high"):
            d["high"], d["high_ts"], d["min_since_high"], d["base"] = mc, now, mc, {"ts": now, "mc": mc}
        d["mc"], d["last_seen"] = mc, now
        if src.get("holders") is not None:
            d["holders"] = src["holders"]                          # None = unknown (PumpSwap emits no buy events) — keep the last real count
        d.setdefault("holders", 0)
        d["ath"] = max(float(d.get("ath") or 0.0), mc)                # never reset (the staircase `high` is) — rug detection reads this
        d["symbol"], d["name"] = src.get("symbol") or d.get("symbol"), src.get("name") or d.get("name")
        if not d["samples"] or now - d["samples"][-1][0] >= SAMPLE_EVERY_S or mc > d["high"]:
            d["samples"].append((round(now), round(mc)))
            d["samples"] = d["samples"][-2000:]
        rx = d["reentry"]
        if rx.get("last_exit_ts"):
            rx["peak_after_exit"] = max(rx.get("peak_after_exit") or 0.0, mc)
            rx["trough_after_exit"] = min(rx.get("trough_after_exit") or mc, mc)
        # pullback tracking
        if mc < d["min_since_high"]:
            d["min_since_high"] = mc
        pullback = (d["high"] - d["min_since_high"]) / d["high"] * 100.0 if d["high"] > 0 else 0.0
        if pullback > MAX_PULLBACK_PCT and (d["steps"] or d["base"]["mc"] != d["high"]):
            d["steps"], d["holders_at_first_step"] = [], None       # too deep — this was a pump, start the staircase over
            d["base"] = {"ts": now, "mc": mc}
            d["high"], d["high_ts"], d["min_since_high"] = mc, now, mc
            d["state"] = "holding" if d["legs"] else "watching"
        if mc > d["high"]:
            ref = d["steps"][-1] if d["steps"] else d["base"]        # every step beats the prior confirmed high (or the base)
            confirmed = mc >= ref["mc"] * (1 + STEP_PCT / 100.0) and _win(now) != _win(ref["ts"])
            if confirmed:
                d["steps"].append({"ts": now, "mc": mc, "pullback_before_pct": round(pullback, 1)})
                d["steps"] = d["steps"][-20:]
                if d["holders_at_first_step"] is None and src.get("holders") is not None:
                    d["holders_at_first_step"] = src["holders"]
                await self._on_step(key, d, src, now, pullback)
            d["high"], d["high_ts"], d["min_since_high"] = mc, now, mc
        h = src.get("holders")
        d["qualified"] = len(d["steps"]) >= STEPS_TO_QUALIFY and (h is None or h >= (d["holders_at_first_step"] or 0))
        if d["qualified"] and d["state"] == "watching" and not d["legs"]:
            await self._try_start(key, d, src, now)
        if d["legs"]:
            await self._manage_legs(key, d, src, now)
            self._push_leg_pnl(d, now)
        self._dirty.add(key)

    def _push_leg_pnl(self, d: dict, now: float):
        """Paper ladder legs live outside the engine's monitor, so the Active Trades table would show '—' forever
        without this: MC-based unrealized P/L per open leg over WS (throttled 2 s/token)."""
        if now - float(d.get("_pnl_push_ts") or 0) < 2.0:
            return
        d["_pnl_push_ts"] = now
        for leg in d["legs"]:
            if leg.get("live") or not leg.get("id") or not leg.get("entry_mc"):
                continue
            asyncio.create_task(hub.broadcast("trade_update", {"id": leg["id"], "mint": d["mint"], **self.leg_pnl(d, leg)}))

    @staticmethod
    def leg_pnl(d: dict, leg: dict) -> dict:
        mc, entry, peak = float(d.get("mc") or 0), float(leg.get("entry_mc") or 0), float(leg.get("peak_mc") or 0)
        if mc <= 0 or entry <= 0:
            return {}
        return {"unrealized_pnl_pct": round((mc / entry - 1.0) * 100.0, 2), "live_usd_market_cap": mc,
                "drawdown_from_peak_pct": round((peak - mc) / peak * 100.0, 1) if peak > 0 else None}

    def augment_trade(self, doc: dict) -> None:
        """REST fallback for /api/trades/active — same numbers the WS pushes."""
        for d in self.tokens.values():
            for leg in d["legs"]:
                if leg.get("id") == doc.get("id"):
                    doc.update(self.leg_pnl(d, leg))
                    return

    # ------------------------------------------------------------------ live routing
    async def _refresh_paper_count(self, now: float):
        if now - self._paper_closed_ts < 60.0:
            return
        self._paper_closed_ts = now
        try:
            self.stats["paper_legs_closed"] = await self.state.db.trades.count_documents({"book": "ladder", "mode": "paper", "status": "closed"})
        except Exception:
            pass

    def live_ready(self, chain: str) -> bool:
        """Live executor only after the paper book proved the ladder AND that chain's live switch is armed."""
        cfg = self.state.config
        armed = bool(getattr(cfg, "rh_live_trading", False)) if chain == "rh" else bool(getattr(cfg, "live_trading", False))
        return armed and int(self.stats.get("paper_legs_closed") or 0) >= LIVE_AFTER_PAPER_LEGS

    async def _open_live_leg(self, d: dict, src: dict, now: float, kind: str) -> bool:
        """Route through the venue's live executor: Sol → runner book (no clock, trails from promotion); RH → rh_pons live.
        The engine owns that leg's exits (SL / ratchet trail / rip-cords); the ladder only adds its structure stop."""
        try:
            if d["chain"] == "rh":
                res = await self.state.rh_paper.manual_enter(d["mint"])
            else:
                res = await self.state.manual_enter(d["mint"], as_runner=True)
        except Exception as e:
            res = {"ok": False, "reason": str(e)[:120]}
        if not res.get("ok"):
            d["gate"] = f"live: {res.get('reason', 'refused')}"[:60]
            logger.info(f"LADDER live {kind} refused for {d.get('symbol')}: {res.get('reason')}")
            return False
        d["legs"].append({"id": None, "kind": kind, "usd": None, "entry_mc": src["mc"], "peak_mc": src["mc"], "ts": now, "live": True})
        self.stats["legs_opened"] += 1
        self.stats["live_legs"] += 1
        logger.info(f"LADDER LIVE {kind.upper()} {d.get('symbol')} [{d['chain']}] via {res.get('book') or 'rh_pons'} @ MC ${src['mc']:,.0f}")
        return True

    async def _close_live_leg(self, d: dict, leg: dict, reason: str):
        try:
            if d["chain"] == "rh":
                await self.state.rh_paper.exit(d["mint"], f"ladder {reason}")
            else:
                await self.state._exit(d["mint"], reason=f"ladder {reason}")
        except Exception as e:
            logger.warning(f"ladder live exit failed {d.get('symbol')}: {e}")
        d["legs"] = [x for x in d["legs"] if x is not leg]
        d["closed_legs"] += 1
        self.stats["legs_closed"] += 1

    # ------------------------------------------------------------------ legs
    def _base_usd(self, d: dict) -> float:
        cfg = self.state.config
        base = float(getattr(cfg, "rh_max_trade_usd", 5.0) if d["chain"] == "rh" else getattr(cfg, "max_trade_usd", 10.0))
        return max(1.0, base * float(getattr(cfg, "ladder_size_mult", 0.5) or 0.5))

    async def _try_start(self, key: str, d: dict, src: dict, now: float):
        blk, mult = self.reentry.check(key, self.state.config, now)
        if blk:
            d["gate"] = blk
            return
        d["gate"] = None
        await self._open_leg(key, d, src, now, self._base_usd(d) * 0.5 * (mult or 1.0), "starter")
        d["state"] = "holding"
        if mult is not None:
            self.reentry.record_attempt(key)
            d["reentry"]["attempts"] += 1

    async def _on_step(self, key: str, d: dict, src: dict, now: float, pullback: float):
        if d["state"] != "holding" or not d["legs"]:
            return
        if pullback < ADD_PULLBACK_PCT or len(d["legs"]) >= MAX_LEGS:
            return
        blk, _ = self.reentry.check(key, self.state.config, now)
        if blk == "sl-cooldown":
            return
        if not any(leg["kind"] == "add" for leg in d["legs"]):
            starter = next((leg for leg in d["legs"] if leg["kind"] == "starter"), None)
            if starter:
                await self._close_leg(key, d, starter, src, now, "banked at first add")   # house money from here on
        await self._open_leg(key, d, src, now, self._base_usd(d), "add")

    async def _open_leg(self, key: str, d: dict, src: dict, now: float, usd: float, kind: str):
        if self.live_ready(d["chain"]):
            await self._open_live_leg(d, src, now, kind)
            return
        mc_fill = src["mc"] * (1 + PAPER_SLIP_PCT / 100.0)
        trade = Trade(mint=d["mint"], name=d.get("name"), symbol=d.get("symbol"), mode="paper", entry_usd=usd, book="ladder",
                      protocol=d["protocol"], chain=d["chain"], classifier_action="ladder", risk_score=40,
                      entry_price_sol=src["price"] if d["chain"] == "sol" else 0.0,
                      entry_price_quote=src["price"] if d["chain"] == "rh" else 0.0, quote_symbol=src.get("price_unit"))
        doc = trade.model_dump()
        doc.update(entry_mc_usd=mc_fill, ladder_leg=kind, ladder_step=len(d["steps"]), size_planned_usd=usd,
                   entry_ctx={"band": "ladder", "steps": len(d["steps"]), "mc": src["mc"], "holders": src.get("holders")})
        try:
            await self.state.db.trades.insert_one(dict(doc))
        except Exception as e:
            logger.warning(f"ladder leg insert failed: {e}")
        d["legs"].append({"id": trade.id, "kind": kind, "usd": usd, "entry_mc": mc_fill, "peak_mc": mc_fill, "ts": now})
        self.stats["legs_opened"] += 1
        logger.info(f"LADDER {kind.upper()} {d.get('symbol')} [{d['chain']}] ${usd:.2f} @ MC ${mc_fill:,.0f} (step {len(d['steps'])})")

    async def _close_leg(self, key: str, d: dict, leg: dict, src: dict, now: float, reason: str):
        if leg.get("live"):
            await self._close_live_leg(d, leg, reason)
            return (src["mc"] / leg["entry_mc"] - 1.0) * 100.0
        mc_fill = src["mc"] * (1 - PAPER_SLIP_PCT / 100.0)
        pnl_pct = (mc_fill / leg["entry_mc"] - 1.0) * 100.0
        pnl_usd = leg["usd"] * pnl_pct / 100.0
        try:
            await self.state.db.trades.update_one({"id": leg["id"]}, {"$set": {
                "status": "closed", "exit_time": datetime.now(timezone.utc), "exit_reason": reason, "exit_usd": leg["usd"] + pnl_usd,
                "exit_mc_usd": mc_fill, "pnl_usd": pnl_usd, "pnl_pct": pnl_pct, "peak_pnl_pct": (leg["peak_mc"] / leg["entry_mc"] - 1.0) * 100.0}})
        except Exception as e:
            logger.warning(f"ladder leg close failed: {e}")
        d["legs"] = [x for x in d["legs"] if x["id"] != leg["id"]]
        d["closed_legs"] += 1
        d["realized_usd"] = float(d.get("realized_usd") or 0.0) + pnl_usd
        self.stats["legs_closed"] += 1
        logger.info(f"LADDER EXIT {d.get('symbol')} [{d['chain']}] {leg['kind']} {reason} pnl={pnl_pct:+.1f}% (${pnl_usd:+.2f})")
        return pnl_pct

    async def _manage_legs(self, key: str, d: dict, src: dict, now: float):
        mc = src["mc"]
        last_high = d["steps"][-1]["mc"] if d["steps"] else d["high"]
        holders_shrinking = d.get("holders_at_first_step") is not None and src.get("holders") is not None and src["holders"] < d["holders_at_first_step"] * 0.9
        if mc < last_high * (1 - STRUCTURE_STOP_PCT / 100.0) or holders_shrinking:
            reason = "structure stop: holders shrinking" if holders_shrinking else f"structure stop: MC {((mc / last_high) - 1) * 100:+.0f}% vs last confirmed high"
            worst = 0.0
            for leg in list(d["legs"]):
                worst = min(worst, await self._close_leg(key, d, leg, src, now, reason))
            self._after_full_exit(key, d, now, reason, worst, was_sl=True)
            return
        for leg in list(d["legs"]):
            leg["peak_mc"] = max(leg["peak_mc"], mc)
            if leg.get("live"):
                if not self._live_leg_open(d):            # the engine closed it (its own SL / trail / rip-cord)
                    d["legs"] = [x for x in d["legs"] if x is not leg]
                    d["closed_legs"] += 1
                continue
            gain = (leg["peak_mc"] / leg["entry_mc"] - 1.0) * 100.0
            if mc <= leg["peak_mc"] * (1 - _trail_pct(gain) / 100.0) and gain > 0:
                await self._close_leg(key, d, leg, src, now, f"ratchet trail ({_trail_pct(gain):g}% from peak +{gain:.0f}%)")
        if not d["legs"]:
            self._after_full_exit(key, d, now, "all legs trailed out", 0.0, was_sl=False)

    def _live_leg_open(self, d: dict) -> bool:
        if d["chain"] == "rh":
            return d["mint"] in getattr(self.state.rh_paper, "positions", {})
        return d["mint"] in getattr(self.state, "active_trades", {})

    def _after_full_exit(self, key: str, d: dict, now: float, reason: str, pnl_pct: float, was_sl: bool):
        d["state"] = "watching"
        d["steps"], d["holders_at_first_step"] = [], None        # must print a fresh staircase before the next starter
        d["base"] = {"ts": now, "mc": d["mc"]}
        d["high"], d["high_ts"], d["min_since_high"] = d["mc"], now, d["mc"]
        d["reentry"].update(last_exit_ts=now, last_exit_reason=reason, peak_after_exit=d["mc"], trough_after_exit=d["mc"])
        self.reentry.record_exit(key, pnl_pct, self.state.config, was_sl=was_sl, now=now)

    # ------------------------------------------------------------------ persistence / api
    async def _reap(self, now: float):
        """Graduates that rugged or vanished from both feeds sit at a frozen MC forever otherwise. Operator pins are exempt
        (they go 'stale', never away); anything with an open leg waits for the structure stop to close it first."""
        for key, d in list(self.tokens.items()):
            if d.get("manual") or d.get("legs"):
                continue
            reason = None
            ath = float(d.get("ath") or d.get("high") or 0)
            if ath >= MIN_MC_USD and float(d.get("mc") or 0) < ath * (1 - RUG_DROP_PCT / 100.0):
                reason = "rugged"
            elif now - float(d.get("last_seen") or d.get("first_seen") or now) > SOURCE_LOST_S:
                reason = "source lost"
            if not reason:
                continue
            d["state"], d["dead_reason"], d["dead_at"] = "dead", reason, now
            self.tokens.pop(key, None)
            self._dirty.discard(key)
            self.stats["reaped"] = self.stats.get("reaped", 0) + 1
            logger.info(f"LADDER DROP {d.get('symbol')} [{d['chain']}] {reason} (MC ${float(d.get('mc') or 0):,.0f} / ath ${ath:,.0f})")
            try:
                await self.state.db.ladder_tokens.update_one({"key": key}, {"$set": {"state": "dead", "dead_reason": reason, "dead_at": now}}, upsert=True)
            except Exception as e:
                logger.debug(f"ladder reap persist failed {key}: {e}")

    def payload(self) -> dict:
        """What the UI renders (REST and WS share it)."""
        cfg = self.state.config
        return {"tokens": self.snapshot(), "stats": self.stats,
                "enabled": bool(getattr(cfg, "ladder_enabled", True)), "size_mult": float(getattr(cfg, "ladder_size_mult", 0.5) or 0.5),
                "live": {"sol": self.live_ready("sol"), "rh": self.live_ready("rh"),
                         "paper_legs_closed": int(self.stats.get("paper_legs_closed") or 0), "needed": LIVE_AFTER_PAPER_LEGS}}

    async def _push(self, now: float):
        """WS push of the ladder board every tick — the UI stops polling and rugged/dropped rows disappear at once."""
        if now - self._last_push < PUSH_EVERY_S:
            return
        self._last_push = now
        p = self.payload()
        sig = tuple((r["key"], round(r["mc"] or 0), r["state"], r["steps"], len(r["legs"]), r["stale"]) for r in p["tokens"])
        if sig == self._last_sig:
            return
        self._last_sig = sig
        await hub.broadcast("ladder", p)

    async def _persist(self, now: float):
        for key in list(self._dirty):
            if now - self._last_persist.get(key, 0.0) < 15.0:
                continue
            self._dirty.discard(key)
            self._last_persist[key] = now
            d = self.tokens.get(key)
            if not d:
                continue
            try:
                await self.state.db.ladder_tokens.update_one({"key": key}, {"$set": d}, upsert=True)
            except Exception as e:
                logger.debug(f"ladder persist failed {key}: {e}")

    def snapshot(self) -> list[dict]:
        now = time.time()
        out = []
        for d in self.tokens.values():
            steps = d.get("steps") or []
            out.append({k: d.get(k) for k in ("key", "chain", "protocol", "mint", "symbol", "name", "state", "qualified", "mc", "holders",
                                              "high", "gate", "closed_legs", "realized_usd", "reentry", "first_seen", "last_seen")}
                       | {"manual": bool(d.get("manual")), "price_unit": d.get("price_unit"),
                          "stale": now - float(d.get("last_print_ts") or d.get("last_seen") or now) > STALE_AFTER_S,
                          "steps": len(steps), "last_step_mc": steps[-1]["mc"] if steps else None,
                          "drawdown_pct": round((d["high"] - d["min_since_high"]) / d["high"] * 100.0, 1) if d.get("high") else 0.0,
                          "legs": [{**leg, "pnl_pct": round((d["mc"] / leg["entry_mc"] - 1.0) * 100.0, 1)} for leg in d.get("legs") or []],
                          "age_h": round((now - float(d.get("first_seen") or now)) / 3600.0, 1)})
        out.sort(key=lambda r: (r["state"] == "holding", r["steps"], r["mc"]), reverse=True)
        return out
