"""
doctor_learning — Strategy Doctor LEARNING POLICY LOOP.

Turns the Doctor from "suggest knobs" into: measure each BOOK (momentum vs
greylist_snipe) on FILL expectancy (mean pnl_sol after fees), emit AT MOST one
structural proposal per cycle (feature flag / threshold from a whitelist),
run it as a paper-first CANARY, then PROMOTE or REVERT on measured expectancy
and drawdown. Config/flags only — never generates strategy code.

Hard constraints enforced here:
  - never touches live_trading / enabled / daily_kill_switch_usd / wallet /
    max_trade_usd (+25% cap is moot: max_trade_usd is not in the whitelist)
  - one change at a time (no apply while a canary is running)
  - auto-apply only when doctor_auto_apply_enabled AND not doctor_advisory_only
    AND (not live_trading OR doctor_auto_apply_live)
"""
from __future__ import annotations

import hashlib
import logging
import statistics
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("doctor_learning")

BOOKS = ("momentum", "greylist_snipe")
ALLOWED_KEYS = {
    "book_momentum_size_mult", "book_snipe_size_mult", "greylist_snipe_min_score",
    "trailing_stop_pct", "greylist_snipe_stale_seconds", "hold_max_seconds",
    "speed_mode", "scanner_interval_s",
}
FORBIDDEN_KEYS = {"live_trading", "enabled", "daily_kill_switch_usd", "max_trade_usd"}
CANARY_ID = "current"


def book_of(t: dict) -> str | None:
    if t.get("chain") == "rh":
        return None
    b = t.get("book")
    if b in BOOKS:
        return b
    return "greylist_snipe" if t.get("classifier_action") == "greylist_snipe" else "momentum"


def book_size_mult(cfg, action: str) -> float:
    """Sizing helper used by bot._enter / reentry. 0.0 ⇒ skip that book."""
    key = "book_snipe_size_mult" if action == "greylist_snipe" else "book_momentum_size_mult"
    v = cfg.get(key, 1.0) if isinstance(cfg, dict) else getattr(cfg, key, 1.0)
    try:
        return max(0.0, float(v if v is not None else 1.0))
    except (TypeError, ValueError):
        return 1.0


def _mfe(t: dict) -> float | None:
    ep, pk = t.get("entry_price_sol") or 0, t.get("peak_price_sol") or 0
    return (pk / ep - 1.0) * 100.0 if ep and pk else None


def _max_drawdown(pnls: list[float]) -> float:
    peak = cum = 0.0
    dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


def _exit_class(t: dict) -> str:
    r = (t.get("exit_reason") or "").lower()
    if "stale" in r:
        return "stale"
    if "timeout" in r or "no-momentum" in r:
        return "timeout"
    return "other"


def book_stats(trades: list[dict]) -> dict:
    """FILL-based book metrics. pnl_sol is the after-fee realised SOL."""
    rows = [t for t in trades if t.get("pnl_sol") is not None]
    n = len(rows)
    if n == 0:
        return {"n": 0}
    pnls = [float(t["pnl_sol"]) for t in rows]
    wins = [t for t in rows if float(t["pnl_sol"]) > 0]
    holds = []
    for t in rows:
        try:
            e = datetime.fromisoformat(str(t["entry_time"]).replace("Z", "+00:00"))
            x = datetime.fromisoformat(str(t["exit_time"]).replace("Z", "+00:00"))
            holds.append((x - e).total_seconds())
        except Exception:
            pass
    mfes = [m for m in (_mfe(t) for t in rows) if m is not None]
    givebacks = [(_mfe(t) or 0) - float(t.get("pnl_pct") or 0) for t in wins if _mfe(t) is not None]
    lat = []
    for t in rows:
        d, f, e = t.get("decision_price_sol"), t.get("fill_price_sol"), t.get("entry_price_sol")
        if d and f and e:
            lat.append((d - f) / e * 100.0)
    by_reason: dict[str, float] = {}
    for t in rows:
        by_reason[t.get("exit_reason") or "?"] = by_reason.get(t.get("exit_reason") or "?", 0.0) + float(t["pnl_sol"])
    stale_or_timeout = [t for t in rows if _exit_class(t) in ("stale", "timeout")]
    return {
        "n": n,
        "expectancy_sol": statistics.mean(pnls),
        "total_sol": sum(pnls),
        "winrate": len(wins) / n * 100.0,
        "median_hold_s": statistics.median(holds) if holds else None,
        "median_mfe": statistics.median(mfes) if mfes else None,
        "median_giveback": statistics.median(givebacks) if givebacks else None,
        "median_latency_tax": statistics.median(lat) if lat else None,
        "max_drawdown_sol": _max_drawdown(pnls),
        "stale_timeout_share": len(stale_or_timeout) / n * 100.0,
        "stale_timeout_mean_pnl": statistics.mean(float(t["pnl_sol"]) for t in stale_or_timeout) if stale_or_timeout else None,
        "top_exit_reasons": sorted(by_reason.items(), key=lambda kv: kv[1])[:4],
    }


