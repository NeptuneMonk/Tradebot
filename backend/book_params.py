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
    "hold_max_seconds": [20, 30, 45, 60, 90, 120, 180, 300],
}
# entry feature (in trade.entry_ctx) → config key raised to the split point, per book
ENTRY_FEATURES = {
    "rh_pons": {"growth_pct": "rh_min_growth_pct", "inflow_usd": "rh_min_inflow_usd",
                "unique_buyers": "rh_min_unique_buyers", "curve_fill_pct": "rh_min_curve_pct", "mc_usd": "rh_min_mc_usd"},
    "momentum": {"curve_liquidity_sol": "min_curve_liquidity_sol", "unique_buyers": "min_buyers_for_entry"},
    "momentum_new": {"curve_liquidity_sol": "min_curve_liquidity_sol_new", "unique_buyers": "min_buyers_for_entry_new"},
    "greylist_snipe": {"creator_score": "greylist_snipe_min_score"},
}
FEATURE_CAPS = {"rh_min_growth_pct": 150.0, "rh_min_inflow_usd": 3000.0, "rh_min_unique_buyers": 40, "rh_min_curve_pct": 40.0,
                "rh_min_mc_usd": 50000.0, "min_curve_liquidity_sol": 60.0, "min_buyers_for_entry": 30,
                "min_curve_liquidity_sol_new": 80.0, "min_buyers_for_entry_new": 40, "greylist_snipe_min_score": 85.0}


def book_for_action(action: str | None, chain: str | None = None) -> str:
    a = action or ""
    if chain == "rh" or a.startswith("rh_pons"):
        return "rh_pons"
    if a == "reentry":
        return "reentry"
    return "greylist_snipe" if a == "greylist_snipe" else "momentum"


def _get(cfg, key, default=None):
    return cfg.get(key, default) if isinstance(cfg, dict) else getattr(cfg, key, default)


def exit_param(cfg, book: str, param: str) -> float:
    """Book override if set, else the shared global."""
    ov = (_get(cfg, "book_exits") or {}).get(book) or {}
    v = ov.get(param)
    if v is None:
        v = _get(cfg, param)
    if v is None:
        from models import BotConfig
        v = getattr(BotConfig(), param)
    return float(v)


def book_exit_view(cfg, book: str) -> dict:
    return {p: exit_param(cfg, book, p) for p in EXIT_PARAMS}


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


def _simulate(p: dict, tp: float, sl: float, hold: float | None, fee_pct: float) -> float:
    """USD result of this trade under (tp, sl, hold). Unknown paths fall back to what really happened."""
    hit_tp, hit_sl = p["mfe"] >= tp - 1e-6, p["mae"] <= -sl + 1e-6
    if hit_tp and hit_sl:
        first = "tp" if p["peak_first"] else "sl"
    elif hit_tp:
        first = "tp"
    elif hit_sl:
        first = "sl"
    else:
        first = None
    if first == "tp":
        # hold cap can only cut a TP short if the peak came after the cap
        if hold and p.get("peak_hold_s") and p["peak_hold_s"] > hold and p["hold_s"] and p["hold_s"] > hold:
            return p["pnl_usd"]
        return p["entry_usd"] * (tp - fee_pct) / 100.0
    if first == "sl":
        return -p["entry_usd"] * (sl + fee_pct) / 100.0
    return p["pnl_usd"]


def whatif_exits(trades: list[dict], cur: dict, fee_pct: float = 2.0) -> dict:
    """Grid-search TP / SL / hold for one book on its own closed fills. Returns the current-setting
    expectancy, the best single-parameter change and the full grid rows for the UI."""
    paths = [p for p in (_path(t) for t in trades) if p]
    n = len(paths)
    if n == 0:
        return {"n": 0}
    tp0, sl0, hold0 = float(cur["take_profit_pct"]), float(cur["stop_loss_pct"]), float(cur["hold_max_seconds"])

    def exp_for(tp, sl, hold):
        return statistics.mean(_simulate(p, tp, sl, hold, fee_pct) for p in paths)

    base = exp_for(tp0, sl0, hold0)
    rows = []
    for tp in EXIT_GRID["take_profit_pct"]:
        rows.append({"param": "take_profit_pct", "value": tp, "expectancy_usd": exp_for(tp, sl0, hold0)})
    for sl in EXIT_GRID["stop_loss_pct"]:
        rows.append({"param": "stop_loss_pct", "value": sl, "expectancy_usd": exp_for(tp0, sl, hold0)})
    # SL what-ifs need the recorded trough: without it a dip-then-recover winner looks like it never
    # dipped, which makes tight stops look free. Require ≥2/3 of fills to carry one.
    have_mae = sum(1 for p in paths if p["mae_recorded"]) >= max(3, (2 * n) // 3)
    if not have_mae:
        rows = [r for r in rows if r["param"] != "stop_loss_pct"]
    best = max(rows, key=lambda r: r["expectancy_usd"]) if rows else None
    gain = (best["expectancy_usd"] - base) if best else 0.0
    return {"n": n, "current": {"take_profit_pct": tp0, "stop_loss_pct": sl0, "hold_max_seconds": hold0, "expectancy_usd": base},
            "best": best, "gain_usd_per_fill": gain, "rows": rows, "mae_recorded": have_mae,
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
        out.append({"feature": feat, "key": key, "n": len(rows), "split": med, "current": cur,
                    "low_expectancy_usd": e_low, "high_expectancy_usd": e_high, "all_expectancy_usd": e_all,
                    "gain_usd_per_fill": e_high - e_all,
                    "actionable": e_low < 0 < e_high and med > cur and (cap is None or med <= cap)})
    out.sort(key=lambda r: -r["gain_usd_per_fill"])
    return out
