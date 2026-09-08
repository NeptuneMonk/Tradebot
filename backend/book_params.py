"""Per-book exit parameters + the Doctor's counterfactual / entry-feature analysis.

`cfg.book_exits = {book: {take_profit_pct, stop_loss_pct, trailing_stop_pct, trailing_arm_pct, hold_max_seconds}}`
overrides the shared globals for that book only, so RH data never moves Solana exits (and vice-versa).
"""
from __future__ import annotations

import statistics

BOOKS = ("momentum", "greylist_snipe", "reentry", "rh_pons")
EXIT_PARAMS = ("take_profit_pct", "stop_loss_pct", "trailing_stop_pct", "trailing_arm_pct", "hold_max_seconds")
EXIT_GRID = {  # candidate values the optimizer may propose
    "take_profit_pct": [10, 15, 20, 25, 30, 40, 50, 60, 80, 100],
    "stop_loss_pct": [8, 10, 12, 15, 20, 25, 30, 35, 40],
    "trailing_stop_pct": [3, 4, 5, 6, 8, 10, 12, 15],
    "trailing_arm_pct": [5, 8, 10, 12, 15, 20, 30],
    "hold_max_seconds": [20, 30, 45, 60, 90, 120, 180, 300],
}
# entry feature (in trade.entry_ctx) → config key raised to the split point, per book
ENTRY_FEATURES = {
    "rh_pons": {"growth_pct": "rh_min_growth_pct", "inflow_usd": "rh_min_inflow_usd",
                "unique_buyers": "rh_min_unique_buyers", "curve_fill_pct": "rh_min_curve_pct", "mc_usd": "rh_min_mc_usd"},
    "momentum": {"curve_liquidity_sol": "min_curve_liquidity_sol", "unique_buyers": "min_buyers_for_entry", "project_score": "project_score_min",
                 "creator_prior_launches": "serial_creator_min_launches"},
    "momentum_new": {"curve_liquidity_sol": "min_curve_liquidity_sol_new", "unique_buyers": "min_buyers_for_entry_new", "project_score": "project_score_min"},
    "greylist_snipe": {"creator_score": "greylist_snipe_min_score"},
}
CEILING_KEYS = {"serial_creator_min_launches", "rh_max_growth_pct"}   # keys where LOWER is stricter
FEATURE_CAPS = {"rh_min_growth_pct": 150.0, "rh_min_inflow_usd": 3000.0, "rh_min_unique_buyers": 40, "rh_min_curve_pct": 40.0,
                "rh_min_mc_usd": 50000.0, "min_curve_liquidity_sol": 60.0, "min_buyers_for_entry": 30,
                "min_curve_liquidity_sol_new": 80.0, "min_buyers_for_entry_new": 40, "greylist_snipe_min_score": 85.0,
                "project_score_min": 4, "serial_creator_min_launches": 50}


REGIMES = ("quiet", "busy")
REGIME_MULT_STEP, REGIME_MULT_CAP = 0.5, 3.0


def regime_for(cfg, book: str, launch_rate_per_h: float) -> str:
    """'busy' when the chain is launching faster than this book's threshold (Doctor-set, default 30/h)."""
    thr = float(((_get(cfg, "regime_busy_threshold") or {}).get(book)) or _get(cfg, "regime_busy_default_per_h", 30.0) or 30.0)
    return "busy" if launch_rate_per_h >= thr else "quiet"


def regime_gate_mult(cfg, book: str, launch_rate_per_h: float) -> float:
    """Multiplier applied to this book's ENTRY thresholds in the current regime (1.0 = untouched)."""
    reg = regime_for(cfg, book, launch_rate_per_h)
    m = ((_get(cfg, "regime_gate_mult") or {}).get(book) or {}).get(reg)
    return float(m) if m is not None else 1.0


def regime_splits(trades: list[dict], book: str, cfg, min_side: int = 5) -> dict | None:
    """Quiet vs busy launch hours for this book's fills (split at the median launch rate at entry)."""
    rows = []
    for t in trades:
        r, p = (t.get("entry_ctx") or {}).get("launch_rate_per_h"), t.get("pnl_usd")
        if r is None or p is None:
            continue
        try:
            rows.append((float(r), float(p)))
        except (TypeError, ValueError):
            pass
    if len(rows) < 2 * min_side:
        return None
    med = statistics.median(r for r, _ in rows)
    quiet = [p for r, p in rows if r < med]
    busy = [p for r, p in rows if r >= med]
    if len(quiet) < min_side or len(busy) < min_side:
        return None
    e_q, e_b = statistics.mean(quiet), statistics.mean(busy)
    losing = "quiet" if e_q < e_b else "busy"
    cur_mult = ((_get(cfg, "regime_gate_mult") or {}).get(book) or {}).get(losing, 1.0)
    return {"n": len(rows), "threshold_per_h": med, "quiet": {"n": len(quiet), "expectancy_usd": e_q},
            "busy": {"n": len(busy), "expectancy_usd": e_b}, "losing": losing, "current_mult": float(cur_mult),
            "actionable": min(e_q, e_b) < 0 < max(e_q, e_b) and float(cur_mult) < REGIME_MULT_CAP,
            "gain_usd_per_fill": max(e_q, e_b) - statistics.mean(p for _, p in rows)}


