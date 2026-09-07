"""
doctor_learning — Strategy Doctor LEARNING POLICY LOOP.

Turns the Doctor from "suggest knobs" into: measure each BOOK (momentum,
greylist_snipe, reentry, rh_pons — SOL and RH alike) on FILL expectancy in USD
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

BOOKS = ("momentum", "greylist_snipe", "reentry", "rh_pons")
BOOK_LABELS = {"momentum": "Momentum book", "greylist_snipe": "Greylist snipe book",
               "reentry": "Re-entry book", "rh_pons": "RH · PONS book (paper)", "global": "All books"}
# Keys the Doctor may move. Shared exit keys (TP/SL/trail/no-momentum) affect
# every book on both chains, so their proposals are scored as book="global".
GLOBAL_KEYS = {"take_profit_pct", "stop_loss_pct", "trailing_stop_pct", "no_momentum_min_mfe_pct",
               "hold_max_seconds", "speed_mode", "scanner_interval_s"}
ALLOWED_KEYS = {
    "book_momentum_size_mult", "book_snipe_size_mult", "greylist_snipe_min_score",
    "greylist_snipe_stale_seconds",
    "reentry_enabled", "reentry_size_multiplier", "reentry_breakout_pct",
    "reentry_min_bounce_pct", "reentry_min_buyers",
    "rh_min_growth_pct", "rh_min_inflow_usd", "rh_min_unique_buyers", "rh_min_curve_pct", "rh_min_mc_usd",
    "min_curve_liquidity_sol", "min_buyers_for_entry", "min_curve_liquidity_sol_new", "min_buyers_for_entry_new",
    "risk_per_trade_pct",
} | GLOBAL_KEYS
FORBIDDEN_KEYS = {"live_trading", "enabled", "daily_kill_switch_usd", "max_trade_usd"}
TECHNIQUE_MIN_GAIN_USD = 0.02        # $/fill a technique change must add to be worth a canary
TECHNIQUE_MIN_GAIN_REL = 0.15        # …and ≥15% of |current expectancy|
LAST_RESORT_MULT = 3                 # size cuts / disables need 3× the minimum sample


def key_ok(key: str) -> bool:
    """Flat whitelist, or a per-book exit override `book_exits.<book>.<param>`."""
    if key in FORBIDDEN_KEYS:
        return False
    if key in ALLOWED_KEYS:
        return True
    parts = key.split(".")
    from book_params import BOOKS as _B, EXIT_PARAMS, REGIMES
    if len(parts) == 3 and parts[0] == "book_exits" and parts[1] in _B and parts[2] in EXIT_PARAMS:
        return True
    if len(parts) == 4 and parts[0] == "book_exits" and parts[1] in _B and parts[2] in REGIMES and parts[3] in EXIT_PARAMS:
        return True
    if len(parts) == 3 and parts[0] == "regime_gate_mult" and parts[1] in _B and parts[2] in REGIMES:
        return True
    return len(parts) == 2 and parts[0] == "regime_busy_threshold" and parts[1] in _B


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


def propose_technique(cfg: dict, trades_by_book: dict[str, list[dict]], min_n: int) -> tuple[dict | None, dict]:
    """Technique first: per book, replay its own fills against the exit grid and split them by entry
    feature; return the single best-gain proposal (or None) plus the full analysis for the UI."""
    from book_params import (BOOKS as _B, REGIME_MULT_CAP, REGIME_MULT_STEP, REGIMES, book_exit_view, entry_feature_splits,
                             entry_pair_splits, regime_splits, trade_regime, whatif_exits)
    analysis: dict = {}
    best: dict | None = None
    for book in _B:
        rows = trades_by_book.get(book) or []
        if book == "momentum":
            by_band = {"new": [t for t in rows if (t.get("entry_ctx") or {}).get("band") == "new"],
                       "seasoned": [t for t in rows if (t.get("entry_ctx") or {}).get("band") != "new"]}
        cur = book_exit_view(cfg, book)
        wi = whatif_exits(rows, cur) if len(rows) >= min_n else {"n": len(rows)}
        splits = entry_feature_splits(rows, book, cfg) if len(rows) >= min_n else []
        if book == "momentum" and len(by_band["new"]) >= min_n:
            splits += entry_feature_splits(by_band["new"], "momentum_new", cfg)
        pairs = []
        if len(rows) >= min_n and not any(sp["actionable"] for sp in splits):
            pairs = entry_pair_splits(rows, book, cfg)   # only when no single feature is clean enough
        regime = regime_splits(rows, book, cfg) if len(rows) >= min_n else None
        # exits per regime: busy hours may deserve a tighter trail than quiet ones
        regime_exits = {}
        for reg in REGIMES:
            sub = [t for t in rows if trade_regime(cfg, book, t) == reg]
            if len(sub) >= min_n:
                regime_exits[reg] = {"n": len(sub), "current_exits": book_exit_view(cfg, book, reg), **whatif_exits(sub, book_exit_view(cfg, book, reg))}
        analysis[book] = {"n": len(rows), "current_exits": cur, "whatif": wi, "splits": splits[:6], "pairs": pairs[:3],
                          "regime": regime, "regime_exits": regime_exits}
        if len(rows) < min_n:
            continue
        cands = []
        b = wi.get("best")
        if b and wi["gain_usd_per_fill"] >= max(TECHNIQUE_MIN_GAIN_USD, TECHNIQUE_MIN_GAIN_REL * abs(wi["current"]["expectancy_usd"]))                 and float(b["value"]) != float(cur[b["param"]]):
            cands.append({"type": "threshold", "book": book, "key": f"book_exits.{book}.{b['param']}", "value": b["value"],
                          "gain": wi["gain_usd_per_fill"],
                          "reason": (f"replaying {wi['n']} {book} fills: {b['param']} {cur[b['param']]:g} → {b['value']:g} lifts expectancy "
                                     f"{wi['current']['expectancy_usd']:+.4f} → {b['expectancy_usd']:+.4f} $/fill (median MFE {wi['median_mfe']:+.1f}%, MAE {wi['median_mae']:+.1f}%)"),
                          "direction": "raise expectancy by tuning this book's exit — measured on its own fills",
                          "evidence": {"n": wi["n"], "expectancy_now": wi["current"]["expectancy_usd"], "expectancy_whatif": b["expectancy_usd"],
                                       "median_mfe": wi["median_mfe"], "median_mae": wi["median_mae"], "grid": wi["rows"][:24]}})
        for sp in splits:
            if sp["actionable"] and sp["gain_usd_per_fill"] >= TECHNIQUE_MIN_GAIN_USD:
                val = round(sp["split"], 2) if isinstance(cfg.get(sp["key"], 0.0), float) else int(round(sp["split"]))
                cands.append({"type": "threshold", "book": book, "key": sp["key"], "value": val, "gain": sp["gain_usd_per_fill"],
                              "reason": (f"{book} fills with {sp['feature']} < {sp['split']:g} lose {sp['low_expectancy_usd']:+.4f} $/fill, "
                                         f"above earn {sp['high_expectancy_usd']:+.4f} (n={sp['n']}) → raise {sp['key']} {sp['current']:g} → {val:g}"),
                              "direction": "raise expectancy by filtering the entries that lose — measured, not guessed",
                              "evidence": {k: sp[k] for k in ("feature", "n", "split", "current", "low_expectancy_usd", "high_expectancy_usd")}})
                break  # one entry-filter candidate per book (the top-gain one)
        for pr in pairs:
            if pr["actionable"] and pr["gain_usd_per_fill"] >= TECHNIQUE_MIN_GAIN_USD:
                acts = {}
                for k, split, curv in zip(pr["keys"], pr["splits"], pr["current"]):
                    if split > curv:
                        acts[k] = round(split, 2) if isinstance(cfg.get(k, 0.0), float) else int(round(split))
                if not acts:
                    break
                cands.append({"type": "threshold", "book": book, "key": "+".join(acts), "value": acts, "actions": acts,
                              "gain": pr["gain_usd_per_fill"],
                              "reason": (f"{book}: no single gate separates winners, but fills with {pr['features'][0]} ≥ {pr['splits'][0]:g} AND "
                                         f"{pr['features'][1]} ≥ {pr['splits'][1]:g} earn {pr['high_high_expectancy_usd']:+.4f} $/fill (n={pr['high_high_n']}) "
                                         f"while the rest lose {pr['rest_expectancy_usd']:+.4f} → raise both gates together"),
                              "direction": "raise expectancy by requiring both signals at entry — measured on this book's fills",
                              "evidence": {k: pr[k] for k in ("features", "n", "splits", "current", "high_high_n", "high_high_expectancy_usd", "rest_expectancy_usd")}})
                break
        for reg, rw in regime_exits.items():
            rb = rw.get("best")
            if not rb:
                continue
            cur_r = rw["current_exits"]
            # weight the regime gain by its share of fills so it competes fairly with book-wide changes
            gain_w = rw["gain_usd_per_fill"] * rw["n"] / max(1, len(rows))
            if rw["gain_usd_per_fill"] >= max(TECHNIQUE_MIN_GAIN_USD, TECHNIQUE_MIN_GAIN_REL * abs(rw["current"]["expectancy_usd"])) \
                    and float(rb["value"]) != float(cur_r[rb["param"]]):
                cands.append({"type": "threshold", "book": book, "key": f"book_exits.{book}.{reg}.{rb['param']}", "value": rb["value"],
                              "gain": gain_w,
                              "reason": (f"{book} fills entered in {reg} hours ({rw['n']}): {rb['param']} {cur_r[rb['param']]:g} → {rb['value']:g} lifts "
                                         f"{rw['current']['expectancy_usd']:+.4f} → {rb['expectancy_usd']:+.4f} $/fill in that regime only"),
                              "direction": f"raise expectancy with a {reg}-hour exit ladder for this book — measured on its {reg}-hour fills",
                              "evidence": {"regime": reg, "n": rw["n"], "expectancy_now": rw["current"]["expectancy_usd"],
                                           "expectancy_whatif": rb["expectancy_usd"], "grid": rw["rows"][:24]}})
        if regime and regime["actionable"] and regime["gain_usd_per_fill"] >= TECHNIQUE_MIN_GAIN_USD:
            lose = regime["losing"]
            new_mult = round(min(REGIME_MULT_CAP, regime["current_mult"] + REGIME_MULT_STEP), 2)
            acts = {f"regime_gate_mult.{book}.{lose}": new_mult, f"regime_busy_threshold.{book}": round(regime["threshold_per_h"], 1)}
            cands.append({"type": "threshold", "book": book, "key": "+".join(acts), "value": acts, "actions": acts,
                          "gain": regime["gain_usd_per_fill"],
                          "reason": (f"{book} in {lose} hours (launch rate {'<' if lose == 'quiet' else '≥'} {regime['threshold_per_h']:g}/h) "
                                     f"earns {regime[lose]['expectancy_usd']:+.4f} $/fill (n={regime[lose]['n']}) vs "
                                     f"{regime['quiet' if lose == 'busy' else 'busy'][ 'expectancy_usd']:+.4f} in the other regime → "
                                     f"tighten every {book} entry gate ×{new_mult:g} during {lose} hours"),
                          "direction": "raise expectancy by demanding more from entries in the regime that loses",
                          "evidence": regime})
        for c in cands:
            if best is None or c["gain"] > best["gain"]:
                best = c
    if best:
        prop = {"type": best["type"], "book": best["book"], "key": best["key"], "value": best["value"], "reason": best["reason"],
                "expected_direction": best["direction"], "evidence": best["evidence"], "technique": True}
        if best.get("actions"):
            prop["actions"] = best["actions"]
        return prop, analysis
    return None, analysis
CANARY_ID = "current"


def book_of(t: dict) -> str | None:
    """Which learning book a closed trade belongs to. Manual buys are the
    operator's decision, not a tunable strategy → None (still shown in P/L)."""
    a = t.get("classifier_action") or ""
    if a in ("manual", "rh_pons_manual"):
        return None
    if t.get("chain") == "rh" or a.startswith("rh_pons"):
        return "rh_pons"          # chain wins: legacy RH docs carry the model default book="momentum"
    b = t.get("book")
    if b in BOOKS:
        return b
    if a == "reentry":
        return "reentry"
    return "greylist_snipe" if a == "greylist_snipe" else "momentum"


