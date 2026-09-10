"""Books, routing and per-book exit parameters + the Doctor's counterfactual / entry-feature analysis (in R).

Two incompatible Solana books plus the RH curve book:
  scalp    momentum (new + seasoned band) and manual buys — short hold, clock stop allowed, single exit
  hunt     greylist_snipe + reentry — pattern rip-cord / R ladder / trail, NO clock
  rh_pons  Robinhood Chain curves — its own exits, never shares Solana values
Every exit parameter lives ONLY under cfg.book_exits.<book>.<param>; there is no global fallback."""
from __future__ import annotations

import statistics

BOOKS = ("scalp", "hunt", "rh_pons")           # entry books (Doctor tunes these)
ALL_BOOKS = BOOKS + ("runner",)                # + runner: promotion-only, never opened cold, never Doctor-tuned
SIZE_KEYS = {"scalp": "book_scalp_size_mult", "hunt": "book_hunt_size_mult", "rh_pons": "book_rh_size_mult",
             "runner": "book_runner_size_mult"}
HUNT_ACTIONS = {"greylist_snipe", "reentry"}
EXIT_PARAMS = ("stop_loss_pct", "target_r", "trailing_stop_pct", "trailing_arm_pct", "hold_max_seconds",
               "ladder_1r_sell_pct", "ladder_2r_sell_pct", "take_profit_pct")
RUNNER_PARAMS = ("add_on_r", "giveback_pct", "dead_s", "grad_grace_s")
BOOK_DEFAULTS = {
    "runner": {"stop_loss_pct": 25.0, "target_r": 0.0, "trailing_stop_pct": 15.0, "trailing_arm_pct": 0.0,
               "hold_max_seconds": 0, "ladder_1r_sell_pct": 0.0, "ladder_2r_sell_pct": 0.0, "take_profit_pct": 0.0,
               "add_on_r": 0.5, "giveback_pct": 25.0, "dead_s": 90, "grad_grace_s": 45},
    "scalp": {"stop_loss_pct": 12.0, "target_r": 1.5, "trailing_stop_pct": 6.0, "trailing_arm_pct": 12.0,
              "hold_max_seconds": 40, "ladder_1r_sell_pct": 0.0, "ladder_2r_sell_pct": 0.0, "take_profit_pct": 0.0},
    "hunt": {"stop_loss_pct": 20.0, "target_r": 2.0, "trailing_stop_pct": 8.0, "trailing_arm_pct": 0.0,
             "hold_max_seconds": 0, "ladder_1r_sell_pct": 35.0, "ladder_2r_sell_pct": 30.0, "take_profit_pct": 0.0},
    "rh_pons": {"stop_loss_pct": 12.0, "target_r": 0.0, "trailing_stop_pct": 6.0, "trailing_arm_pct": 12.0,
                "hold_max_seconds": 35, "ladder_1r_sell_pct": 0.0, "ladder_2r_sell_pct": 0.0, "take_profit_pct": 20.0},
}
FIRST_TARGET_R = {"scalp": 1.5, "hunt": 1.0, "rh_pons": 1.0, "runner": 1.0}   # first cash-out in R (cost gate + breaker)
EXIT_GRID = {
    "target_r": [1.0, 1.25, 1.5, 2.0, 2.5, 3.0],
    "stop_loss_pct": [8, 10, 12, 15, 20, 25, 30],
    "trailing_stop_pct": [3, 4, 5, 6, 8, 10, 12, 15],
    "trailing_arm_pct": [0, 5, 8, 10, 12, 15, 20],
    "hold_max_seconds": [20, 30, 40, 60, 90, 120],
}
ENTRY_FEATURES = {
    "rh_pons": {"growth_pct": "rh_min_growth_pct", "inflow_usd": "rh_min_inflow_usd",
                "unique_buyers": "rh_min_unique_buyers", "curve_fill_pct": "rh_min_curve_pct", "mc_usd": "rh_min_mc_usd"},
    "scalp": {"curve_liquidity_sol": "min_curve_liquidity_sol", "unique_buyers": "min_buyers_for_entry",
              "creator_prior_launches": "serial_creator_min_launches"},
    "hunt": {"creator_score": "greylist_snipe_min_score"},
}
CEILING_KEYS = {"serial_creator_min_launches", "rh_max_growth_pct"}
FEATURE_CAPS = {"rh_min_growth_pct": 150.0, "rh_min_inflow_usd": 3000.0, "rh_min_unique_buyers": 40, "rh_min_curve_pct": 40.0,
                "rh_min_mc_usd": 50000.0, "min_curve_liquidity_sol": 60.0, "min_buyers_for_entry": 30,
                "greylist_snipe_min_score": 85.0, "serial_creator_min_launches": 50}

