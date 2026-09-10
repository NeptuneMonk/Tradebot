"""
doctor_learning — Strategy Doctor LEARNING POLICY LOOP.

Turns the Doctor from "suggest knobs" into: measure each BOOK (scalp, hunt,
rh_pons — SOL and RH alike) on FILL expectancy in R
(mean pnl_usd after fees — profit, never win rate), emit AT MOST one
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

from book_params import BOOKS
BOOK_LABELS = {"scalp": "Scalp book (momentum)", "hunt": "Hunt book (greylist snipe + re-entry)",
               "rh_pons": "RH · PONS book", "global": "All books"}
# The Doctor may move: one BOOK-SCOPED exit key (book_exits.<book>.<param>), book entry thresholds, or a book
# size mult via the allocator. There are no shared exit keys any more.
BOOK_EXIT_KEYS = {"stop_loss_pct", "target_r", "trailing_stop_pct", "trailing_arm_pct", "hold_max_seconds",
                  "ladder_1r_sell_pct", "ladder_2r_sell_pct", "take_profit_pct"}
ALLOWED_KEYS = {
    "book_scalp_size_mult", "book_hunt_size_mult", "book_rh_size_mult", "greylist_snipe_min_score",
    "greylist_snipe_stale_seconds", "reentry_enabled", "reentry_breakout_pct", "reentry_min_bounce_pct", "reentry_min_buyers",
    "rh_min_growth_pct", "rh_min_inflow_usd", "rh_min_unique_buyers", "rh_min_curve_pct", "rh_min_mc_usd", "rh_max_growth_pct",
    "flush_hold_s", "serial_creator_min_launches", "serial_creator_requires_graduation",
    "min_curve_liquidity_sol", "min_buyers_for_entry", "min_curve_liquidity_sol_new", "min_buyers_for_entry_new",
}
FORBIDDEN_KEYS = {"live_trading", "rh_live_trading", "enabled", "daily_kill_switch_usd", "rh_daily_kill_switch_usd", "max_trade_usd", "rh_max_trade_usd"}
TECHNIQUE_MIN_GAIN_R = 0.05          # R/fill a technique change must add to be worth a canary
TECHNIQUE_MIN_GAIN_REL = 0.15        # …and ≥15% of |current expectancy_r|
LAST_RESORT_MULT = 3
CANARY_ID = "current"
PROMOTION_MIN_FILLS = {"scalp": 30, "hunt": 20, "rh_pons": 20, "global": 30}   # post-canary-start fills, per book


def key_ok(key: str) -> bool:
    if key in FORBIDDEN_KEYS:
        return False
    if key.startswith("book_exits."):
        parts = key.split(".")
        return len(parts) in (3, 4) and parts[1] in BOOKS and parts[-1] in BOOK_EXIT_KEYS
    if key.startswith("regime_gate_mult."):
        return True
    return key in ALLOWED_KEYS


def cfg_get(cfg: dict, key: str):
    cur = cfg
    for part in key.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def cfg_set(cfg: dict, key: str, value):
    parts = key.split(".")
    cur = cfg
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def propose_technique(cfg: dict, trades_by_book: dict[str, list[dict]], min_n: int, extra: dict | None = None) -> tuple[dict | None, dict]:
    """One book-scoped candidate per cycle, ranked by R/fill gain: an exit key from the counterfactual grid,
    an entry threshold from feature splits, or a regime gate multiplier. Never a shared key."""
    from book_params import whatif_exits, entry_feature_splits, entry_pair_splits, regime_splits, book_exit_view, REGIME_MULT_STEP, REGIME_MULT_CAP
    cands, analysis = [], {}
    for book in BOOKS:
        rows = trades_by_book.get(book) or []
        if len(rows) < min_n:
            continue
        cur = book_exit_view(cfg, book)
        wi = whatif_exits(rows, cur)
        analysis[book] = {"whatif": wi}
        b = wi.get("best")
        if b and wi["gain_r_per_fill"] >= max(TECHNIQUE_MIN_GAIN_R, TECHNIQUE_MIN_GAIN_REL * abs(wi["current"]["expectancy_r"])) \
                and float(b["value"]) != float(cur[b["param"]]):
            cands.append({"type": "exit", "book": book, "key": f"book_exits.{book}.{b['param']}", "value": b["value"], "gain": wi["gain_r_per_fill"],
                          "reason": (f"{book}: {b['param']} {cur[b['param']]:g} → {b['value']:g} would move expectancy "
                                     f"{wi['current']['expectancy_r']:+.3f} → {b['expectancy_r']:+.3f} R/fill (median MFE {wi['median_mfe']:+.1f}%, MAE {wi['median_mae']:+.1f}%)"),
                          "direction": "tighten" if b["param"] in ("stop_loss_pct", "hold_max_seconds") and b["value"] < cur[b["param"]] else "widen",
                          "evidence": {"n": wi["n"], "expectancy_now": wi["current"]["expectancy_r"], "expectancy_whatif": b["expectancy_r"], "grid": wi["rows"][:24]}})
        splits = entry_feature_splits(rows, book, cfg)
        analysis[book]["entry_splits"] = splits
        for sp in splits:
            if sp["actionable"] and sp["gain_r_per_fill"] >= TECHNIQUE_MIN_GAIN_R:
                val = round(sp["split"], 2) if isinstance(sp["split"], float) and not float(sp["split"]).is_integer() else int(sp["split"])
                side = ("≥", "lose", sp["high_expectancy_r"], "below earn", sp["low_expectancy_r"], "tighten") if sp["direction"] == "ceiling" \
                    else ("<", "lose", sp["low_expectancy_r"], "above earn", sp["high_expectancy_r"], "raise")
                cands.append({"type": "threshold", "book": book, "key": sp["key"], "value": val, "gain": sp["gain_r_per_fill"],
                              "reason": (f"{book} fills with {sp['feature']} {side[0]} {sp['split']:g} {side[1]} {side[2]:+.3f} R/fill, "
                                         f"{side[3]} {side[4]:+.3f} (n={sp['n']}) → {side[5]} {sp['key']} {sp['current']:g} → {val:g}"),
                              "direction": side[5], "evidence": {k: sp[k] for k in ("feature", "n", "split", "current", "low_expectancy_r", "high_expectancy_r")}})
                break
        pairs = entry_pair_splits(rows, book, cfg)
        analysis[book]["entry_pairs"] = pairs
        for pr in pairs:
            if pr["actionable"] and pr["gain_r_per_fill"] >= TECHNIQUE_MIN_GAIN_R:
                vals = [round(x, 2) if isinstance(x, float) and not float(x).is_integer() else int(x) for x in pr["splits"]]
                cands.append({"type": "threshold_pair", "book": book, "key": pr["keys"][0], "value": vals[0],
                              "extra_actions": {pr["keys"][1]: vals[1]}, "gain": pr["gain_r_per_fill"],
                              "reason": (f"{book} fills with {pr['features'][0]} ≥ {pr['splits'][0]:g} AND {pr['features'][1]} ≥ {pr['splits'][1]:g} "
                                         f"earn {pr['high_high_expectancy_r']:+.3f} R/fill (n={pr['high_high_n']}) while the rest lose {pr['rest_expectancy_r']:+.3f} → raise both gates"),
                              "direction": "raise", "evidence": {k: pr[k] for k in ("features", "n", "splits", "current", "high_high_n", "high_high_expectancy_r", "rest_expectancy_r")}})
                break
        regime = regime_splits(rows, book, cfg)
        analysis[book]["regime"] = regime
        if regime and regime["actionable"] and regime["gain_r_per_fill"] >= TECHNIQUE_MIN_GAIN_R:
            lose = regime["losing"]
            cands.append({"type": "regime", "book": book, "key": f"regime_gate_mult.{book}.{lose}",
                          "value": round(min(REGIME_MULT_CAP, regime["current_mult"] + REGIME_MULT_STEP), 2), "gain": regime["gain_r_per_fill"],
                          "reason": (f"{book} in {lose} hours (launch rate {'≥' if lose == 'busy' else '<'} {regime['threshold_per_h']:.0f}/h) "
                                     f"earns {regime[lose]['expectancy_r']:+.3f} R/fill (n={regime[lose]['n']}) vs "
                                     f"{regime['quiet' if lose == 'busy' else 'busy']['expectancy_r']:+.3f} in the other regime → tighten that regime's gates"),
                          "direction": "tighten", "evidence": regime})
    if not cands:
        return None, analysis
    best = max(cands, key=lambda c: c["gain"])
    return {"kind": best["type"], "book": best["book"], "key": best["key"], "value": best["value"], "reason": best["reason"],
            "expected_direction": best["direction"], "evidence": best["evidence"], "gain_r_per_fill": round(best["gain"], 4),
            "technique": True, "actions": {best["key"]: best["value"], **(best.get("extra_actions") or {})}}, analysis


def book_of(t: dict) -> str | None:
    """Learning book of a closed trade. Manual buys are the operator's decision → None (still shown in P/L)."""
    from book_params import book_for_action
    a = t.get("classifier_action") or ""
    if a in ("manual", "rh_pons_manual"):
        return None
    b = t.get("book")
    if b == "runner":
        return "runner"
    return b if b in BOOKS else book_for_action(a, t.get("chain"))