def book_size_mult(cfg, action: str) -> float:
    """Sizing helper used by bot._enter / reentry. 0.0 ⇒ skip that book."""
    key = "book_snipe_size_mult" if action == "greylist_snipe" else "book_momentum_size_mult"
    v = cfg.get(key, 1.0) if isinstance(cfg, dict) else getattr(cfg, key, 1.0)
    try:
        return max(0.0, float(v if v is not None else 1.0))
    except (TypeError, ValueError):
        return 1.0


def _pnl(t: dict) -> float | None:
    """USD after-fee realised PnL — the one unit SOL and RH books share."""
    v = t.get("pnl_usd")
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
    tp = float((cfg or {}).get("take_profit_pct") or 0) if cfg else 0.0
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
        "expectancy_usd": statistics.mean(pnls),
        "total_usd": sum(pnls),
        "winrate": len(wins) / n * 100.0,
        "winner_mean_usd": winner_mean,
        "loser_mean_usd": loser_mean,
        "payoff_ratio": (winner_mean / abs(loser_mean)) if (winner_mean is not None and loser_mean) else None,
        "median_hold_s": statistics.median(holds) if holds else None,
        "median_mfe": statistics.median(mfes) if mfes else None,
        "winners_median_mfe": statistics.median(win_mfes) if win_mfes else None,
        "mfe_over_tp_share": (sum(1 for m in win_mfes if tp and m >= 1.5 * tp) / len(win_mfes) * 100.0) if (win_mfes and tp) else None,
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