REGIMES = ("quiet", "busy")
REGIME_MULT_STEP, REGIME_MULT_CAP = 0.5, 3.0


def _get(cfg, key, default=None):
    return cfg.get(key, default) if isinstance(cfg, dict) else getattr(cfg, key, default)


def book_for_action(action: str | None, chain: str | None = None) -> str:
    a = action or ""
    if chain == "rh" or a.startswith("rh_pons"):
        return "rh_pons"
    return "hunt" if a in HUNT_ACTIONS else "scalp"


def book_size_mult(cfg, book: str) -> float:
    v = _get(cfg, SIZE_KEYS.get(book, "book_scalp_size_mult"), 1.0)
    try:
        return max(0.0, float(v if v is not None else 1.0))
    except (TypeError, ValueError):
        return 1.0


def exit_param(cfg, book: str, param: str, regime: str | None = None) -> float:
    """Regime override (book_exits.<book>.<regime>.<param>) → book override → book default. Never global."""
    ov = (_get(cfg, "book_exits") or {}).get(book) or {}
    v = None
    if regime and isinstance(ov.get(regime), dict):
        v = ov[regime].get(param)
    if v is None:
        v = ov.get(param)
    if v is None:
        v = BOOK_DEFAULTS[book][param]
    return float(v)


def book_exit_view(cfg, book: str, regime: str | None = None) -> dict:
    return {p: exit_param(cfg, book, p, regime) for p in EXIT_PARAMS + (RUNNER_PARAMS if book == "runner" else ())}


def r_of(t: dict) -> float | None:
    """1R for a trade in USD: persisted r_usd, else entry × its book's SL (pre-migration rows)."""
    try:
        r = float(t.get("r_usd") or 0)
        if r > 0:
            return r
        e = float(t.get("entry_usd") or 0)
        book = t.get("book") if t.get("book") in ALL_BOOKS else book_for_action(t.get("classifier_action"), t.get("chain"))
        return e * BOOK_DEFAULTS[book]["stop_loss_pct"] / 100.0 if e > 0 else None
    except (TypeError, ValueError):
        return None


def pnl_r(t: dict) -> float | None:
    r = r_of(t)
    try:
        return float(t["pnl_usd"]) / r if r else None
    except (KeyError, TypeError, ValueError):
        return None


def regime_for(cfg, book: str, launch_rate_per_h: float) -> str:
    thr = float(((_get(cfg, "regime_busy_threshold") or {}).get(book)) or _get(cfg, "regime_busy_default_per_h", 30.0) or 30.0)
    return "busy" if launch_rate_per_h >= thr else "quiet"


def regime_gate_mult(cfg, book: str, launch_rate_per_h: float) -> float:
    reg = regime_for(cfg, book, launch_rate_per_h)
    m = ((_get(cfg, "regime_gate_mult") or {}).get(book) or {}).get(reg)
    return float(m) if m is not None else 1.0


def trade_regime(cfg, book: str, trade: dict) -> str | None:
    rate = (trade.get("entry_ctx") or {}).get("launch_rate_per_h")
    return regime_for(cfg, book, float(rate)) if rate is not None else None


def regime_splits(trades: list[dict], book: str, cfg, min_side: int = 5) -> dict | None:
    rows = [(float((t.get("entry_ctx") or {}).get("launch_rate_per_h")), pnl_r(t)) for t in trades
            if (t.get("entry_ctx") or {}).get("launch_rate_per_h") is not None and pnl_r(t) is not None]
    if len(rows) < 2 * min_side:
        return None
    med = statistics.median(r for r, _ in rows)
    quiet, busy = [p for r, p in rows if r < med], [p for r, p in rows if r >= med]
    if len(quiet) < min_side or len(busy) < min_side:
        return None
    e_q, e_b = statistics.mean(quiet), statistics.mean(busy)
    losing = "quiet" if e_q < e_b else "busy"
    cur_mult = ((_get(cfg, "regime_gate_mult") or {}).get(book) or {}).get(losing, 1.0)
    return {"n": len(rows), "threshold_per_h": med, "quiet": {"n": len(quiet), "expectancy_r": e_q},
            "busy": {"n": len(busy), "expectancy_r": e_b}, "losing": losing, "current_mult": float(cur_mult),
            "actionable": min(e_q, e_b) < 0 < max(e_q, e_b) and float(cur_mult) < REGIME_MULT_CAP,
            "gain_r_per_fill": max(e_q, e_b) - statistics.mean(p for _, p in rows)}


