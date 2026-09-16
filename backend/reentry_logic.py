"""Shared re-entry trigger logic for the SOL watcher (bot.py) and RH paper (rh_paper.py).

A watch is created by a WINNING exit. It fires on one of two paths:
  pullback  — the token kept running after we sold (peak ≥ exit × (1+min_bounce)),
              pulled back ≥ pullback_pct from that peak, and is now bouncing
              (price ≥ trough × (1+bounce_confirm)) with buyers still arriving.
  breakout  — price broke ≥ breakout_pct above our exit with real buy pressure,
              and the previous leg did NOT exit via stop-loss.
Both paths respect a minimum wait after the last exit on that mint.
"""
from __future__ import annotations


def _cfg(cfg, key, default):
    v = cfg.get(key, default) if isinstance(cfg, dict) else getattr(cfg, key, default)
    return default if v is None else v


def update_watch_price(w: dict, price: float) -> None:
    """Track post-exit peak and the trough since that peak."""
    if price <= 0:
        return
    if price > (w.get("peak_price_after_exit") or 0):
        w["peak_price_after_exit"] = price
        w["trough_after_peak"] = price
    else:
        t = w.get("trough_after_peak")
        w["trough_after_peak"] = price if (t is None or price < t) else t


def decide_reentry(w: dict, price: float, now: float, buyers_recent: int,
                   inflow_ok: bool, cfg) -> str | None:
    """Return 'pullback' | 'breakout' | None. Mutates peak/trough on `w`."""
    if price <= 0:
        return None
    update_watch_price(w, price)
    min_wait = float(_cfg(cfg, "reentry_min_wait_s", 20))
    if now - float(w.get("last_exit_time") or w.get("exit_time") or 0) < min_wait:
        return None
    exit_price = float(w.get("exit_price_quote") or w.get("exit_price_sol") or 0)
    if exit_price <= 0:
        return None
    peak = float(w.get("peak_price_after_exit") or 0)
    trough = float(w.get("trough_after_peak") or price)
    min_bounce = float(_cfg(cfg, "reentry_min_bounce_pct", 5.0))
    bounce_confirm = float(_cfg(cfg, "reentry_bounce_confirm_pct", 3.0))
    min_buyers = int(_cfg(cfg, "reentry_min_buyers", 2))
    pullback_pct = float(_cfg(cfg, "reentry_pullback_pct", None) or w.get("pullback_pct") or 25.0)   # live setting wins over the watch snapshot
    ran_on = peak >= exit_price * (1.0 + min_bounce / 100.0)
    pulled_back = peak > 0 and (peak - trough) / peak * 100.0 >= pullback_pct
    bouncing = price >= trough * (1.0 + bounce_confirm / 100.0)
    if ran_on and pulled_back and bouncing and buyers_recent >= min_buyers:
        return "pullback"
    breakout_pct = float(_cfg(cfg, "reentry_breakout_pct", 5.0))
    strong_buyers = int(_cfg(cfg, "exit_momentum_min_buyers", 3))
    if (not w.get("last_exit_was_sl")
            and price > exit_price * (1.0 + breakout_pct / 100.0)
            and buyers_recent >= max(1, strong_buyers)
            and inflow_ok):
        return "breakout"
    return None


def recent_buyers_and_inflow(buy_events, now: float, window_s: float) -> tuple[int, float]:
    """buy_events rows: (ts, amount, wallet). Returns (distinct wallets, summed amount)."""
    cutoff = now - window_s
    wallets: set = set()
    amt = 0.0
    for ev in buy_events or ():
        try:
            ts, q, wl = ev[0], ev[1], ev[2]
        except (IndexError, TypeError):
            continue
        if ts >= cutoff:
            wallets.add(wl)
            amt += float(q or 0)
    return len(wallets), amt