def _pnl(t: dict) -> float | None:
    """USD after-fee realised PnL — the one unit SOL and RH books share. Promoted runners count only the
    post-promotion leg (runner_pnl_usd); the chips banked before promotion belong to the book they came from."""
    v = t.get("runner_pnl_usd") if t.get("book") == "runner" and t.get("runner_pnl_usd") is not None else t.get("pnl_usd")
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _mfe(t: dict) -> float | None:
    ep = t.get("entry_price_sol") or t.get("entry_price_quote") or 0
    pk = t.get("peak_price_sol") or t.get("peak_price_quote") or 0
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
    if "timeout" in r or "no-momentum" in r or "no_momentum" in r:
        return "timeout"
    if "stop" in r and "loss" in r:
        return "sl"
    if "take" in r and "profit" in r or r.startswith("tp"):
        return "tp"
    if "trail" in r:
        return "trail"
    return "other"


def book_stats(trades: list[dict], cfg: dict | None = None) -> dict:
    """FILL-based book metrics in USD (pnl_usd is after fees on both chains)."""
    trades = [t for t in trades if t.get("r_usd")]   # post-migration fills only — legacy exits never judge the new books
    rows = [(t, _pnl(t)) for t in trades]
    rows = [(t, p) for t, p in rows if p is not None]
    n = len(rows)
    if n == 0:
        return {"n": 0}
    pnls = [p for _, p in rows]
    wins = [(t, p) for t, p in rows if p > 0]
    losses = [(t, p) for t, p in rows if p <= 0]
    holds = []
    for t, _ in rows:
        try:
            e = datetime.fromisoformat(str(t["entry_time"]).replace("Z", "+00:00"))
            x = datetime.fromisoformat(str(t["exit_time"]).replace("Z", "+00:00"))
            holds.append((x - e).total_seconds())
        except Exception:
            pass
    mfes = [m for m in (_mfe(t) for t, _ in rows) if m is not None]
    win_mfes = [m for m in (_mfe(t) for t, _ in wins) if m is not None]
    givebacks = [(_mfe(t) or 0) - float(t.get("pnl_pct") or 0) for t, _ in wins if _mfe(t) is not None]
    lat = []
    for t, _ in rows:
        d, f, e = t.get("decision_price_sol"), t.get("fill_price_sol"), t.get("entry_price_sol")
        if d and f and e:
            lat.append((d - f) / e * 100.0)
    by_reason: dict[str, float] = {}
    classes: dict[str, int] = {}
    for t, p in rows:
        by_reason[t.get("exit_reason") or "?"] = by_reason.get(t.get("exit_reason") or "?", 0.0) + p
        c = _exit_class(t)
        classes[c] = classes.get(c, 0) + 1
    stale_or_timeout = [p for t, p in rows if _exit_class(t) in ("stale", "timeout")]
    from book_params import pnl_r
    rs = [r for r in (pnl_r(t) for t, _ in rows) if r is not None]
    win_r = [r for r in rs if r > 0]
    loss_r = [r for r in rs if r <= 0]
    winner_mean = statistics.mean(p for _, p in wins) if wins else None
    loser_mean = statistics.mean(p for _, p in losses) if losses else None
    by_trigger: dict[str, dict] = {}
    for t, p in rows:
        trig = t.get("reentry_trigger") or (t.get("reentry") if isinstance(t.get("reentry"), str) else None)
        if trig:
            d = by_trigger.setdefault(trig, {"n": 0, "total_usd": 0.0, "wins": 0})
            d["n"] += 1
            d["total_usd"] += p
            d["wins"] += 1 if p > 0 else 0
    for d in by_trigger.values():
        d["expectancy_usd"] = d["total_usd"] / d["n"]
        d["winrate"] = d["wins"] / d["n"] * 100.0
    return {
        "n": n,
        "expectancy_r": statistics.mean(rs) if rs else None,
        "avg_win_r": statistics.mean(win_r) if win_r else None,
        "avg_loss_r": statistics.mean(loss_r) if loss_r else None,
        "expectancy_usd": statistics.mean(pnls),
        "total_usd": sum(pnls),
        "winrate": len(wins) / n * 100.0,
        "winner_mean_usd": winner_mean,
        "loser_mean_usd": loser_mean,
        "payoff_ratio": (winner_mean / abs(loser_mean)) if (winner_mean is not None and loser_mean) else None,
        "median_hold_s": statistics.median(holds) if holds else None,
        "median_mfe": statistics.median(mfes) if mfes else None,
        "winners_median_mfe": statistics.median(win_mfes) if win_mfes else None,
        "median_giveback": statistics.median(givebacks) if givebacks else None,
        "median_latency_tax": statistics.median(lat) if lat else None,
        "max_drawdown_usd": _max_drawdown(pnls),
        "sl_share": classes.get("sl", 0) / n * 100.0,
        "tp_share": classes.get("tp", 0) / n * 100.0,
        "stale_timeout_share": len(stale_or_timeout) / n * 100.0,
        "stale_timeout_mean_pnl": statistics.mean(stale_or_timeout) if stale_or_timeout else None,
        "top_exit_reasons": sorted(by_reason.items(), key=lambda kv: kv[1])[:4],
        "by_trigger": by_trigger,
    }