# ---------------- counterfactual exit optimizer (results in R) ----------------
def _path(t: dict) -> dict | None:
    ep = t.get("entry_price_quote") or t.get("entry_price_sol") or 0
    r = r_of(t)
    if not ep or not r:
        return None
    pk = t.get("peak_price_quote") or t.get("peak_price_sol") or ep
    tr_rec = t.get("trough_price_quote") or t.get("trough_price_sol")
    tr = tr_rec or min(ep, t.get("exit_price_quote") or t.get("exit_price_sol") or ep)
    try:
        pnl_pct, entry_usd, pnl_usd = float(t.get("pnl_pct")), float(t.get("entry_usd") or 0), float(t.get("pnl_usd"))
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
    return {"mfe": (pk / ep - 1) * 100, "mae": (tr / ep - 1) * 100, "pnl_pct": pnl_pct, "pnl_usd": pnl_usd, "r_usd": r,
            "entry_usd": entry_usd, "peak_first": (t.get("peak_ts") or 0) <= (t.get("trough_ts") or 0),
            "hold_s": hold, "peak_hold_s": t.get("peak_hold_s"), "mae_recorded": bool(tr_rec)}


def _simulate(p: dict, params: dict, fee_pct: float) -> float:
    """R result of this trade under the exit ladder `params` (single exit at +target_r / −1R)."""
    sl = float(params["stop_loss_pct"])
    tp = float(params["target_r"]) * sl if float(params.get("target_r") or 0) > 0 else float(params.get("take_profit_pct") or 0)
    trail, arm = float(params["trailing_stop_pct"]), float(params["trailing_arm_pct"])
    hold = float(params.get("hold_max_seconds") or 0) or None
    hit_tp, hit_sl = tp > 0 and p["mfe"] >= tp - 1e-6, p["mae"] <= -sl + 1e-6
    first = ("tp" if p["peak_first"] else "sl") if (hit_tp and hit_sl) else "tp" if hit_tp else "sl" if hit_sl else None
    usd = None
    if hold and p.get("hold_s") and p["hold_s"] > hold and p.get("peak_hold_s") is not None and first != "sl" \
            and not (first == "tp" and p["peak_hold_s"] <= hold):
        if p["peak_hold_s"] >= hold:
            usd = -p["entry_usd"] * fee_pct / 100.0
        else:
            frac = (hold - p["peak_hold_s"]) / max(1e-9, p["hold_s"] - p["peak_hold_s"])
            usd = p["entry_usd"] * (p["mfe"] + (p["pnl_pct"] - p["mfe"]) * frac - fee_pct) / 100.0
    if usd is None and first == "sl":
        usd = -p["entry_usd"] * (sl + fee_pct) / 100.0
    if usd is None and first == "tp":
        usd = p["entry_usd"] * (tp - fee_pct) / 100.0
    if usd is None and p["mfe"] >= arm - 1e-6 and p["mfe"] > 0 and trail > 0:
        drop = (1.0 - (1.0 + p["pnl_pct"] / 100.0) / (1.0 + p["mfe"] / 100.0)) * 100.0
        if drop >= trail - 1e-6:
            usd = p["entry_usd"] * (((1.0 + p["mfe"] / 100.0) * (1.0 - trail / 100.0) - 1.0) * 100.0 - fee_pct) / 100.0
    if usd is None:
        usd = p["pnl_usd"]
    return usd / p["r_usd"]