def fingerprint(proposal: dict) -> str:
    return hashlib.sha1(f"{proposal['book']}|{proposal['key']}|{proposal['value']}".encode()).hexdigest()[:16]


def propose(cfg: dict, stats: dict[str, dict], min_n: int) -> dict | None:
    """Priority ladder — first match wins. Returns None when nothing qualifies."""
    def P(kind, book, key, value, reason, direction, evidence):
        assert key in ALLOWED_KEYS and key not in FORBIDDEN_KEYS
        return {"type": kind, "book": book, "key": key, "value": value, "reason": reason,
                "expected_direction": direction, "evidence": evidence}

    mom, sn = stats.get("momentum", {"n": 0}), stats.get("greylist_snipe", {"n": 0})
    # 1. disable a losing book
    for book, st, key in (("momentum", mom, "book_momentum_size_mult"), ("greylist_snipe", sn, "book_snipe_size_mult")):
        if st["n"] >= min_n and st["expectancy_sol"] < 0 and float(cfg.get(key, 1.0) or 0) > 0:
            # 2. large snipe sample with a still-low score threshold → raise the bar first
            if book == "greylist_snipe" and st["n"] >= 3 * min_n and float(cfg.get("greylist_snipe_min_score", 45)) < 70 \
                    and st["expectancy_sol"] > -0.002:
                new = min(80.0, float(cfg.get("greylist_snipe_min_score", 45)) + 5)
                return P("threshold", "greylist_snipe", "greylist_snipe_min_score", new,
                         f"snipe book slightly negative ({st['expectancy_sol']:+.5f} SOL/trade, n={st['n']}); raise score bar before disabling",
                         "raise expectancy by filtering weakest snipes",
                         {"expectancy_sol": st["expectancy_sol"], "n": st["n"], "winrate": st["winrate"]})
            return P("flag", book, key, 0.0,
                     f"{book} book expectancy {st['expectancy_sol']:+.5f} SOL/trade over {st['n']} fills (total {st['total_sol']:+.4f} SOL) — disable the losing machine",
                     "raise expectancy by cutting losing book",
                     {"expectancy_sol": st["expectancy_sol"], "n": st["n"], "winrate": st["winrate"], "total_sol": st["total_sol"]})
    # 3. momentum giveback
    if mom["n"] >= min_n and (mom.get("median_giveback") or 0) > 10 and float(cfg.get("trailing_stop_pct", 6)) > 4:
        new = max(4.0, float(cfg.get("trailing_stop_pct", 6)) - 1)
        return P("threshold", "momentum", "trailing_stop_pct", new,
                 f"momentum winners give back a median {mom['median_giveback']:.1f}pp of their peak",
                 "raise expectancy by cutting giveback",
                 {"median_giveback": mom["median_giveback"], "median_mfe": mom.get("median_mfe"), "n": mom["n"]})
    # 4. stale / timeout heavy book
    for book, st in (("greylist_snipe", sn), ("momentum", mom)):
        if st["n"] >= min_n and st["stale_timeout_share"] > 40 and (st.get("stale_timeout_mean_pnl") or 0) < 0:
            if book == "greylist_snipe":
                cur = int(cfg.get("greylist_snipe_stale_seconds", 60))
                if cur > 30:
                    return P("threshold", book, "greylist_snipe_stale_seconds", max(30, cur - 15),
                             f"{st['stale_timeout_share']:.0f}% of snipe exits are stale/timeout with mean {st['stale_timeout_mean_pnl']:+.5f} SOL",
                             "raise expectancy by cutting dead holds", {"share": st["stale_timeout_share"], "n": st["n"]})
            else:
                cur = int(cfg.get("hold_max_seconds", 35))
                if cur > 20:
                    return P("threshold", book, "hold_max_seconds", max(20, cur - 5),
                             f"{st['stale_timeout_share']:.0f}% of momentum exits are timeout with mean {st['stale_timeout_mean_pnl']:+.5f} SOL",
                             "raise expectancy by cutting dead holds", {"share": st["stale_timeout_share"], "n": st["n"]})
    # 5. latency tax
    for book, st in (("momentum", mom), ("greylist_snipe", sn)):
        if st["n"] >= min_n and (st.get("median_latency_tax") or 0) > 8:
            mode = str(cfg.get("speed_mode", "manual"))
            if mode in ("eco", "manual"):
                return P("flag", "global", "speed_mode", "fast",
                         f"{book} median latency tax {st['median_latency_tax']:.1f}pp (decision→fill); TP is NOT loosened",
                         "raise expectancy by landing exits closer to decision price", {"latency_tax": st["median_latency_tax"]})
            cur = int(cfg.get("scanner_interval_s", 15))
            return P("threshold", "global", "scanner_interval_s", min(60, cur + 5),
                     f"latency tax {st['median_latency_tax']:.1f}pp at speed_mode={mode}; reduce entry rate instead of loosening TP",
                     "raise expectancy by taking fewer, better fills", {"latency_tax": st["median_latency_tax"]})
    return None


