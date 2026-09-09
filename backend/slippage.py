"""Slippage helpers shared by the entry cost gate and the exit engines (single copy — bot.py imports these)."""
from __future__ import annotations

import time

from solana_client import LAMPORTS_PER_SOL

# (depth_sol_below, floor_bps) — thinner pools move faster per SOL of order; first match wins.
ENTRY_SLIP_FLOORS = {
    "pumpfun": ((32, 2500), (40, 2000), (55, 1500), (float("inf"), 1000)),
    "pumpswap": ((5, 2500), (15, 1800), (40, 1200), (float("inf"), 800)),
}


def pool_depth_sol(state: dict | None, protocol: str) -> float:
    if not state:
        return 0.0
    key = "quote_reserves" if protocol == "pumpswap" else "virtual_sol_reserves"
    return (state.get(key) or 0) / LAMPORTS_PER_SOL


def entry_slip_bps(protocol: str, depth_sol: float, base_bps: int) -> int:
    for below, floor in ENTRY_SLIP_FLOORS.get(protocol, ENTRY_SLIP_FLOORS["pumpfun"]):
        if depth_sol < below:
            return max(int(base_bps), floor)
    return int(base_bps)


def auto_exit_slip_bps(cfg, *, panic: bool, pool_depth_sol: float, recent_vol_pct: float | None) -> int:
    """Exchange-style exit slippage: base + thin-pool / volatility / panic adders, hard-capped."""
    bps = int(cfg.auto_exit_slip_base_bps)
    if 0 < pool_depth_sol < cfg.auto_exit_slip_thin_pool_sol:
        bps += int(cfg.auto_exit_slip_thin_pool_extra_bps)
    if recent_vol_pct is not None and recent_vol_pct >= cfg.auto_exit_slip_vol_threshold_pct:
        bps += int(cfg.auto_exit_slip_high_vol_extra_bps)
    if panic:
        bps += int(cfg.auto_exit_slip_panic_extra_bps)
    return min(bps, int(cfg.auto_exit_slip_cap_bps))


def recent_vol_pct(samples: list[tuple[float, float]], window_s: int) -> float | None:
    if not samples or len(samples) < 4:
        return None
    now = time.time()
    recent = [p for (ts, p) in samples if now - ts <= window_s]
    if len(recent) < 4:
        return None
    mean = sum(recent) / len(recent)
    if mean <= 0:
        return None
    return ((sum((p - mean) ** 2 for p in recent) / len(recent)) ** 0.5 / mean) * 100.0