def fingerprint(proposal: dict) -> str:
    return hashlib.sha1(f"{proposal['book']}|{proposal['key']}|{proposal['value']}".encode()).hexdigest()[:16]


def _f(cfg: dict, key: str, default: float) -> float:
    try:
        v = cfg.get(key)
        return float(default if v is None else v)
    except (TypeError, ValueError):
        return float(default)


def propose(cfg: dict, stats: dict[str, dict], min_n: int, trades_by_book: dict[str, list[dict]] | None = None,
            extra: dict | None = None) -> dict | None:
    """Structural proposal (rare, needs LAST_RESORT_MULT × min_n fills): raise a book's entry bar when it loses in R.
    Size cuts are the allocator's job; exits are the technique proposer's job."""
    def P(kind, book, key, value, reason, direction, evidence):
        return {"kind": kind, "book": book, "key": key, "value": value, "reason": reason, "expected_direction": direction, "evidence": evidence}

    def S(book):
        return stats.get(book) or {"n": 0}

    def ev(st):
        return {"expectancy_r": st.get("expectancy_r"), "n": st.get("n"), "winrate": st.get("winrate"), "payoff_ratio": st.get("payoff_ratio")}

    h = S("hunt")
    if h["n"] >= LAST_RESORT_MULT * min_n and (h.get("expectancy_r") or 0) < 0 and _f(cfg, "greylist_snipe_min_score", 45) < 85:
        return P("threshold", "hunt", "greylist_snipe_min_score", min(85.0, _f(cfg, "greylist_snipe_min_score", 45) + 10),
                 f"hunt book {h['expectancy_r']:+.3f} R/fill over {h['n']} fills — raise the creator score bar", "tighten", ev(h))
    r = S("rh_pons")
    if r["n"] >= LAST_RESORT_MULT * min_n and (r.get("expectancy_r") or 0) < 0:
        if _f(cfg, "rh_min_growth_pct", 20) < 150:
            return P("threshold", "rh_pons", "rh_min_growth_pct", min(150.0, _f(cfg, "rh_min_growth_pct", 20) + 10),
                     f"RH book {r['expectancy_r']:+.3f} R/fill over {r['n']} fills — demand more growth before entry", "tighten", ev(r))
        if _f(cfg, "rh_min_inflow_usd", 200) < 3000:
            return P("threshold", "rh_pons", "rh_min_inflow_usd", min(3000.0, _f(cfg, "rh_min_inflow_usd", 200) * 1.5),
                     f"RH book still negative ({r['expectancy_r']:+.3f} R/fill, n={r['n']}) with growth bar maxed — demand more inflow", "tighten", ev(r))
    sc = S("scalp")
    if sc["n"] >= LAST_RESORT_MULT * min_n and (sc.get("expectancy_r") or 0) < 0 and _f(cfg, "min_buyers_for_entry_new", 8) < 40:
        return P("threshold", "scalp", "min_buyers_for_entry_new", int(_f(cfg, "min_buyers_for_entry_new", 8) + 4),
                 f"scalp book {sc['expectancy_r']:+.3f} R/fill over {sc['n']} fills — require more real buyers on new-band entries", "tighten", ev(sc))
    return None