def book_for_action(action: str | None, chain: str | None = None) -> str:
    a = action or ""
    if chain == "rh" or a.startswith("rh_pons"):
        return "rh_pons"
    if a == "reentry":
        return "reentry"
    return "greylist_snipe" if a == "greylist_snipe" else "momentum"


def _get(cfg, key, default=None):
    return cfg.get(key, default) if isinstance(cfg, dict) else getattr(cfg, key, default)


def exit_param(cfg, book: str, param: str, regime: str | None = None) -> float:
    """Regime override (book_exits.<book>.<regime>.<param>) → book override → shared global → model default."""
    ov = (_get(cfg, "book_exits") or {}).get(book) or {}
    v = None
    if regime and isinstance(ov.get(regime), dict):
        v = ov[regime].get(param)
    if v is None:
        v = ov.get(param)
    if v is None:
        v = _get(cfg, param)
    if v is None:
        from models import BotConfig
        v = getattr(BotConfig(), param)
    return float(v)


def book_exit_view(cfg, book: str, regime: str | None = None) -> dict:
    return {p: exit_param(cfg, book, p, regime) for p in EXIT_PARAMS}


def trade_regime(cfg, book: str, trade: dict) -> str | None:
    """Regime the trade was ENTERED in (its exit ladder is chosen once, at entry)."""
    rate = (trade.get("entry_ctx") or {}).get("launch_rate_per_h")
    return regime_for(cfg, book, float(rate)) if rate is not None else None


# ---------------- counterfactual exit optimizer ----------------
def _path(t: dict) -> dict | None:
    """Per-trade excursion summary from what the engines record."""
    ep = t.get("entry_price_quote") or t.get("entry_price_sol") or 0
    if not ep:
        return None
    pk = t.get("peak_price_quote") or t.get("peak_price_sol") or ep
    tr_rec = t.get("trough_price_quote") or t.get("trough_price_sol")
    tr = tr_rec or min(ep, t.get("exit_price_quote") or t.get("exit_price_sol") or ep)
    try:
        pnl_pct = float(t.get("pnl_pct"))
        entry_usd = float(t.get("entry_usd") or 0)
        pnl_usd = float(t.get("pnl_usd"))
    except (TypeError, ValueError):
        return None
    if entry_usd <= 0:
        return None
    hold = None
    try:
        from datetime import datetime
        e = datetime.fromisoformat(str(t["entry_time"]).replace("Z", "+00:00"))
        x = datetime.fromisoformat(str(t["exit_time"]).replace("Z", "+00:00"))
        hold = (x - e).total_seconds()
    except Exception:
        pass
    return {"mfe": (pk / ep - 1) * 100, "mae": (tr / ep - 1) * 100, "pnl_pct": pnl_pct, "pnl_usd": pnl_usd,
            "entry_usd": entry_usd, "peak_first": (t.get("peak_ts") or 0) <= (t.get("trough_ts") or 0),
            "hold_s": hold, "peak_hold_s": t.get("peak_hold_s"), "mae_recorded": bool(tr_rec)}


