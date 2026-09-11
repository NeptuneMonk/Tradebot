"""Immutable rails — the part of the system that never learns.

The Doctor, the allocator and the autopilot may move config only INSIDE these bounds. Book-scoped exit keys
(`book_exits.<book>.<param>`) are clamped by the param's rail."""
from __future__ import annotations

import logging

logger = logging.getLogger("rails")

RAILS: dict[str, tuple[float, float]] = {
    "book_scalp_size_mult": (0.25, 2.0),
    "book_hunt_size_mult": (0.25, 2.0),
    "book_runner_size_mult": (0.25, 2.0),
    "book_rh_size_mult": (0.25, 2.0),
    "stop_loss_pct": (5.0, 40.0),
    "target_r": (1.0, 5.0),
    "take_profit_pct": (8.0, 200.0),
    "trailing_stop_pct": (2.0, 25.0),
    "trailing_arm_pct": (0.0, 50.0),
    "hold_max_seconds": (0, 3600),
    "ladder_1r_sell_pct": (0.0, 60.0),
    "ladder_2r_sell_pct": (0.0, 60.0),
    "max_concurrent_positions": (1, 8),
    "rh_max_positions": (1, 10),
    "rh_live_slippage_pct": (1.0, 15.0),
    "flush_hold_s": (0, 30),
    "hot_breakdown_pct": (15.0, 60.0),
    "risk_per_trade_pct": (0.25, 5.0),
}
NEVER_TOUCH = {"daily_kill_switch_usd", "rh_daily_kill_switch_usd", "live_trading", "rh_live_trading", "enabled",
               "max_trade_usd", "rh_max_trade_usd", "rh_gas_reserve_eth",
               "helius_tracker_enabled", "rh_feed_enabled", "rh_paper_enabled", "scanner_enabled"}
MAX_CHANGES_PER_DAY = 6


def clamp_actions(actions: dict, who: str = "doctor") -> tuple[dict, list[str]]:
    """Drop NEVER_TOUCH keys, clamp ranged keys (nested `book_exits.<book>.<param>` use the param's rail)."""
    out, notes = {}, []
    for k, v in actions.items():
        leaf = k.split(".")[-1]
        if k in NEVER_TOUCH or leaf in NEVER_TOUCH:
            notes.append(f"{who} may not change {k} — dropped")
            continue
        rail = RAILS.get(k) or RAILS.get(leaf)
        if rail and isinstance(v, (int, float)) and not isinstance(v, bool):
            lo, hi = rail
            cv = min(hi, max(lo, v))
            if cv != v:
                notes.append(f"{k} {v} clamped to {cv} (rail {lo}–{hi})")
                v = type(v)(cv) if isinstance(v, int) and float(cv).is_integer() else cv
        out[k] = v
    for n in notes:
        logger.warning(f"rails: {n}")
    return out, notes


def describe() -> dict:
    return {"ranges": {k: {"min": lo, "max": hi} for k, (lo, hi) in RAILS.items()},
            "never_touch": sorted(NEVER_TOUCH), "max_changes_per_day": MAX_CHANGES_PER_DAY}
