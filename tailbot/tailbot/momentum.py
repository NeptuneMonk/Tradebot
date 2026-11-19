"""Buy-flow slowdown on a coin we hold.

Short-window buy rate (SOL / second) versus the preceding baseline window.
Armed only after the position is in profit (see exits.py). Defaults: 30s vs the
prior 150s, exit when the short rate falls under 40% of the baseline and the
baseline itself was at least 1 SOL of buys — a quiet coin with no tape does not
trip the exit.
"""
from __future__ import annotations


def buy_flow_slow(
    events: list[tuple[float, str, float]],
    now: float,
    *,
    short_s: float,
    baseline_s: float,
    drop_ratio: float,
    min_baseline_sol: float,
) -> bool:
    if baseline_s <= short_s or short_s <= 0:
        return False
    if not events:
        return False
    first = min(ts for ts, _side, _q in events)
    if now - first < baseline_s:
        return False
    base_lo = now - baseline_s
    base_hi = now - short_s
    base = sum(q for ts, side, q in events if side == "buy" and base_lo <= ts < base_hi)
    short = sum(q for ts, side, q in events if side == "buy" and ts >= base_hi)
    if base < min_baseline_sol:
        return False
    base_rate = base / (baseline_s - short_s)
    short_rate = short / short_s
    return short_rate < drop_ratio * base_rate
