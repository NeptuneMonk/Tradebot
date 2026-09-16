"""Pre-trade cost gate — the single source of truth for "may we enter?".

Expected round-trip friction (entry slip + exit slip + protocol fee both ways + priority fees + token
shave) must be cleared TWICE by the first cash-out of the book (scalp: target_r × R, hunt: +1R)."""
from __future__ import annotations

COST_MULT = 2.0                 # first cash-out must be ≥ 2× expected round-trip friction
MAX_ROUND_TRIP_PCT = 8.0        # sit out mints whose friction alone eats > 8%
MAX_ROUND_TRIP_PCT_MEASURED = 12.0   # a quoter-measured pool trip is a known toll, not uncertainty — wider ceiling, same 2× rule
MAX_ROUND_TRIP_PCT_RH = 12.0    # RH curve: 1 % + 1 % protocol take, fixed gas and a FIFO sequencer — the toll is deterministic, not MEV risk
ADVERSE_FILL_PCT = 1.0          # per side: expected adverse move between quote and fill (beyond pure impact)
PROTOCOL_FEE_PCT = {"pumpfun": 1.0, "pumpswap": 0.25, "rh": 1.0}   # per side
TOKEN_SHAVE_PCT = 0.5           # dust left behind on a full sell
LADDER_SHAVE_PCT = 1.5          # dust across the 3 ladder legs (0.5% each)


def expected_slip_pct(size_usd: float, depth_usd: float, tolerance_bps: int) -> float:
    """Expected realised slip for one side: price impact of our order on the pool + adverse fill, never
    above the tolerance we quote with (the tolerance is a survival cap, not the expectation)."""
    impact = (size_usd / depth_usd * 100.0) if depth_usd > 0 else tolerance_bps / 100.0
    return min(tolerance_bps / 100.0, impact + ADVERSE_FILL_PCT)


def quote(*, size_usd: float, r_usd: float, first_target_r: float, protocol: str,
          entry_slip_bps: int, exit_slip_bps: int, fee_usd_round_trip: float, ladder: bool, depth_usd: float = 0.0,
          first_leg_frac: float = 0.35, measured_round_trip_pct: float | None = None) -> dict:
    """Friction that the FIRST cash-out must clear. Hunt: entry on the full bag + exit of the first ladder leg
    only (entry slip/fee on size, exit slip/fee/shave on size × first_leg_frac). Scalp / RH: full round trip.
    `measured_round_trip_pct` (e.g. the V4Quoter buy→sell at our size) replaces the modelled slip + protocol take."""
    size_usd = max(float(size_usd), 1e-9)
    leg = size_usd * (first_leg_frac if ladder else 1.0)
    proto = PROTOCOL_FEE_PCT.get(protocol, 1.0) / 100.0
    shave = (LADDER_SHAVE_PCT if ladder else TOKEN_SHAVE_PCT) / 100.0
    if measured_round_trip_pct is not None:
        slip_usd = max(0.0, float(measured_round_trip_pct)) / 100.0 * size_usd
        proto_usd = 0.0
    else:
        slip_usd = expected_slip_pct(size_usd, depth_usd, entry_slip_bps) / 100.0 * size_usd + expected_slip_pct(leg, depth_usd, exit_slip_bps) / 100.0 * leg
        proto_usd = proto * size_usd + proto * leg
    shave_usd = shave * leg
    cost_usd = slip_usd + proto_usd + shave_usd + fee_usd_round_trip
    cost_pct = cost_usd / size_usd * 100.0
    target_pct = first_target_r * r_usd / size_usd * 100.0
    ceiling = MAX_ROUND_TRIP_PCT_MEASURED if measured_round_trip_pct is not None else (MAX_ROUND_TRIP_PCT_RH if protocol == "rh" else MAX_ROUND_TRIP_PCT)
    ok = cost_pct <= ceiling and target_pct >= COST_MULT * cost_pct
    if cost_pct > ceiling:
        reason = f"round-trip cost {cost_pct:.1f}% > {ceiling:g}% ceiling"
    elif not ok:
        reason = f"first target {target_pct:.1f}% < {COST_MULT:g}× cost {cost_pct:.1f}%"
    else:
        reason = "pass"
    return {"expected_cost_pct": round(cost_pct, 3), "expected_cost_usd": round(cost_usd, 4),
            "expected_target_pct": round(target_pct, 3), "cost_gate_pass": ok, "cost_gate_reason": reason,
            "cost_breakdown": {"slip_pct": round(slip_usd / size_usd * 100, 3), "fee_pct": round(fee_usd_round_trip / size_usd * 100, 3),
                               "protocol_pct": round(proto_usd / size_usd * 100, 3), "shave_pct": round(shave_usd / size_usd * 100, 3),
                               "measured": measured_round_trip_pct is not None}}
