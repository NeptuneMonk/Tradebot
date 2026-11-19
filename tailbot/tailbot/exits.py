"""Position exits.

Copy-sells are applied by the engine (same fraction the leader sold). This module
decides the position's own exits:

* hard stop at -35%
* trailing stop 25% once peak is +50%, and the stop never sits below entry
* momentum exit once the position is more than +20% and buy flow has slowed
* at 24h, ask (do not sell)
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ExitDecision:
    kind: str  # hold | exit | prompt
    reason: str = ""
    fraction: float = 1.0


def pnl_pct(price: float, entry: float) -> float:
    if entry <= 0 or price <= 0:
        return 0.0
    return (price / entry - 1.0) * 100.0


def trail_stop_price(entry: float, peak: float, trail_pct: float) -> float:
    """25% off the peak, floored at the entry price."""
    raw = peak * (1.0 - trail_pct / 100.0)
    return max(entry, raw)


def decide_exit(
    *,
    entry: float,
    last: float,
    peak: float,
    now: float,
    next_prompt_ts: float | None,
    momentum_slow: bool,
    cfg,
) -> ExitDecision:
    if entry <= 0 or last <= 0:
        return ExitDecision("hold")
    pnl = pnl_pct(last, entry)
    if pnl <= -abs(float(cfg.stop_loss_pct)):
        return ExitDecision("exit", "stop")
    peak_pnl = pnl_pct(peak, entry)
    if peak_pnl >= float(cfg.trail_arm_pct):
        stop_px = trail_stop_price(entry, peak, float(cfg.trail_pct))
        if last <= stop_px:
            return ExitDecision("exit", "trail")
    if pnl > float(cfg.momentum_arm_pct) and momentum_slow:
        return ExitDecision("exit", "momentum")
    if next_prompt_ts and now >= next_prompt_ts:
        return ExitDecision("prompt", "24h")
    return ExitDecision("hold")