def _simulate(p: dict, params: dict, fee_pct: float) -> float:
    """USD result of this trade under the exit ladder `params`. Unknown paths fall back to what really happened."""
    tp, sl = float(params["take_profit_pct"]), float(params["stop_loss_pct"])
    trail, arm = float(params["trailing_stop_pct"]), float(params["trailing_arm_pct"])
    hold = float(params.get("hold_max_seconds") or 0) or None
    hit_tp, hit_sl = p["mfe"] >= tp - 1e-6, p["mae"] <= -sl + 1e-6
    if hit_tp and hit_sl:
        first = "tp" if p["peak_first"] else "sl"
    elif hit_tp:
        first = "tp"
    elif hit_sl:
        first = "sl"
    else:
        first = None
    # max-hold cut before the real exit: before the peak → assume flat (fees only); after the peak →
    # interpolate between the peak and the real exit. Longer holds than the real one can't be known.
    if hold and p.get("hold_s") and p["hold_s"] > hold and p.get("peak_hold_s") is not None and first != "sl":
        if not (first == "tp" and p["peak_hold_s"] <= hold):
            if p["peak_hold_s"] >= hold:
                return -p["entry_usd"] * fee_pct / 100.0
            frac = (hold - p["peak_hold_s"]) / max(1e-9, p["hold_s"] - p["peak_hold_s"])
            at_cut = p["mfe"] + (p["pnl_pct"] - p["mfe"]) * frac
            return p["entry_usd"] * (at_cut - fee_pct) / 100.0
    if first == "sl":
        return -p["entry_usd"] * (sl + fee_pct) / 100.0
    if first == "tp":
        return p["entry_usd"] * (tp - fee_pct) / 100.0
    # trailing stop: armed once the run reached `arm`; fires if the price later gave back ≥ `trail` from the peak
    if p["mfe"] >= arm - 1e-6 and p["mfe"] > 0:
        drop_from_peak = (1.0 - (1.0 + p["pnl_pct"] / 100.0) / (1.0 + p["mfe"] / 100.0)) * 100.0
        if drop_from_peak >= trail - 1e-6:
            exit_pct = ((1.0 + p["mfe"] / 100.0) * (1.0 - trail / 100.0) - 1.0) * 100.0
            return p["entry_usd"] * (exit_pct - fee_pct) / 100.0
    return p["pnl_usd"]


