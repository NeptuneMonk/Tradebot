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
    pullback_pct = float(w.get("pullback_pct") or _cfg(cfg, "reentry_pullback_pct", 25.0))
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
