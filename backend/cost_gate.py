"""Pre-trade cost gate — the single source of truth for "may we enter?".

Expected round-trip friction (entry slip + exit slip + protocol fee both ways + priority fees + token
shave) must be cleared TWICE by the first cash-out of the book (scalp: target_r × R, hunt: +1R)."""
from __future__ import annotations

COST_MULT = 2.0                 # first cash-out must be ≥ 2× expected round-trip friction
MAX_ROUND_TRIP_PCT = 8.0        # sit out mints whose friction alone eats > 8%
ADVERSE_FILL_PCT = 1.0          # per side: expected adverse move between quote and fill (beyond pure impact)
PROTOCOL_FEE_PCT = {"pumpfun": 1.0, "pumpswap": 0.25, "rh": 1.0}   # per side
TOKEN_SHAVE_PCT = 0.5           # dust left behind on a full sell
LADDER_SHAVE_PCT = 5.0          # partial sells shave more (rounding across 3 legs)


def expected_slip_pct(size_usd: float, depth_usd: float, tolerance_bps: int) -> float:
    """Expected realised slip for one side: price impact of our order on the pool + adverse fill, never
    above the tolerance we quote with (the tolerance is a survival cap, not the expectation)."""
    impact = (size_usd / depth_usd * 100.0) if depth_usd > 0 else tolerance_bps / 100.0
    return min(tolerance_bps / 100.0, impact + ADVERSE_FILL_PCT)


def quote(*, size_usd: float, r_usd: float, first_target_r: float, protocol: str,
          entry_slip_bps: int, exit_slip_bps: int, fee_usd_round_trip: float, ladder: bool, depth_usd: float = 0.0) -> dict:
    size_usd = max(float(size_usd), 1e-9)
    slip_pct = expected_slip_pct(size_usd, depth_usd, entry_slip_bps) + expected_slip_pct(size_usd, depth_usd, exit_slip_bps)
    fee_pct = fee_usd_round_trip / size_usd * 100.0
    proto_pct = 2 * PROTOCOL_FEE_PCT.get(protocol, 1.0)
    shave_pct = LADDER_SHAVE_PCT if ladder else TOKEN_SHAVE_PCT
    cost_pct = slip_pct + fee_pct + proto_pct + shave_pct
    target_pct = first_target_r * r_usd / size_usd * 100.0
    ok = cost_pct <= MAX_ROUND_TRIP_PCT and target_pct >= COST_MULT * cost_pct
    if cost_pct > MAX_ROUND_TRIP_PCT:
        reason = f"round-trip cost {cost_pct:.1f}% > {MAX_ROUND_TRIP_PCT:g}% ceiling"
    elif not ok:
        reason = f"first target {target_pct:.1f}% < {COST_MULT:g}× cost {cost_pct:.1f}%"
    else:
        reason = "pass"
    return {"expected_cost_pct": round(cost_pct, 3), "expected_cost_usd": round(cost_pct / 100.0 * size_usd, 4),
            "expected_target_pct": round(target_pct, 3), "cost_gate_pass": ok, "cost_gate_reason": reason,
            "cost_breakdown": {"slip_pct": round(slip_pct, 3), "fee_pct": round(fee_pct, 3),
                               "protocol_pct": proto_pct, "shave_pct": shave_pct}}