class LearningEngine:
    def __init__(self, db, hub=None, reload_cb=None):
        self.db = db
        self.hub = hub
        self.reload_cb = reload_cb
        self.tick_store = None          # set by server startup — universe replay + post-exit peaks
        self.last: dict = {"books": {}, "proposal": None, "canary": None, "note": ""}

    async def _universe_inputs(self, cfg: dict, trades_7d: list[dict], min_n: int) -> dict:
        """Tick-store-backed inputs for the technique pass: post-exit peaks (for `stopped_then_ran`) and the
        universe replay. Best effort — the Doctor runs without them."""
        ts = self.tick_store
        if ts is None:
            return {}
        out: dict = {"tick_store": dict(ts.stats)}
        peaks: dict = {}
        try:
            for t in trades_7d:
                if (t.get("pnl_usd") or 0) >= 0 or not t.get("exit_time"):
                    continue
                chain = "rh" if t.get("chain") == "rh" else "sol"
                ep = float(t.get("exit_price_quote" if chain == "rh" else "exit_price_sol") or 0)
                from autopsy import _ts
                xt = _ts(t["exit_time"]).timestamp()
                pk = await ts.post_exit_peak_pct(chain, t["mint"], xt, ep)
                if pk is not None:
                    peaks[t.get("id")] = pk
        except Exception as e:
            logger.debug(f"post-exit peaks failed: {e}")
        out["post_peaks"] = peaks
        try:
            from replay import replay_universe
            from rh_discovery import _eth_usd_cache
            from solana_client import _sol_price_cache
            quote_usd = {"ETH": float(_eth_usd_cache.get("price") or 0), "USDG": 1.0, "SOL": float(_sol_price_cache.get("price") or 0)}
            by_book = {b: [t for t in trades_7d if book_of(t) == b] for b in BOOKS}
            out["replay"] = await replay_universe(ts, cfg, quote_usd, min_n, trades_by_book=by_book)
        except Exception as e:
            logger.warning(f"universe replay failed: {e}")
            out["replay"] = {}
        return out

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
        if not cfg.get("doctor_auto_apply_enabled"):
            return False
        if cfg.get("live_trading") and not cfg.get("doctor_auto_apply_live"):
            return False
        return True

    # ---------- main cycle ----------
    @staticmethod
    def _book_note(book: str, st: dict, min_n: int) -> str:
        """One line per book: why the Doctor did or didn't act."""
        n = st.get("n", 0)
        if n == 0:
            return "no fills in the last 24h"
        if n < min_n:
            return f"{n}/{min_n} fills — collecting evidence before acting"
        e = st.get("expectancy_r") or 0.0
        e7 = st.get("expectancy_7d")
        if e < 0 and e7 is not None and e7 > 0:
            return f"24h negative ({e:+.3f} R/fill) but 7d positive ({e7:+.3f}) — treating as noise, not a regime change"
        if e < 0:
            return f"losing {e:+.3f} R/fill — candidate for a corrective canary"
        pr = st.get("payoff_ratio")
        return f"earning {e:+.3f} R/fill" + (f", payoff {pr:.2f}" if pr else "") + " — no structural edge to fix; scale-up needs 7d confirmation"

    async def cycle(self, cfg: dict, trades_24h: list[dict], trades_7d: list[dict]) -> list[dict]:
        """Returns 0–1 suggestion dicts (category 'learning') for the Doctor to
        surface. Handles canary evaluation + optional auto-apply itself."""
        if not cfg.get("doctor_learning_enabled", True):
            return []
        min_n = int(cfg.get("doctor_learning_min_trades_per_book", 15))
        books: dict[str, dict] = {}
        from book_params import ALL_BOOKS
        for b in ALL_BOOKS + ("global",):
            sel = (lambda t: book_of(t) is not None) if b == "global" else (lambda t, _b=b: book_of(t) == _b)
            s24 = book_stats([t for t in trades_24h if sel(t)], cfg)
            s7 = book_stats([t for t in trades_7d if sel(t)], cfg)
            s24["expectancy_7d"] = s7.get("expectancy_r")
            s24["n_7d"] = s7.get("n", 0)
            books[b] = s24
        for b, st in books.items():
            st["note"] = self._book_note(b, st, min_n)
        self.last["books"] = books

        by_book: dict[str, list[dict]] = {b: [] for b in BOOKS}
        for t in trades_7d:
            bk = book_of(t)
            if bk in by_book:
                by_book[bk].append(t)
        extra = await self._universe_inputs(cfg, trades_7d, min_n)
        _, self.last["technique"] = propose_technique(cfg, by_book, min_n, extra)   # always refreshed for the UI
        await self._allocate(cfg, books, trades_7d, min_n)

        can = await self.canary()
        if can and can.get("state") == "running":
            await self._evaluate_canary(can, cfg, trades_24h)
            self.last["canary"] = await self.canary()
            return []  # one change at a time

        proposal = propose(cfg, books, min_n, by_book, extra)
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
                f"{PROMOTION_MIN_FILLS.get(proposal['book'], 30)} fills AFTER the canary start; promoted only if expectancy in R "
                "improves and drawdown is not >15% worse — otherwise reverted and blacklisted 24h. "
                "Optimises R expectancy after fill, never win rate."
            ),
            "actions": proposal.get("actions") or {proposal["key"]: proposal["value"]},
            "confidence": "med",
            "metrics": {"book": proposal["book"], "fingerprint": fp, **{k: (round(v, 6) if isinstance(v, float) else v) for k, v in proposal["evidence"].items()}},
            "learning": True,
        }
        if self.auto_apply_allowed(cfg):
            await self.apply(proposal, cfg, books, auto=True)
            sugg["status"] = "applied"
        return [sugg]

    async def apply(self, proposal: dict, cfg: dict, books: dict | None = None, auto: bool = False) -> dict:
        key, value = proposal["key"], proposal["value"]
        actions = proposal.get("actions") or {key: value}
        for k in actions:
            if not key_ok(k):
                raise ValueError(f"key {k} not allowed")
        from rails import clamp_actions
        actions, rail_notes = clamp_actions(actions, "doctor")
        if not actions:
            raise ValueError("proposal touches only immutable rails: " + "; ".join(rail_notes))
        if rail_notes:
            proposal = {**proposal, "rail_notes": rail_notes, "value": actions.get(key, value)}
        if await self.canary() and (await self.canary()).get("state") == "running":
            raise RuntimeError("canary already running — one change at a time")
        book = proposal["book"]
        st = (books or self.last.get("books") or {}).get(book if book in BOOKS else "global", {})
        baseline = {}
        for k in actions:
            prior = cfg_get(cfg, k)
            if prior is None and "." not in k:  # key never persisted → baseline is the model default
                from models import BotConfig
                prior = BotConfig().model_dump().get(k)
            baseline[k] = prior
        canary = {
            "state": "running",
            "proposal": proposal,
            "book": book,
            "baseline_config_subset": baseline,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "trades_at_start": st.get("n", 0),
            "baseline_expectancy_r": st.get("expectancy_r"),
            "baseline_max_drawdown_usd": st.get("max_drawdown_usd"),
            "auto": auto,
        }
        await self.db.bot_config.update_one({}, {"$set": dict(actions)})   # dotted keys nest natively in Mongo
        for k, v in actions.items():
            cfg_set(cfg, k, v)
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
                 and (book_of(t) is not None if book == "global" else book_of(t) == book)]
        n_req = PROMOTION_MIN_FILLS.get(book or "global", 30)
        try:
            age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(started)).total_seconds() / 3600.0
        except Exception:
            age_h = 0.0
        if len(since) < n_req:
            return   # promotion is judged on post-start fills only — never on the clock
        st = book_stats(since, cfg)
        base_e = can.get("baseline_expectancy_r")
        base_dd = can.get("baseline_max_drawdown_usd") or 0.0
        exp_ok = base_e is not None and (st.get("expectancy_r") or 0) > base_e
        dd_ok = st.get("max_drawdown_usd", 0.0) <= base_dd * 1.15 + 1e-9
        verdict = {"expectancy_r_since": st.get("expectancy_r"), "n_since": st.get("n", 0),
                   "drawdown_since": st.get("max_drawdown_usd"), "age_h": round(age_h, 2)}
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
            to_set = {k: v for k, v in subset.items() if v is not None}
            to_unset = {k: "" for k, v in subset.items() if v is None}
            ops = {}
            if to_set:
                ops["$set"] = to_set
            if to_unset:
                ops["$unset"] = to_unset
            await self.db.bot_config.update_one({}, ops)
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

    async def _allocate(self, cfg: dict, books: dict, trades_7d: list[dict], min_n: int):
        """Desk allocator: continuous per-book weights (floor ×0.25, cap ×2, one step per cycle)."""
        import allocator
        from book_params import ALL_BOOKS
        books7 = {b: book_stats([t for t in trades_7d if book_of(t) == b], cfg) for b in ALL_BOOKS}
        enabled = {"scalp": bool(cfg.get("helius_tracker_enabled", True)), "hunt": bool(cfg.get("creator_greylist_enabled", True)),
                   "rh_pons": bool(cfg.get("rh_paper_enabled", True)), "runner": True}
        rows = allocator.plan(cfg, {b: books.get(b) or {} for b in ALL_BOOKS}, books7, min_n, enabled)
        self.last["allocator"] = {"enabled": bool(cfg.get("allocator_enabled", True)), "driving": bool(cfg.get("autopilot_enabled")),
                                  "rows": rows, "floor": allocator.FLOOR, "cap": allocator.CAP, "step": allocator.STEP}
        if not (cfg.get("allocator_enabled", True) and cfg.get("autopilot_enabled") and cfg.get("doctor_auto_apply_enabled", True)):
            return
        changes = await allocator.apply(self.db, rows, self._reload)
        if changes and self.hub:
            try:
                await self.hub.broadcast("doctor_allocator", {"changes": changes})
            except Exception:
                pass

    async def _reload(self):
        if self.reload_cb:
            try:
                await self.reload_cb()
            except Exception as e:
                logger.warning(f"learning reload_cb failed: {e}")

    async def status(self) -> dict:
        return {"books": self.last.get("books", {}), "proposal": self.last.get("proposal"),
                "canary": await self.canary(), "note": self.last.get("note", ""),
                "technique": self.last.get("technique", {}), "allocator": self.last.get("allocator")}
