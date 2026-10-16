"""
Smart wallets ("top trader buys as a trigger").

Two sources feed ONE set of smart wallets:
  manual   operator-pasted addresses (UI list), always smart
  learned  wallets the tape keeps catching EARLY on tokens that then run — graded per launch from the bucket's
           first-K buyers: a buy is a WIN when the post-buy peak ≥ +min_gain % (or the token graduates), else a MISS.
           A wallet is PROMOTED while wins ≥ min_wins AND win-rate ≥ min_win_rate (demoted automatically).

A smart buy on a token INSIDE the operator's band window (scanner.classify_band) acts per `smart_wallet_mode`:
  hard  → `_enter(action="smart_wallet")` right away (hunt book; momentum gates bypassed, safety gates kept)
  soft  → the bucket is stamped `smart_hits`; the scanner relaxes its gates by `smart_wallet_gate_relax_pct` and
          adds `smart_wallet_soft_boost` rank points while the hit is fresh
  both  → hard trigger + soft stamp (the stamp also helps when the hard path is refused, e.g. hunt-cap)
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import TYPE_CHECKING

from pymongo import UpdateOne

if TYPE_CHECKING:
    from bot import BotState

logger = logging.getLogger("smart_wallets")

LAMPORTS_PER_SOL = 1_000_000_000
EARLY_K = 20                   # first-K distinct buyers per launch are graded
LEARN_MIN_BUY_SOL = 0.05       # dust buys never earn a grade
GRADE_TICK_S = 20.0
FLUSH_EVERY_S = 60.0
MAX_LEARNED = 20_000
RECENT_EVENTS = 60


class SmartWallets:
    def __init__(self, state: "BotState"):
        self.state = state
        self.manual: dict[str, dict] = {}
        self.learned: dict[str, dict] = {}
        self.fires: deque = deque()
        self.recent: deque = deque(maxlen=RECENT_EVENTS)
        self.stats = {"hits": 0, "fired": 0, "opened": 0, "refused": 0, "parked": 0, "out_of_window": 0, "rate_capped": 0,
                      "graded_buys": 0, "graded_launches": 0, "wins": 0, "misses": 0}
        self._dirty: set[str] = set()
        self._task: asyncio.Task | None = None
        self._last_flush = 0.0

    # ---------- config ----------
    def _cfg(self, key: str, default):
        return getattr(self.state.config, key, default)

    def enabled(self) -> bool:
        return bool(self._cfg("smart_wallet_enabled", True))

    def mode(self) -> str:
        m = str(self._cfg("smart_wallet_mode", "both") or "both")
        return m if m in ("hard", "soft", "both") else "both"

    # ---------- persistence ----------
    async def load(self) -> None:
        self.manual.clear()
        self.learned.clear()
        async for d in self.state.db.smart_wallets.find({}, {"_id": 1, "wallet": 1, "kind": 1, "label": 1, "added_at": 1, "wins": 1,
                                                           "misses": 1, "last_seen": 1, "last_win": 1, "best_gain_pct": 1}):
            w = d.get("wallet") or d.pop("_id")
            if d.get("kind") == "manual":
                self.manual[w] = {"label": d.get("label") or "", "added_at": d.get("added_at")}
            else:
                self.learned[w] = {"wins": int(d.get("wins") or 0), "misses": int(d.get("misses") or 0),
                                   "last_seen": float(d.get("last_seen") or 0), "last_win": d.get("last_win"),
                                   "best_gain_pct": float(d.get("best_gain_pct") or 0)}
        logger.info(f"smart wallets: {len(self.manual)} manual · {len(self.learned)} learned ({self.promoted_count()} promoted)")

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def _flush(self) -> None:
        if not self._dirty:
            return
        ops = []
        for w in list(self._dirty):
            s = self.learned.get(w)
            if s is not None:
                ops.append(UpdateOne({"_id": f"learned:{w}"}, {"$set": {"kind": "learned", "wallet": w, **s}}, upsert=True))
        self._dirty.clear()
        if ops:
            try:
                await self.state.db.smart_wallets.bulk_write(ops, ordered=False)
            except Exception as e:
                logger.debug(f"smart wallets flush: {e}")

    async def add_manual(self, wallet: str, label: str = "") -> dict:
        self.manual[wallet] = {"label": label or "", "added_at": time.time()}
        await self.state.db.smart_wallets.update_one({"_id": f"manual:{wallet}"}, {"$set": {"kind": "manual", "wallet": wallet, "label": label or "", "added_at": time.time()}}, upsert=True)
        return self.snapshot()

    async def remove_manual(self, wallet: str) -> dict:
        self.manual.pop(wallet, None)
        await self.state.db.smart_wallets.delete_one({"_id": f"manual:{wallet}"})
        return self.snapshot()

    async def forget_learned(self, wallet: str | None = None) -> dict:
        if wallet:
            self.learned.pop(wallet, None)
            await self.state.db.smart_wallets.delete_one({"_id": f"learned:{wallet}"})
        else:
            self.learned.clear()
            await self.state.db.smart_wallets.delete_many({"kind": "learned"})
        self._dirty.clear()
        return self.snapshot()

    # ---------- membership ----------
    def _promoted(self, s: dict) -> bool:
        wins, misses = int(s.get("wins") or 0), int(s.get("misses") or 0)
        n = wins + misses
        if wins < int(self._cfg("smart_wallet_learn_min_wins", 3) or 0) or n == 0:
            return False
        return wins / n >= float(self._cfg("smart_wallet_learn_min_win_rate", 0.6) or 0.0)

    def kind_of(self, wallet: str) -> str | None:
        if wallet in self.manual:
            return "manual"
        s = self.learned.get(wallet)
        if s is not None and self._cfg("smart_wallet_learn_enabled", True) and self._promoted(s):
            return "learned"
        return None

    def promoted_count(self) -> int:
        return sum(1 for s in self.learned.values() if self._promoted(s))

    # ---------- tape ----------
    def on_buy(self, mint: str, b: dict, wallet: str, lamports: int, price: float | None, now: float) -> None:
        if not wallet or wallet == b.get("creator"):
            return
        if self._cfg("smart_wallet_learn_enabled", True):
            self._record_early(b, wallet, lamports, price, now)
        if not self.enabled():
            return
        kind = self.kind_of(wallet)
        if kind is None or lamports < float(self._cfg("smart_wallet_min_buy_sol", 0.2) or 0.0) * LAMPORTS_PER_SOL:
            return
        hits = b.setdefault("smart_hits", {})
        hits[wallet] = {"ts": now, "kind": kind, "sol": lamports / LAMPORTS_PER_SOL}
        b["smart_hit_ts"] = now
        self.stats["hits"] += 1
        if self.mode() in ("hard", "both") and not b.get("_sw_fired"):
            self._maybe_fire(mint, b, wallet, kind, lamports, now)

    def _record_early(self, b: dict, wallet: str, lamports: int, price: float | None, now: float) -> None:
        early = b.get("_sw_early")
        if early is None:
            early = b["_sw_early"] = {}
        if b.get("_sw_graded"):
            return
        if price and price > 0:
            for rec in early.values():
                if price > rec["peak"]:
                    rec["peak"] = price
        if wallet in early or len(early) >= EARLY_K or not price or price <= 0:
            return
        if now - float(b.get("start") or now) > float(self._cfg("smart_wallet_learn_early_s", 120) or 120):
            return
        if lamports < LEARN_MIN_BUY_SOL * LAMPORTS_PER_SOL:
            return
        early[wallet] = {"ts": now, "price": price, "peak": price, "sol": lamports / LAMPORTS_PER_SOL}

    def parked(self, b: dict, now: float | None = None) -> dict | None:
        """Smart buy that could not be taken (slots full, cap…): the token is held in the candidate window for a while."""
        p = b.get("smart_parked")
        if not p:
            return None
        now = now or time.time()
        if float(p.get("until") or 0) <= now:
            b.pop("smart_parked", None)
            return None
        return {**p, "left_s": int(float(p["until"]) - now)}

    def _park(self, b: dict, ev: dict, reason: str, now: float) -> None:
        ttl = float(self._cfg("smart_wallet_park_s", 300) or 0)
        if ttl <= 0:
            return
        b["smart_parked"] = {"wallet": ev["wallet"], "kind": ev["kind"], "label": ev.get("label") or "", "sol": ev.get("sol"),
                             "reason": reason, "ts": now, "until": now + ttl}
        self.stats["parked"] = self.stats.get("parked", 0) + 1

    def _maybe_fire(self, mint: str, b: dict, wallet: str, kind: str, lamports: int, now: float) -> None:
        st = self.state
        from scanner import MomentumScanner
        ev = {"ts": now, "mint": mint, "symbol": b.get("symbol"), "wallet": wallet, "kind": kind,
              "label": (self.manual.get(wallet) or {}).get("label") or "", "sol": round(lamports / LAMPORTS_PER_SOL, 4)}
        if not st.config.enabled or st.stopping_gracefully:
            return
        if MomentumScanner.classify_band(b, st.config, now) is None:
            self.stats["out_of_window"] += 1
            self.recent.appendleft({**ev, "result": "out-of-window"})
            return
        if mint in st.active_trades or mint in st._pending_entry_mints or mint in st.entered_mints:
            return
        cap = max(0, int(self._cfg("smart_wallet_max_per_hour", 6) or 0))
        while self.fires and now - self.fires[0] > 3600:
            self.fires.popleft()
        if cap and len(self.fires) >= cap:
            self.stats["rate_capped"] += 1
            self._park(b, ev, "rate-cap", now)
            self.recent.appendleft({**ev, "result": "parked: rate-cap"})
            return
        b["_sw_fired"] = now
        self.fires.append(now)
        self.stats["fired"] += 1
        asyncio.create_task(self._fire(mint, b, ev))

    async def _fire(self, mint: str, b: dict, ev: dict) -> None:
        from models import Launch
        from ws_hub import hub
        st = self.state
        logger.info(f"smart wallet [{ev['kind']}] {ev['wallet'][:8]}… bought {ev['sol']} SOL of {ev.get('symbol') or mint[:8]} — firing hunt entry")
        launch = Launch(mint=mint, creator=b.get("creator") or "", bonding_curve="", name=b.get("name"), symbol=b.get("symbol"))
        launch.id = b.get("launch_id") or launch.id
        launch.classifier_action = "smart_wallet"
        b["smart_trigger_wallet"] = ev["wallet"]
        try:
            await st._enter(launch, 50, "smart_wallet")
        except Exception as e:
            logger.exception(f"smart wallet entry failed for {mint}: {e}")
        opened = mint in st.active_trades
        if opened:
            self.stats["opened"] += 1
            t = (st.active_trades[mint].get("trade") or {})
            t["smart_wallet"] = ev["wallet"]
            t["smart_wallet_kind"] = ev["kind"]
            if t.get("id"):
                await st.db.trades.update_one({"_id": t["id"]}, {"$set": {"smart_wallet": ev["wallet"], "smart_wallet_kind": ev["kind"]}})
        else:
            self.stats["refused"] += 1
            b.pop("_sw_fired", None)                      # a refusal (hunt-cap, doctor pause…) lets the next smart buy retry
        ref = (b.get("entry_refusal") or {}) if not opened else {}
        if not opened:
            self._park(b, ev, ref.get("reason") or "refused", time.time())
        self.recent.appendleft({**ev, "result": "opened" if opened else f"parked: {ref.get('reason') or 'refused'}"})
        await hub.broadcast("smart_wallet_fire", {**ev, "opened": opened})

    # ---------- scanner hooks ----------
    def fresh_hits(self, b: dict, now: float) -> int:
        if not self.enabled() or self.mode() not in ("soft", "both"):
            return 0
        ttl = float(self._cfg("smart_wallet_hit_ttl_s", 180) or 180)
        return sum(1 for h in (b.get("smart_hits") or {}).values() if now - float(h["ts"]) <= ttl)

    def relax_gates(self, g: dict, b: dict, now: float) -> dict:
        if not self.fresh_hits(b, now):
            return g
        f = max(0.0, 1.0 - float(self._cfg("smart_wallet_gate_relax_pct", 50.0) or 0.0) / 100.0)
        return {**g, "min_growth_pct": g["min_growth_pct"] * f, "min_inflow_sol": g["min_inflow_sol"] * f,
                "min_new_buyers": int(g["min_new_buyers"] * f)}

    def rank_boost(self, b: dict, now: float) -> float:
        return self.fresh_hits(b, now) * float(self._cfg("smart_wallet_soft_boost", 25.0) or 0.0)

    # ---------- learning ----------
    def grade_bucket(self, b: dict, now: float, final: bool = False) -> int:
        """Grade this launch's early buyers once: WIN when post-buy peak ≥ +min_gain % (or graduated), else MISS.
        `final` (eviction) grades anything ≥ 60 s old; the periodic pass waits for the learn horizon."""
        early = b.get("_sw_early")
        if not early or b.get("_sw_graded") or not self._cfg("smart_wallet_learn_enabled", True):
            return 0
        age = now - float(b.get("start") or now)
        horizon = float(self._cfg("smart_wallet_learn_horizon_s", 300) or 300)
        if age < (60.0 if final else horizon):
            return 0
        b["_sw_graded"] = True
        min_gain = float(self._cfg("smart_wallet_learn_min_gain_pct", 50.0) or 0.0)
        graduated = (b.get("protocol") == "pumpswap") or float(b.get("curve_fill_pct") or 0) >= 99.0
        last = float(b.get("last_price_sol") or 0.0)
        n = 0
        for w, rec in early.items():
            peak = max(rec["peak"], last)
            gain = (peak / rec["price"] - 1.0) * 100.0 if rec["price"] > 0 else 0.0
            win = graduated or gain >= min_gain
            s = self.learned.setdefault(w, {"wins": 0, "misses": 0, "last_seen": 0.0, "last_win": None, "best_gain_pct": 0.0})
            s["wins" if win else "misses"] += 1
            s["last_seen"] = now
            if win:
                s["last_win"] = b.get("symbol") or ""
                s["best_gain_pct"] = max(float(s.get("best_gain_pct") or 0), round(gain, 1))
            self.stats["wins" if win else "misses"] += 1
            self._dirty.add(w)
            n += 1
        self.stats["graded_buys"] += n
        self.stats["graded_launches"] += 1
        return n

    def _prune(self) -> None:
        if len(self.learned) <= MAX_LEARNED:
            return
        keep = sorted(self.learned.items(), key=lambda kv: (kv[1]["wins"], kv[1]["last_seen"]), reverse=True)[:MAX_LEARNED]
        self.learned = dict(keep)

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(GRADE_TICK_S)
                now = time.time()
                for b in list(self.state.tracking.values()):
                    self.grade_bucket(b, now)
                if now - self._last_flush >= FLUSH_EVERY_S:
                    self._last_flush = now
                    self._prune()
                    await self._flush()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"smart wallets loop: {e!r}")

    # ---------- API ----------
    def snapshot(self, limit: int = 50) -> dict:
        now = time.time()
        learned = sorted(self.learned.items(), key=lambda kv: (self._promoted(kv[1]), kv[1]["wins"], -kv[1]["misses"]), reverse=True)
        rows = []
        for w, s in learned[:limit]:
            n = s["wins"] + s["misses"]
            rows.append({"wallet": w, "wins": s["wins"], "misses": s["misses"], "win_rate": round(s["wins"] / n, 2) if n else 0.0,
                         "promoted": self._promoted(s), "last_seen": s["last_seen"], "last_win": s.get("last_win"),
                         "best_gain_pct": s.get("best_gain_pct", 0.0)})
        watching = [{"mint": m, "symbol": b.get("symbol"), "hits": len(b.get("smart_hits") or {}),
                     "age_s": round(now - float(b.get("start") or now)), "parked": self.parked(b, now)}
                    for m, b in self.state.tracking.items() if b.get("smart_hits")]
        return {"enabled": self.enabled(), "mode": self.mode(),
                "manual": [{"wallet": w, **m} for w, m in self.manual.items()],
                "learned": rows, "learned_total": len(self.learned), "promoted": self.promoted_count(),
                "fires_last_hour": sum(1 for t in self.fires if now - t <= 3600),
                "grading": sum(1 for b in self.state.tracking.values() if b.get("_sw_early") and not b.get("_sw_graded")),
                "recent": list(self.recent), "watching": watching[:20], "stats": dict(self.stats)}