def propose(cfg: dict, stats: dict[str, dict], min_n: int, trades_by_book: dict[str, list[dict]] | None = None) -> dict | None:
    """Priority ladder — first match wins. Every rule is scored on USD
    expectancy per fill (profit), never on win rate. Order: TECHNIQUE (per-book exit grid,
    data-driven entry filters) → profit-shape rules → scale winners → LAST RESORT size cuts.
    Returns None when nothing qualifies."""
    if trades_by_book is not None:
        tech, _ = propose_technique(cfg, trades_by_book, min_n)
        if tech:
            return tech
    def P(kind, book, key, value, reason, direction, evidence):
        assert key_ok(key)
        return {"type": kind, "book": book, "key": key, "value": value, "reason": reason,
                "expected_direction": direction, "evidence": evidence}

    def S(book):
        return stats.get(book) or {"n": 0}

    def ev(st, **extra):
        base = {"expectancy_usd": st.get("expectancy_usd"), "n": st.get("n"), "winrate": st.get("winrate"),
                "total_usd": st.get("total_usd")}
        base.update(extra)
        return base

    mom, sn, re_, rh = S("momentum"), S("greylist_snipe"), S("reentry"), S("rh_pons")
    # 1. a losing book (negative USD expectancy over a real sample, and not
    #    just a bad hour: the 7d expectancy must not contradict it)
    for book, st in (("momentum", mom), ("greylist_snipe", sn), ("reentry", re_), ("rh_pons", rh)):
        # LAST RESORT: only after technique found nothing, on 3× the sample, and never while a
        # smaller fix is plausible on a thin sample
        if st["n"] < LAST_RESORT_MULT * min_n or st["expectancy_usd"] >= 0:
            continue
        if (st.get("expectancy_7d") or 0) > 0 and st.get("n_7d", 0) >= 3 * min_n:
            continue
        if book == "momentum" and _f(cfg, "book_momentum_size_mult", 1.0) > 0:
            return P("flag", book, "book_momentum_size_mult", 0.0,
                     f"momentum book expectancy {st['expectancy_usd']:+.4f} $/fill over {st['n']} fills (total {st['total_usd']:+.2f} $) — disable the losing machine",
                     "raise expectancy by cutting losing book", ev(st))
        if book == "greylist_snipe" and _f(cfg, "book_snipe_size_mult", 1.0) > 0:
            if st["n"] >= 3 * min_n and _f(cfg, "greylist_snipe_min_score", 45) < 70 and st["expectancy_usd"] > -0.10:
                new = min(80.0, _f(cfg, "greylist_snipe_min_score", 45) + 5)
                return P("threshold", book, "greylist_snipe_min_score", new,
                         f"snipe book slightly negative ({st['expectancy_usd']:+.4f} $/fill, n={st['n']}); raise score bar before disabling",
                         "raise expectancy by filtering weakest snipes", ev(st))
            return P("flag", book, "book_snipe_size_mult", 0.0,
                     f"snipe book expectancy {st['expectancy_usd']:+.4f} $/fill over {st['n']} fills (total {st['total_usd']:+.2f} $) — disable the losing machine",
                     "raise expectancy by cutting losing book", ev(st))
        if book == "reentry" and cfg.get("reentry_enabled", True):
            bt = st.get("by_trigger") or {}
            bo, pb = bt.get("breakout", {"n": 0}), bt.get("pullback", {"n": 0})
            half = max(3, min_n // 2)
            if bo.get("n", 0) >= half and bo["expectancy_usd"] < 0 and (pb.get("n", 0) < half or pb["expectancy_usd"] >= 0) \
                    and _f(cfg, "reentry_breakout_pct", 5) < 60:
                return P("threshold", book, "reentry_breakout_pct", min(60.0, _f(cfg, "reentry_breakout_pct", 5) + 10),
                         f"breakout re-entries lose {bo['expectancy_usd']:+.4f} $/fill (n={bo['n']}) while pullbacks hold up — demand a bigger breakout",
                         "raise expectancy by cutting the losing trigger", ev(st, breakout=bo, pullback=pb))
            if pb.get("n", 0) >= half and pb["expectancy_usd"] < 0 and _f(cfg, "reentry_min_bounce_pct", 5) < 30:
                return P("threshold", book, "reentry_min_bounce_pct", min(30.0, _f(cfg, "reentry_min_bounce_pct", 5) + 5),
                         f"pullback re-entries lose {pb['expectancy_usd']:+.4f} $/fill (n={pb['n']}) — require a bigger run-on before buying the dip",
                         "raise expectancy by filtering weak pullbacks", ev(st, breakout=bo, pullback=pb))
            if pb.get("n", 0) >= half and pb["expectancy_usd"] < 0 and int(_f(cfg, "reentry_min_buyers", 2)) < 5:
                return P("threshold", book, "reentry_min_buyers", int(_f(cfg, "reentry_min_buyers", 2)) + 1,
                         f"pullback re-entries still negative ({pb['expectancy_usd']:+.4f} $/fill, n={pb['n']}) with the bounce bar maxed — require more buyers",
                         "raise expectancy by filtering weak pullbacks", ev(st, pullback=pb))
            return P("flag", book, "reentry_enabled", False,
                     f"re-entry book expectancy {st['expectancy_usd']:+.4f} $/fill over {st['n']} fills (total {st['total_usd']:+.2f} $) after trigger filters — switch it off",
                     "raise expectancy by cutting losing book", ev(st, by_trigger=bt))
        if book == "rh_pons":
            if _f(cfg, "rh_min_growth_pct", 30) < 100:
                return P("threshold", book, "rh_min_growth_pct", min(100.0, _f(cfg, "rh_min_growth_pct", 30) + 10),
                         f"RH paper book expectancy {st['expectancy_usd']:+.4f} $/fill over {st['n']} fills — demand more growth before entry",
                         "raise expectancy by taking fewer, stronger RH entries", ev(st))
            if _f(cfg, "rh_min_inflow_usd", 300) < 2000:
                return P("threshold", book, "rh_min_inflow_usd", min(2000.0, _f(cfg, "rh_min_inflow_usd", 300) + 100),
                         f"RH paper book still negative ({st['expectancy_usd']:+.4f} $/fill, n={st['n']}) with growth bar maxed — demand more inflow",
                         "raise expectancy by taking fewer, stronger RH entries", ev(st))
    # 1b. bankroll risk dial DOWN: the whole machine is losing → risk less per trade
    g = S("global")
    if cfg.get("bankroll_sizing_enabled") and g["n"] >= LAST_RESORT_MULT * min_n and g["expectancy_usd"] < 0 \
            and (g.get("expectancy_7d") is None or g["expectancy_7d"] <= 0) and _f(cfg, "risk_per_trade_pct", 2.0) > 0.5:
        return P("threshold", "global", "risk_per_trade_pct", round(max(0.5, _f(cfg, "risk_per_trade_pct", 2.0) - 0.5), 2),
                 f"all books together lose {g['expectancy_usd']:+.4f} $/fill over {g['n']} fills — risk less of the bankroll per trade while the edge is missing",
                 "protect bankroll by shrinking stake while expectancy is negative", ev(g))
    # 2. profit shape on the shared exit keys (scored across every book)
    if g["n"] >= min_n:
        wm, lm = g.get("winner_mean_usd"), g.get("loser_mean_usd")
        sl = _f(cfg, "stop_loss_pct", 20)
        if wm and lm and abs(lm) > 1.5 * wm and (g.get("sl_share") or 0) > 30 and sl > 10:
            return P("threshold", "global", "stop_loss_pct", max(10.0, sl - 3),
                     f"average loser (-{abs(lm):.3f} $) is {abs(lm) / wm:.1f}x the average winner (+{wm:.3f} $) and {g['sl_share']:.0f}% of exits are stop-loss — cut losers sooner",
                     "raise expectancy by shrinking the average loss", ev(g, winner_mean_usd=wm, loser_mean_usd=lm, sl_share=g.get("sl_share")))
        tp = _f(cfg, "take_profit_pct", 45)
        if (g.get("mfe_over_tp_share") or 0) > 40 and (g.get("tp_share") or 0) > 40 and tp < 100:
            return P("threshold", "global", "take_profit_pct", min(100.0, tp + 5),
                     f"{g['mfe_over_tp_share']:.0f}% of winners ran ≥1.5x past TP ({tp:g}%) and {g['tp_share']:.0f}% of exits are TP — money left on the table",
                     "raise expectancy by letting winners run", ev(g, mfe_over_tp_share=g.get("mfe_over_tp_share"), tp_share=g.get("tp_share"),
                                                                    winners_median_mfe=g.get("winners_median_mfe")))
    # 3. momentum giveback
    if mom["n"] >= min_n and (mom.get("median_giveback") or 0) > 10 and _f(cfg, "trailing_stop_pct", 6) > 4:
        new = max(4.0, _f(cfg, "trailing_stop_pct", 6) - 1)
        return P("threshold", "global", "trailing_stop_pct", new,
                 f"momentum winners give back a median {mom['median_giveback']:.1f}pp of their peak",
                 "raise expectancy by cutting giveback",
                 {"median_giveback": mom["median_giveback"], "median_mfe": mom.get("median_mfe"), "n": mom["n"]})
    # 4. stale / timeout heavy book
    for book, st in (("greylist_snipe", sn), ("momentum", mom)):
        if st["n"] >= min_n and st["stale_timeout_share"] > 40 and (st.get("stale_timeout_mean_pnl") or 0) < 0:
            if book == "greylist_snipe":
                cur = int(_f(cfg, "greylist_snipe_stale_seconds", 60))
                if cur > 30:
                    return P("threshold", book, "greylist_snipe_stale_seconds", max(30, cur - 15),
                             f"{st['stale_timeout_share']:.0f}% of snipe exits are stale/timeout with mean {st['stale_timeout_mean_pnl']:+.4f} $",
                             "raise expectancy by cutting dead holds", {"share": st["stale_timeout_share"], "n": st["n"]})
            else:
                cur = int(_f(cfg, "hold_max_seconds", 35))
                if cur > 20:
                    return P("threshold", "global", "hold_max_seconds", max(20, cur - 5),
                             f"{st['stale_timeout_share']:.0f}% of momentum exits are timeout with mean {st['stale_timeout_mean_pnl']:+.4f} $",
                             "raise expectancy by cutting dead holds", {"share": st["stale_timeout_share"], "n": st["n"]})
    # 5. latency tax
    for book, st in (("momentum", mom), ("greylist_snipe", sn)):
        if st["n"] >= min_n and (st.get("median_latency_tax") or 0) > 8:
            mode = str(cfg.get("speed_mode", "manual"))
            if mode in ("eco", "manual"):
                return P("flag", "global", "speed_mode", "fast",
                         f"{book} median latency tax {st['median_latency_tax']:.1f}pp (decision→fill); TP is NOT loosened",
                         "raise expectancy by landing exits closer to decision price", {"latency_tax": st["median_latency_tax"]})
            cur = int(_f(cfg, "scanner_interval_s", 15))
            return P("threshold", "global", "scanner_interval_s", min(60, cur + 5),
                     f"latency tax {st['median_latency_tax']:.1f}pp at speed_mode={mode}; reduce entry rate instead of loosening TP",
                     "raise expectancy by taking fewer, better fills", {"latency_tax": st["median_latency_tax"]})
    # 6. scale what is making money (profit, not win rate): a book with
    #    positive 24h AND 7d expectancy on a solid sample earns more size
    for book, st, key, step, cap in (("momentum", mom, "book_momentum_size_mult", 0.25, 2.0),
                                     ("greylist_snipe", sn, "book_snipe_size_mult", 0.25, 2.0),
                                     ("reentry", re_, "reentry_size_multiplier", 0.1, 1.0)):
        if st["n"] >= 2 * min_n and st["expectancy_usd"] > 0 and (st.get("expectancy_7d") or 0) > 0 \
                and (st.get("payoff_ratio") or 0) >= 1.0:
            cur = _f(cfg, key, 1.0 if key != "reentry_size_multiplier" else 0.5)
            if cur > 0 and cur < cap:
                return P("threshold", book, key, round(min(cap, cur + step), 2),
                         f"{book} book earns {st['expectancy_usd']:+.4f} $/fill (n={st['n']}, 7d {st['expectancy_7d']:+.4f}) with payoff {st['payoff_ratio']:.2f} — scale the winner",
                         "raise total profit by sizing up a positive-expectancy book", ev(st, payoff_ratio=st.get("payoff_ratio")))
    # 7. bankroll risk dial UP: positive 24h AND 7d expectancy across the
    #    machine with payoff ≥ 1 → compound harder (Doctor-steered, capped 5%)
    if cfg.get("bankroll_sizing_enabled") and g["n"] >= 2 * min_n and g["expectancy_usd"] > 0 \
            and (g.get("expectancy_7d") or 0) > 0 and (g.get("payoff_ratio") or 0) >= 1.0 \
            and _f(cfg, "risk_per_trade_pct", 2.0) < 5.0:
        return P("threshold", "global", "risk_per_trade_pct", round(min(5.0, _f(cfg, "risk_per_trade_pct", 2.0) + 0.5), 2),
                 f"machine earns {g['expectancy_usd']:+.4f} $/fill (n={g['n']}, 7d {g['expectancy_7d']:+.4f}) with payoff {g['payoff_ratio']:.2f} — risk a little more of the bankroll per trade",
                 "raise total profit by compounding a positive-expectancy machine", ev(g, payoff_ratio=g.get("payoff_ratio")))
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
        e = st.get("expectancy_usd") or 0.0
        e7 = st.get("expectancy_7d")
        if e < 0 and e7 is not None and e7 > 0:
            return f"24h negative ({e:+.3f} $/fill) but 7d positive ({e7:+.3f}) — treating as noise, not a regime change"
        if e < 0:
            return f"losing {e:+.3f} $/fill — candidate for a corrective canary"
        pr = st.get("payoff_ratio")
        return f"earning {e:+.3f} $/fill" + (f", payoff {pr:.2f}" if pr else "") + " — no structural edge to fix; scale-up needs 7d confirmation"

    async def cycle(self, cfg: dict, trades_24h: list[dict], trades_7d: list[dict]) -> list[dict]:
        """Returns 0–1 suggestion dicts (category 'learning') for the Doctor to
        surface. Handles canary evaluation + optional auto-apply itself."""
        if not cfg.get("doctor_learning_enabled", True):
            return []
        min_n = int(cfg.get("doctor_learning_min_trades_per_book", 15))
        books: dict[str, dict] = {}
        for b in BOOKS + ("global",):
            sel = (lambda t: book_of(t) is not None) if b == "global" else (lambda t, _b=b: book_of(t) == _b)
            s24 = book_stats([t for t in trades_24h if sel(t)], cfg)
            s7 = book_stats([t for t in trades_7d if sel(t)], cfg)
            s24["expectancy_7d"] = s7.get("expectancy_usd")
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
        _, self.last["technique"] = propose_technique(cfg, by_book, min_n)   # always refreshed for the UI

        can = await self.canary()
        if can and can.get("state") == "running":
            await self._evaluate_canary(can, cfg, trades_24h)
            self.last["canary"] = await self.canary()
            return []  # one change at a time

        proposal = propose(cfg, books, min_n, by_book)
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
                "Optimises USD expectancy after fill (profit), not win rate."
            ),
            "actions": proposal.get("actions") or {proposal["key"]: proposal["value"]},
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
        actions = proposal.get("actions") or {key: value}
        for k in actions:
            if not key_ok(k):
                raise ValueError(f"key {k} not allowed")
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
            "baseline_expectancy_usd": st.get("expectancy_usd"),
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
        n_req = int(cfg.get("doctor_learning_canary_trades", 12))
        hours_req = float(cfg.get("doctor_learning_canary_hours", 6.0))
        try:
            age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(started)).total_seconds() / 3600.0
        except Exception:
            age_h = 0.0
        if len(since) < n_req and age_h < hours_req:
            return
        st = book_stats(since, cfg)
        base_e = can.get("baseline_expectancy_usd", can.get("baseline_expectancy_sol"))
        base_dd = can.get("baseline_max_drawdown_usd", can.get("baseline_max_drawdown_sol")) or 0.0
        exp_ok = st.get("n", 0) >= max(3, n_req // 2) and base_e is not None and st["expectancy_usd"] > base_e
        dd_ok = st.get("max_drawdown_usd", 0.0) <= base_dd * 1.15 + 1e-9
        verdict = {"expectancy_since": st.get("expectancy_usd"), "n_since": st.get("n", 0),
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

    async def _reload(self):
        if self.reload_cb:
            try:
                await self.reload_cb()
            except Exception as e:
                logger.warning(f"learning reload_cb failed: {e}")

    async def status(self) -> dict:
        return {"books": self.last.get("books", {}), "proposal": self.last.get("proposal"),
                "canary": await self.canary(), "note": self.last.get("note", ""),
                "technique": self.last.get("technique", {})}