class LearningEngine:
    def __init__(self, db, hub=None, reload_cb=None):
        self.db = db
        self.hub = hub
        self.reload_cb = reload_cb
        self.last: dict = {"books": {}, "proposal": None, "canary": None, "note": ""}

    # ---------- persistence ----------
    async def canary(self) -> dict | None:
        return await self.db.doctor_canary.find_one({"_id": CANARY_ID}, {"_id": 0})

    async def _set_canary(self, doc: dict | None):
        if doc is None:
            await self.db.doctor_canary.delete_many({"_id": CANARY_ID})
        else:
            await self.db.doctor_canary.update_one({"_id": CANARY_ID}, {"$set": doc}, upsert=True)

    async def _blacklisted(self, fp: str) -> bool:
        d = await self.db.doctor_blacklist.find_one({"fingerprint": fp}, {"_id": 0})
        return bool(d and str(d.get("expires_at", "")) > datetime.now(timezone.utc).isoformat())

    async def _blacklist(self, fp: str, hours: int = 24):
        await self.db.doctor_blacklist.update_one(
            {"fingerprint": fp},
            {"$set": {"fingerprint": fp, "expires_at": (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()}},
            upsert=True,
        )

    # ---------- gates ----------
    @staticmethod
    def auto_apply_allowed(cfg: dict) -> bool:
        if not cfg.get("doctor_auto_apply_enabled") or cfg.get("doctor_advisory_only"):
            return False
        if cfg.get("live_trading") and not cfg.get("doctor_auto_apply_live"):
            return False
        return True

    # ---------- main cycle ----------
    async def cycle(self, cfg: dict, trades_24h: list[dict], trades_7d: list[dict]) -> list[dict]:
        """Returns 0–1 suggestion dicts (category 'learning') for the Doctor to
        surface. Handles canary evaluation + optional auto-apply itself."""
        if not cfg.get("doctor_learning_enabled", True):
            return []
        min_n = int(cfg.get("doctor_learning_min_trades_per_book", 15))
        books: dict[str, dict] = {}
        for b in BOOKS:
            s24 = book_stats([t for t in trades_24h if book_of(t) == b])
            s7 = book_stats([t for t in trades_7d if book_of(t) == b])
            s24["expectancy_7d"] = s7.get("expectancy_sol")
            s24["n_7d"] = s7.get("n", 0)
            books[b] = s24
        self.last["books"] = books

        can = await self.canary()
        if can and can.get("state") == "running":
            await self._evaluate_canary(can, cfg, trades_24h)
            self.last["canary"] = await self.canary()
            return []  # one change at a time

        proposal = propose(cfg, books, min_n)
        self.last["canary"] = can
        if not proposal:
            self.last["proposal"] = None
            self.last["note"] = "no structural edge found this cycle"
            return []
        fp = fingerprint(proposal)
        if await self._blacklisted(fp):
            self.last["proposal"] = None
            self.last["note"] = f"proposal {proposal['key']}={proposal['value']} blacklisted after a failed canary"
            return []
        proposal["fingerprint"] = fp
        self.last["proposal"] = proposal
        self.last["note"] = ""
        sugg = {
            "category": "learning",
            "title": f"[{proposal['book']}] {proposal['key']} → {proposal['value']}",
            "rationale": (
                f"{proposal['reason']}. Expected: {proposal['expected_direction']}. Runs as a canary for "
                f"{int(cfg.get('doctor_learning_canary_trades', 12))} trades / "
                f"{float(cfg.get('doctor_learning_canary_hours', 6.0)):g}h; promoted only if fill expectancy "
                "improves and drawdown is not >15% worse — otherwise reverted and blacklisted 24h. "
                "Optimises expectancy after fill, not a green streak."
            ),
            "actions": {proposal["key"]: proposal["value"]},
            "confidence": "high" if proposal["type"] == "flag" else "med",
            "metrics": {"book": proposal["book"], "fingerprint": fp, **{k: (round(v, 6) if isinstance(v, float) else v) for k, v in proposal["evidence"].items()}},
            "learning": True,
        }
        if self.auto_apply_allowed(cfg):
            await self.apply(proposal, cfg, books, auto=True)
            sugg["status"] = "applied"
        return [sugg]

    async def apply(self, proposal: dict, cfg: dict, books: dict | None = None, auto: bool = False) -> dict:
        key, value = proposal["key"], proposal["value"]
        if key not in ALLOWED_KEYS or key in FORBIDDEN_KEYS:
            raise ValueError(f"key {key} not allowed")
        if await self.canary() and (await self.canary()).get("state") == "running":
            raise RuntimeError("canary already running — one change at a time")
        book = proposal["book"]
        st = (books or self.last.get("books") or {}).get(book if book in BOOKS else "momentum", {})
        prior = cfg.get(key)
        if prior is None:  # key never persisted → baseline is the model default
            from models import BotConfig
            prior = BotConfig().model_dump().get(key)
        canary = {
            "state": "running",
            "proposal": proposal,
            "book": book,
            "baseline_config_subset": {key: prior},
            "started_at": datetime.now(timezone.utc).isoformat(),
            "trades_at_start": st.get("n", 0),
            "baseline_expectancy_sol": st.get("expectancy_sol"),
            "baseline_max_drawdown_sol": st.get("max_drawdown_sol"),
            "auto": auto,
        }
        await self.db.bot_config.update_one({}, {"$set": {key: value}})
        cfg[key] = value
        await self._set_canary(canary)
        await self._reload()
        logger.warning(f"doctor LEARNING canary started: {key}={value} ({'auto' if auto else 'manual'}) book={book}")
        if self.hub:
            try:
                await self.hub.broadcast("doctor_canary", {"state": "running", **canary})
            except Exception:
                pass
        return canary

    async def _evaluate_canary(self, can: dict, cfg: dict, trades_24h: list[dict]):
        started = can.get("started_at", "")
        book = can.get("book")
        since = [t for t in trades_24h if str(t.get("exit_time") or "") >= started
                 and (book == "global" or book_of(t) == book)]
        n_req = int(cfg.get("doctor_learning_canary_trades", 12))
        hours_req = float(cfg.get("doctor_learning_canary_hours", 6.0))
        try:
            age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(started)).total_seconds() / 3600.0
        except Exception:
            age_h = 0.0
        if len(since) < n_req and age_h < hours_req:
            return
        st = book_stats(since)
        base_e = can.get("baseline_expectancy_sol")
        base_dd = can.get("baseline_max_drawdown_sol") or 0.0
        exp_ok = st.get("n", 0) >= max(3, n_req // 2) and base_e is not None and st["expectancy_sol"] > base_e
        dd_ok = st.get("max_drawdown_sol", 0.0) <= base_dd * 1.15 + 1e-9
        verdict = {"expectancy_since": st.get("expectancy_sol"), "n_since": st.get("n", 0),
                   "drawdown_since": st.get("max_drawdown_sol"), "age_h": round(age_h, 2)}
        if exp_ok and dd_ok:
            await self._set_canary({**can, "state": "promote", "ended_at": datetime.now(timezone.utc).isoformat(), **verdict})
            logger.warning(f"doctor LEARNING canary PROMOTED {can['proposal']['key']}={can['proposal']['value']} {verdict}")
        else:
            await self.revert(reason="canary failed", verdict=verdict)

    async def revert(self, reason: str = "manual", verdict: dict | None = None) -> dict | None:
        can = await self.canary()
        if not can or can.get("state") != "running":
            return None
        subset = can.get("baseline_config_subset") or {}
        if subset:
            await self.db.bot_config.update_one({}, {"$set": subset})
        await self._blacklist(fingerprint(can["proposal"]))
        done = {**can, "state": "reverted", "ended_at": datetime.now(timezone.utc).isoformat(),
                "revert_reason": reason, **(verdict or {})}
        await self._set_canary(done)
        await self._reload()
        logger.warning(f"doctor LEARNING canary REVERTED ({reason}) restored {subset}")
        if self.hub:
            try:
                await self.hub.broadcast("doctor_canary", {"state": "reverted", **done})
            except Exception:
                pass
        return done

    async def _reload(self):
        if self.reload_cb:
            try:
                await self.reload_cb()
            except Exception as e:
                logger.warning(f"learning reload_cb failed: {e}")

    async def status(self) -> dict:
        return {"books": self.last.get("books", {}), "proposal": self.last.get("proposal"),
                "canary": await self.canary(), "note": self.last.get("note", "")}
