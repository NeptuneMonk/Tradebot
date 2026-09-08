"""Immutable rails — the part of the system that never learns.

The Doctor, the allocator and the autopilot may move config only INSIDE these bounds. They are not
config: they live in code, are shown in the UI, and every clamp is logged."""
from __future__ import annotations

import logging

logger = logging.getLogger("rails")

RAILS: dict[str, tuple[float, float]] = {
    # key: (min, max) — the learner can never set outside this range
    "book_momentum_size_mult": (0.25, 2.0),
    "book_snipe_size_mult": (0.25, 2.0),
    "book_rh_size_mult": (0.25, 2.0),
    "reentry_size_multiplier": (0.25, 2.0),
    "stop_loss_pct": (5.0, 40.0),
    "take_profit_pct": (8.0, 200.0),
    "trailing_stop_pct": (2.0, 25.0),
    "hold_max_seconds": (20, 3600),
    "max_concurrent_positions": (1, 20),
    "rh_max_positions": (1, 10),
    "rh_live_slippage_pct": (1.0, 15.0),
    "flush_hold_s": (0, 30),
    "hot_breakdown_pct": (15.0, 60.0),
}
NEVER_TOUCH = {"daily_kill_switch_usd", "rh_daily_kill_switch_usd", "live_trading", "rh_live_trading", "enabled",
               "max_trade_usd", "rh_max_trade_usd", "rh_gas_reserve_eth"}
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