def update_swings(w: dict, price: float, reversal_pct: float) -> None:
    """Zig-zag swing tracker on the watch: a move of reversal_pct against the current leg
    completes a swing high/low. Feeds the hot-token lower-lows walk-away rule."""
    if price <= 0:
        return
    z = w.get("swings")
    if z is None:
        w["swings"] = {"dir": "up", "ext": price, "lows": [], "highs": []}
        return
    r = reversal_pct / 100.0
    if z["dir"] == "up":
        if price > z["ext"]:
            z["ext"] = price
        elif price <= z["ext"] * (1.0 - r):
            z["highs"].append(z["ext"])
            z["dir"], z["ext"] = "down", price
    else:
        if price < z["ext"]:
            z["ext"] = price
        elif price >= z["ext"] * (1.0 + r):
            z["lows"].append(z["ext"])
            z["dir"], z["ext"] = "up", price
    del z["lows"][:-6], z["highs"][:-6]


def _tail_declines(xs: list) -> int:
    n = 0
    for i in range(len(xs) - 1, 0, -1):
        if xs[i] < xs[i - 1]:
            n += 1
        else:
            break
    return n


def hot_walk_away_reason(w: dict, b: dict, price: float, now: float, cfg) -> str | None:
    """Why a HOT watch should be dropped: the token stopped trending (user rule — no attempt cap,
    just walk away on lower lows / weak bounce / stagnation / breakdown)."""
    if price <= 0:
        return None
    n_lows = int(_cfg(cfg, "hot_walk_lower_lows_n", 2))
    if int(w.get("strikes") or 0) >= n_lows:
        return "losing_legs"
    z = w.get("swings") or {}
    if _tail_declines(z.get("lows") or []) >= n_lows and _tail_declines(z.get("highs") or []) >= 1:
        return "lower_lows"
    peak = float(w.get("peak_price_after_exit") or 0)
    if peak > 0 and price < peak * (1.0 - float(_cfg(cfg, "hot_breakdown_pct", 40.0)) / 100.0):
        return "broke_down"
    trough = float(w.get("trough_after_peak") or price)
    pullback_pct = float(_cfg(cfg, "reentry_pullback_pct", None) or w.get("pullback_pct") or 25.0)   # live setting wins over the watch snapshot
    dipped = peak > 0 and (peak - trough) / peak * 100.0 >= pullback_pct / 2.0
    weak_s = float(_cfg(cfg, "hot_weak_bounce_s", 120))
    if (dipped and now - float(w.get("trough_ts") or now) >= weak_s
            and price < trough * (1.0 + float(_cfg(cfg, "hot_weak_bounce_pct", 3.0)) / 100.0)):
        return "weak_bounce"
    stag_s = float(_cfg(cfg, "hot_stagnant_s", 180))
    age = now - float(w.get("exit_time") or now)
    if age < stag_s:
        return None
    if recent_buyers_and_inflow(b.get("buy_events"), now, stag_s)[0] == 0:
        return "no_buyers"
    recent = [p for ts, p in (b.get("price_samples") or ()) if ts >= now - stag_s and p > 0]
    if len(recent) >= 3:
        hi, lo = max(recent), min(recent)
        if hi > 0 and (hi - lo) / hi * 100.0 < float(_cfg(cfg, "hot_stagnant_range_pct", 4.0)):
            return "stagnant"
    return None


def trigger_context(w: dict, price: float, buyers_recent: int, trigger: str) -> dict:
    """Audit record stored on the re-entry trade: why it fired."""
    exit_price = float(w.get("exit_price_quote") or w.get("exit_price_sol") or 0)
    peak = float(w.get("peak_price_after_exit") or 0)
    trough = float(w.get("trough_after_peak") or price)
    return {
        "trigger": trigger,
        "exit_price": exit_price,
        "peak": peak,
        "trough": trough,
        "entry_price": price,
        "run_on_pct": round((peak / exit_price - 1.0) * 100.0, 2) if exit_price else None,
        "pullback_pct": round((peak - trough) / peak * 100.0, 2) if peak else None,
        "bounce_pct": round((price / trough - 1.0) * 100.0, 2) if trough else None,
        "vs_exit_pct": round((price / exit_price - 1.0) * 100.0, 2) if exit_price else None,
        "buyers": int(buyers_recent),
        "attempt": int(w.get("attempts") or 0) + 1,
    }