def whatif_exits(trades: list[dict], cur: dict, fee_pct: float = 2.0) -> dict:
    """Grid-search one book's exit keys on its own closed fills. Everything in R per fill."""
    paths = [p for p in (_path(t) for t in trades) if p]
    n = len(paths)
    if n == 0:
        return {"n": 0}
    cur_params = {k: float(cur.get(k) if cur.get(k) is not None else 0.0) for k in EXIT_PARAMS}

    def exp_for(**over):
        return statistics.mean(_simulate(p, {**cur_params, **over}, fee_pct) for p in paths)

    base = exp_for()
    rows = [{"param": param, "value": v, "expectancy_r": exp_for(**{param: v})} for param in EXIT_GRID for v in EXIT_GRID[param]]
    have_peak_timing = sum(1 for p in paths if p.get("peak_hold_s") is not None and p.get("hold_s")) >= max(3, (2 * n) // 3)
    if not have_peak_timing:
        rows = [r for r in rows if r["param"] != "hold_max_seconds"]
    have_mae = sum(1 for p in paths if p["mae_recorded"]) >= max(3, (2 * n) // 3)
    if not have_mae:
        rows = [r for r in rows if r["param"] != "stop_loss_pct"]
    best = max(rows, key=lambda r: r["expectancy_r"]) if rows else None
    return {"n": n, "current": {**cur_params, "expectancy_r": base}, "best": best,
            "gain_r_per_fill": (best["expectancy_r"] - base) if best else 0.0, "rows": rows,
            "mae_recorded": have_mae, "peak_timing_recorded": have_peak_timing,
            "median_mfe": statistics.median(p["mfe"] for p in paths), "median_mae": statistics.median(p["mae"] for p in paths)}


# ---------------- data-driven entry filters (in R) ----------------
def _feature_rows(trades, feat):
    rows = []
    for t in trades:
        v, p = (t.get("entry_ctx") or {}).get(feat), pnl_r(t)
        if v is None or p is None:
            continue
        try:
            rows.append((float(v), p))
        except (TypeError, ValueError):
            pass
    return rows


def entry_feature_splits(trades: list[dict], book: str, cfg, min_side: int = 5) -> list[dict]:
    out = []
    for feat, key in (ENTRY_FEATURES.get(book) or {}).items():
        rows = sorted(_feature_rows(trades, feat))
        if len(rows) < 2 * min_side:
            continue
        med = statistics.median(v for v, _ in rows)
        low, high = [p for v, p in rows if v < med], [p for v, p in rows if v >= med]
        if len(low) < min_side or len(high) < min_side:
            continue
        e_low, e_high, e_all = statistics.mean(low), statistics.mean(high), statistics.mean(p for _, p in rows)
        cur, cap, ceiling = float(_get(cfg, key) or 0), FEATURE_CAPS.get(key), key in CEILING_KEYS
        if ceiling:
            gain, actionable = e_low - e_all, e_high < 0 < e_low and (cur == 0 or med < cur) and med >= 1
        else:
            gain, actionable = e_high - e_all, e_low < 0 < e_high and med > cur and (cap is None or med <= cap)
        out.append({"feature": feat, "key": key, "n": len(rows), "split": med, "current": cur, "direction": "ceiling" if ceiling else "floor",
                    "low_expectancy_r": e_low, "high_expectancy_r": e_high, "all_expectancy_r": e_all,
                    "gain_r_per_fill": gain, "actionable": actionable})
    out.sort(key=lambda r: -r["gain_r_per_fill"])
    return out


def entry_pair_splits(trades: list[dict], book: str, cfg, min_side: int = 5) -> list[dict]:
    feats = list((ENTRY_FEATURES.get(book) or {}).items())
    out = []
    for i in range(len(feats)):
        for j in range(i + 1, len(feats)):
            (fa, ka), (fb, kb) = feats[i], feats[j]
            rows = []
            for t in trades:
                ctx, p = t.get("entry_ctx") or {}, pnl_r(t)
                if ctx.get(fa) is None or ctx.get(fb) is None or p is None:
                    continue
                try:
                    rows.append((float(ctx[fa]), float(ctx[fb]), p))
                except (TypeError, ValueError):
                    pass
            if len(rows) < 3 * min_side:
                continue
            ma, mb = statistics.median(r[0] for r in rows), statistics.median(r[1] for r in rows)
            hh = [p for a, b, p in rows if a >= ma and b >= mb]
            rest = [p for a, b, p in rows if not (a >= ma and b >= mb)]
            if len(hh) < min_side or len(rest) < min_side:
                continue
            e_hh, e_rest, e_all = statistics.mean(hh), statistics.mean(rest), statistics.mean(p for *_, p in rows)
            cur_a, cur_b = float(_get(cfg, ka) or 0), float(_get(cfg, kb) or 0)
            cap_a, cap_b = FEATURE_CAPS.get(ka), FEATURE_CAPS.get(kb)
            out.append({"features": [fa, fb], "keys": [ka, kb], "n": len(rows), "splits": [ma, mb], "current": [cur_a, cur_b],
                        "high_high_n": len(hh), "high_high_expectancy_r": e_hh, "rest_expectancy_r": e_rest,
                        "all_expectancy_r": e_all, "gain_r_per_fill": e_hh - e_all,
                        "actionable": e_rest < 0 < e_hh and (ma > cur_a or mb > cur_b)
                        and (cap_a is None or ma <= cap_a) and (cap_b is None or mb <= cap_b)})
    out.sort(key=lambda r: -r["gain_r_per_fill"])
    return out
