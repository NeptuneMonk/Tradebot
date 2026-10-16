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


def slip_cap_bps(cfg) -> int:
    """Operator ceiling (config.max_slippage_bps); 0 / missing = no cap."""
    try:
        cap = int(getattr(cfg, "max_slippage_bps", 0) or 0)
    except (TypeError, ValueError):
        cap = 0
    return cap if cap > 0 else 10**9


def capped(bps: int, cfg) -> int:
    return max(1, min(int(bps), slip_cap_bps(cfg)))


def entry_slip_bps(protocol: str, depth_sol: float, base_bps: int, cfg=None) -> int:
    """Thin-pool floors widen the operator's entry slip — but never above max_slippage_bps when `cfg` is given."""
    out = int(base_bps)
    for below, floor in ENTRY_SLIP_FLOORS.get(protocol, ENTRY_SLIP_FLOORS["pumpfun"]):
        if depth_sol < below:
            out = max(int(base_bps), floor)
            break
    return capped(out, cfg) if cfg is not None else out


def auto_exit_slip_bps(cfg, *, panic: bool, pool_depth_sol: float, recent_vol_pct: float | None) -> int:
    """Exchange-style exit slippage: base + thin-pool / volatility / panic adders, hard-capped."""
    bps = int(cfg.auto_exit_slip_base_bps)
    if 0 < pool_depth_sol < cfg.auto_exit_slip_thin_pool_sol:
        bps += int(cfg.auto_exit_slip_thin_pool_extra_bps)
    if recent_vol_pct is not None and recent_vol_pct >= cfg.auto_exit_slip_vol_threshold_pct:
        bps += int(cfg.auto_exit_slip_high_vol_extra_bps)
    if panic:
        bps += int(cfg.auto_exit_slip_panic_extra_bps)
    return capped(min(bps, int(cfg.auto_exit_slip_cap_bps)), cfg)


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