def whatif_exits(trades: list[dict], cur: dict, fee_pct: float = 2.0) -> dict:
    """Grid-search TP / SL / hold for one book on its own closed fills. Returns the current-setting
    expectancy, the best single-parameter change and the full grid rows for the UI."""
    paths = [p for p in (_path(t) for t in trades) if p]
    n = len(paths)
    if n == 0:
        return {"n": 0}
    from models import BotConfig
    _d = BotConfig()
    cur_params = {k: float(cur.get(k, getattr(_d, k))) for k in EXIT_PARAMS}

    def exp_for(**over):
        params = {**cur_params, **over}
        return statistics.mean(_simulate(p, params, fee_pct) for p in paths)

    base = exp_for()
    rows = [{"param": param, "value": v, "expectancy_usd": exp_for(**{param: v})}
            for param in ("take_profit_pct", "stop_loss_pct", "trailing_stop_pct", "trailing_arm_pct", "hold_max_seconds")
            for v in EXIT_GRID[param]]
    have_peak_timing = sum(1 for p in paths if p.get("peak_hold_s") is not None and p.get("hold_s")) >= max(3, (2 * n) // 3)
    if not have_peak_timing:
        rows = [r for r in rows if r["param"] != "hold_max_seconds"]
    have_mae = sum(1 for p in paths if p["mae_recorded"]) >= max(3, (2 * n) // 3)
    if not have_mae:
        rows = [r for r in rows if r["param"] != "stop_loss_pct"]
    best = max(rows, key=lambda r: r["expectancy_usd"]) if rows else None
    gain = (best["expectancy_usd"] - base) if best else 0.0
    return {"n": n, "current": {**cur_params, "expectancy_usd": base},
            "best": best, "gain_usd_per_fill": gain, "rows": rows, "mae_recorded": have_mae, "peak_timing_recorded": have_peak_timing,
            "median_mfe": statistics.median(p["mfe"] for p in paths), "median_mae": statistics.median(p["mae"] for p in paths)}


# ---------------- data-driven entry filters ----------------
def entry_feature_splits(trades: list[dict], book: str, cfg, min_side: int = 5) -> list[dict]:
    """For each entry feature: split fills at the median; if the low side loses and the high side
    earns, the median is a candidate floor. Sorted by the $/fill gain of dropping the low side."""
    feats = ENTRY_FEATURES.get(book) or {}
    out = []
    for feat, key in feats.items():
        rows = []
        for t in trades:
            ctx = t.get("entry_ctx") or {}
            v, p = ctx.get(feat), t.get("pnl_usd")
            if v is None or p is None:
                continue
            try:
                rows.append((float(v), float(p)))
            except (TypeError, ValueError):
                pass
        if len(rows) < 2 * min_side:
            continue
        rows.sort()
        med = statistics.median(v for v, _ in rows)
        low = [p for v, p in rows if v < med]
        high = [p for v, p in rows if v >= med]
        if len(low) < min_side or len(high) < min_side:
            continue
        e_low, e_high, e_all = statistics.mean(low), statistics.mean(high), statistics.mean(p for _, p in rows)
        cur = float(_get(cfg, key) or 0)
        cap = FEATURE_CAPS.get(key)
        ceiling = key in CEILING_KEYS            # "fewer is better": propose LOWERING the key to the median
        if ceiling:
            gain, actionable = e_low - e_all, e_high < 0 < e_low and (cur == 0 or med < cur) and med >= 1
        else:
            gain, actionable = e_high - e_all, e_low < 0 < e_high and med > cur and (cap is None or med <= cap)
        out.append({"feature": feat, "key": key, "n": len(rows), "split": med, "current": cur, "direction": "ceiling" if ceiling else "floor",
                    "low_expectancy_usd": e_low, "high_expectancy_usd": e_high, "all_expectancy_usd": e_all,
                    "gain_usd_per_fill": gain, "actionable": actionable})
    out.sort(key=lambda r: -r["gain_usd_per_fill"])
    return out


def entry_pair_splits(trades: list[dict], book: str, cfg, min_side: int = 5) -> list[dict]:
    """When no single feature separates winners from losers, try pairs: fills above BOTH medians
    ("high-high") vs everything else. Actionable when the rest loses, high-high earns, and both
    gates can be raised to their split."""
    feats = list((ENTRY_FEATURES.get(book) or {}).items())
    out = []
    for i in range(len(feats)):
        for j in range(i + 1, len(feats)):
            (fa, ka), (fb, kb) = feats[i], feats[j]
            rows = []
            for t in trades:
                ctx = t.get("entry_ctx") or {}
                if ctx.get(fa) is None or ctx.get(fb) is None or t.get("pnl_usd") is None:
                    continue
                try:
                    rows.append((float(ctx[fa]), float(ctx[fb]), float(t["pnl_usd"])))
                except (TypeError, ValueError):
                    pass
            if len(rows) < 3 * min_side:
                continue
            ma = statistics.median(r[0] for r in rows)
            mb = statistics.median(r[1] for r in rows)
            hh = [p for a, b, p in rows if a >= ma and b >= mb]
            rest = [p for a, b, p in rows if not (a >= ma and b >= mb)]
            if len(hh) < min_side or len(rest) < min_side:
                continue
            e_hh, e_rest, e_all = statistics.mean(hh), statistics.mean(rest), statistics.mean(p for *_, p in rows)
            cur_a, cur_b = float(_get(cfg, ka) or 0), float(_get(cfg, kb) or 0)
            cap_a, cap_b = FEATURE_CAPS.get(ka), FEATURE_CAPS.get(kb)
            out.append({"features": [fa, fb], "keys": [ka, kb], "n": len(rows), "splits": [ma, mb], "current": [cur_a, cur_b],
                        "high_high_n": len(hh), "high_high_expectancy_usd": e_hh, "rest_expectancy_usd": e_rest,
                        "all_expectancy_usd": e_all, "gain_usd_per_fill": e_hh - e_all,
                        "actionable": e_rest < 0 < e_hh and (ma > cur_a or mb > cur_b)
                        and (cap_a is None or ma <= cap_a) and (cap_b is None or mb <= cap_b)})
    out.sort(key=lambda r: -r["gain_usd_per_fill"])
    return out


RIDE_STEP_PCT, RIDE_MIN_PCT, RIDE_MAX_PCT = 5.0, 0.0, 50.0


def ride_scorecard(trades: list[dict], cfg, min_n: int = 8) -> dict | None:
    """Rode-winner exits vs what the clock would have paid (P/L at the moment the ride began).
    Proposes moving winner_ride_min_pnl_pct: down when riding pays, up when it gives profits back."""
    rows = []
    for t in trades:
        if not t.get("rode_winner") or t.get("ride_started_pnl_pct") is None:
            continue
        try:
            entry_usd, pnl = float(t.get("entry_usd") or 0), float(t.get("pnl_usd"))
            clock = entry_usd * float(t["ride_started_pnl_pct"]) / 100.0
        except (TypeError, ValueError):
            continue
        if entry_usd > 0:
            rows.append(pnl - clock)
    if not rows:
        return None
    cur = float(_get(cfg, "winner_ride_min_pnl_pct", 10.0))
    gain = statistics.mean(rows)
    wins = sum(1 for g in rows if g > 0)
    out = {"n": len(rows), "gain_vs_clock_usd_per_ride": gain, "rides_that_beat_clock": wins, "current_min_pnl_pct": cur, "proposal": None}
    if len(rows) >= min_n:
        if gain > 0 and cur > RIDE_MIN_PCT:
            out["proposal"] = max(RIDE_MIN_PCT, cur - RIDE_STEP_PCT)   # riding pays → let more winners ride
        elif gain < 0 and cur < RIDE_MAX_PCT:
            out["proposal"] = min(RIDE_MAX_PCT, cur + RIDE_STEP_PCT)   # riding gives back → demand a bigger lead first
    return out
