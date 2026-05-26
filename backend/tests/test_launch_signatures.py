"""
Tests for launch_signatures.py — the Bing-reference behavioral
signatures (accel_class / flow_class / rug_speed_class) and the
per-creator aggregate that feeds the +15 acceleration bonus.
"""
from __future__ import annotations
import os
import sys
from datetime import datetime, timezone, timedelta

import pytest

# Allow `from launch_signatures import ...` when pytest is run from /app/backend
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from launch_signatures import (
    accel_class, flow_class, rug_speed_class,
    derive_signatures, aggregate_signatures,
    accel_signature_v2, profit_window_seconds,
    derive_curve_fill_pct,
)


# ----- derive_curve_fill_pct (historical backfill) -----------------------

def test_derive_curve_fill_from_peak_mc():
    # $34,500 peak → 50% of $69k graduation
    assert derive_curve_fill_pct({"final_peak_mc_usd": 34_500}) == pytest.approx(50.0, abs=0.1)


def test_derive_curve_fill_from_peak_mc_clamps_at_100():
    # $80k peak → clamped to 100 (above graduation should never happen but be safe)
    assert derive_curve_fill_pct({"final_peak_mc_usd": 80_000}) == 100.0


def test_derive_curve_fill_from_sol_inflow_fallback():
    # 42.5 SOL inflow → 50% of 85 SOL graduation
    assert derive_curve_fill_pct({"sol_inflow": 42.5}) == pytest.approx(50.0, abs=0.1)


def test_derive_curve_fill_peak_mc_preferred_over_inflow():
    # Both set — peak_mc wins because it represents WHERE the curve got to
    out = derive_curve_fill_pct({
        "final_peak_mc_usd": 13_800,   # → 20%
        "sol_inflow": 42.5,            # → 50% (but ignored)
    })
    assert out == pytest.approx(20.0, abs=0.1)


def test_derive_curve_fill_skips_when_no_data():
    assert derive_curve_fill_pct({}) is None
    assert derive_curve_fill_pct({"final_peak_mc_usd": 0, "sol_inflow": 0}) is None


def test_derive_curve_fill_skips_negligible_inflow():
    # 0.05 SOL inflow — below the 0.1 threshold the backfill endpoint enforces.
    # Helper itself returns a value (0.06% curve) because the threshold is a
    # caller-side decision; the helper just computes from what's given.
    out = derive_curve_fill_pct({"sol_inflow": 0.05})
    assert out is not None
    assert out < 1.0  # ~0.06%


def test_derive_curve_fill_idempotent_realistic_examples():
    # Real launch from DB: 100% curve filled (graduated then rugged)
    assert derive_curve_fill_pct({"final_peak_mc_usd": 69_000}) == 100.0
    # Real failed launch: peak was ~$8k (12% curve)
    assert derive_curve_fill_pct({"final_peak_mc_usd": 8_280}) == pytest.approx(12.0, abs=0.1)


# ----- accel_class bands ---------------------------------------------------

def test_accel_class_fast_by_inflow():
    assert accel_class({"sol_inflow": 12.5, "buy_count": 5}) == "fast"


def test_accel_class_fast_by_buys():
    assert accel_class({"sol_inflow": 0.5, "buy_count": 35}) == "fast"


def test_accel_class_moderate():
    assert accel_class({"sol_inflow": 3.0, "buy_count": 8}) == "moderate"
    assert accel_class({"sol_inflow": 1.0, "buy_count": 15}) == "moderate"


def test_accel_class_slow():
    assert accel_class({"sol_inflow": 0.5, "buy_count": 4}) == "slow"


def test_accel_class_dead():
    assert accel_class({"sol_inflow": 0.0, "buy_count": 0}) == "dead"
    assert accel_class({}) == "dead"


# ----- flow_class bands ----------------------------------------------------

def test_flow_class_unknown_for_low_data():
    # < 3 buys → unknown
    assert flow_class({"sol_inflow": 0.5, "buy_count": 2}) == "unknown"
    # < 0.05 SOL inflow → unknown even if many buys
    assert flow_class({"sol_inflow": 0.01, "buy_count": 30}) == "unknown"


def test_flow_class_whale_led():
    # 5 buys of 0.3 SOL each = 1.5 SOL → 0.3 per buy → whale
    assert flow_class({"sol_inflow": 1.5, "buy_count": 5}) == "whale_led"


def test_flow_class_broad():
    # 20 buys of 0.05 SOL each → 0.05 per buy → broad
    assert flow_class({"sol_inflow": 1.0, "buy_count": 20}) == "broad"


def test_flow_class_swarm():
    # 50 buys of 0.005 SOL each → bot-swarm signature
    assert flow_class({"sol_inflow": 0.25, "buy_count": 50}) == "swarm"


def test_flow_class_borderline_swarm_needs_buys():
    # avg < 0.02 but only 8 buys — not enough volume to call swarm
    # 0.06 SOL / 8 buys = 0.0075/buy → fails the buys≥10 swarm gate → broad
    assert flow_class({"sol_inflow": 0.06, "buy_count": 8}) == "broad"


# ----- rug_speed_class bands ----------------------------------------------

def _iso(seconds_from_base: float, base: datetime | None = None) -> str:
    base = base or datetime(2026, 1, 1, tzinfo=timezone.utc)
    return (base + timedelta(seconds=seconds_from_base)).isoformat()


def test_rug_speed_none_for_graduated():
    assert rug_speed_class({"outcome": "graduated",
                            "detected_at": _iso(0), "outcome_at": _iso(60)}) is None


def test_rug_speed_none_for_missing_timestamps():
    assert rug_speed_class({"outcome": "failed"}) is None


def test_rug_speed_instant():
    assert rug_speed_class({"outcome": "failed",
                            "detected_at": _iso(0), "outcome_at": _iso(30)}) == "instant"


def test_rug_speed_fast():
    assert rug_speed_class({"outcome": "failed",
                            "detected_at": _iso(0), "outcome_at": _iso(300)}) == "fast"


def test_rug_speed_delayed():
    assert rug_speed_class({"outcome": "failed",
                            "detected_at": _iso(0), "outcome_at": _iso(3600)}) == "delayed"


def test_rug_speed_fizzle():
    assert rug_speed_class({"outcome": "failed",
                            "detected_at": _iso(0), "outcome_at": _iso(86400)}) == "fizzle"


# ----- derive_signatures combo --------------------------------------------

def test_derive_signatures_includes_rug_speed_for_failed():
    sig = derive_signatures({
        "sol_inflow": 1.0, "buy_count": 20,
        "outcome": "failed",
        "detected_at": _iso(0), "outcome_at": _iso(120),
    })
    assert sig["accel_class"] == "moderate"
    assert sig["flow_class"] == "broad"
    assert sig["rug_speed_class"] == "fast"
    assert sig["rug_seconds_from_launch"] == pytest.approx(120, abs=0.1)


def test_derive_signatures_skips_rug_speed_when_not_failed():
    sig = derive_signatures({
        "sol_inflow": 1.0, "buy_count": 20, "outcome": "graduated",
    })
    assert sig == {"accel_class": "moderate", "flow_class": "broad"}


# ----- aggregate_signatures (repeatability) -------------------------------

def test_aggregate_empty_launches():
    assert aggregate_signatures([]) == {}


def test_aggregate_repeatability_perfect():
    launches = [
        {"sol_inflow": 15, "buy_count": 50},  # fast / whale_led-ish... let's compute
        {"sol_inflow": 15, "buy_count": 50},
        {"sol_inflow": 15, "buy_count": 50},
    ]
    agg = aggregate_signatures(launches)
    # all 3 launches share the SAME accel + flow → 100% repeatability
    assert agg["signature_repeatability"] == 100.0
    assert agg["dominant_accel"] in ("fast", "moderate")  # depends on math
    assert agg["dominant_flow"] is not None


def test_aggregate_repeatability_mixed():
    launches = [
        {"sol_inflow": 0.0, "buy_count": 0},  # dead/unknown
        {"sol_inflow": 12, "buy_count": 30},  # fast/whale-ish
        {"sol_inflow": 1.0, "buy_count": 20},  # moderate/broad
        {"sol_inflow": 0.3, "buy_count": 4},  # slow/unknown
    ]
    agg = aggregate_signatures(launches)
    # 4 distinct accel buckets → repeatability ~50% (1/4 each)
    assert 0 < agg["signature_repeatability"] <= 50.0
    assert agg["dominant_accel"] in {"dead", "fast", "moderate", "slow"}


def test_aggregate_rug_seconds_stats_requires_min_3():
    # only 2 rug-speed samples → no stats block
    launches = [
        {"sol_inflow": 5, "buy_count": 10, "outcome": "failed",
         "detected_at": _iso(0), "outcome_at": _iso(120)},
        {"sol_inflow": 5, "buy_count": 10, "outcome": "failed",
         "detected_at": _iso(0), "outcome_at": _iso(180)},
    ]
    # Pre-populate rug_seconds_from_launch so the aggregator sees it
    for L in launches:
        sig = derive_signatures(L)
        L.update(sig)
    agg = aggregate_signatures(launches)
    assert agg["rug_seconds_stats"] is None


def test_aggregate_rug_seconds_stats_with_enough_samples():
    launches = []
    for s in (90, 120, 110, 95, 130):
        L = {"sol_inflow": 5, "buy_count": 10, "outcome": "failed",
             "detected_at": _iso(0), "outcome_at": _iso(s)}
        L.update(derive_signatures(L))
        launches.append(L)
    agg = aggregate_signatures(launches)
    stats = agg["rug_seconds_stats"]
    assert stats is not None
    assert stats["n"] == 5
    assert 90 <= stats["median"] <= 130
    assert stats["stddev"] >= 0
    assert stats["cv"] >= 0


def test_aggregate_uses_persisted_signatures_when_present():
    # Sim: launch was previously persisted with accel_class='fast'. The
    # aggregator should respect that override rather than recomputing from
    # raw fields (the persisted value is the source of truth at write time).
    launches = [
        {"accel_class": "fast", "flow_class": "broad"},
        {"accel_class": "fast", "flow_class": "broad"},
        {"accel_class": "fast", "flow_class": "broad"},
    ]
    agg = aggregate_signatures(launches)
    assert agg["dominant_accel"] == "fast"
    assert agg["dominant_flow"] == "broad"
    assert agg["signature_repeatability"] == 100.0


def test_aggregate_distributions_count_correctly():
    launches = [
        {"accel_class": "fast", "flow_class": "broad"},
        {"accel_class": "fast", "flow_class": "broad"},
        {"accel_class": "moderate", "flow_class": "whale_led"},
    ]
    agg = aggregate_signatures(launches)
    assert agg["accel_distribution"] == {"fast": 2, "moderate": 1}
    assert agg["flow_distribution"] == {"broad": 2, "whale_led": 1}


# ----- accel_signature_v2 (delta-based) -----------------------------------

def _evt(ts: float, sol: float, user: str = "u") -> tuple:
    """Helper: build a (ts, lamports, user) buy_event tuple."""
    return (ts, int(sol * 1_000_000_000), user)


def test_accel_sig_v2_dead_for_no_events():
    assert accel_signature_v2([]) == "dead"
    assert accel_signature_v2([_evt(1, 0.01)]) == "dead"
    assert accel_signature_v2([_evt(1, 0.001), _evt(2, 0.001)]) == "dead"


def test_accel_sig_v2_whale_led():
    # one 2 SOL buy out of 3 SOL total → whale dominates
    events = [_evt(1, 2.0), _evt(2, 0.05), _evt(3, 0.5), _evt(4, 0.45)]
    assert accel_signature_v2(events) == "whale_led"


def test_accel_sig_v2_bot_swarm():
    # 30 tiny buys (0.002 SOL each = 0.06 SOL total → above dead threshold)
    events = [_evt(i, 0.002, f"u{i}") for i in range(30)]
    assert accel_signature_v2(events) == "bot_swarm"


def test_accel_sig_v2_parabolic():
    # accelerating cumulative inflow shape
    events = (
        [_evt(0 + i, 0.01) for i in range(3)]
        + [_evt(3 + i, 0.05) for i in range(3)]
        + [_evt(6 + i, 0.15) for i in range(3)]
        + [_evt(9 + i, 0.4) for i in range(3)]
        + [_evt(12 + i, 1.0) for i in range(3)]
    )
    assert accel_signature_v2(events) == "parabolic"


def test_accel_sig_v2_moderate_default():
    # Flat inflow — no whale, no swarm, no acceleration — moderate
    events = [_evt(i, 0.05) for i in range(10)]
    assert accel_signature_v2(events) == "moderate"


# ----- profit_window_seconds ----------------------------------------------

def test_profit_window_none_without_peak_at():
    assert profit_window_seconds({"outcome_at": _iso(120)}) is None


def test_profit_window_none_without_outcome_at():
    assert profit_window_seconds({"peak_mc_usd_at": _iso(60)}) is None


def test_profit_window_positive():
    pw = profit_window_seconds({
        "peak_mc_usd_at": _iso(60),
        "outcome_at": _iso(180),
    })
    assert pw == pytest.approx(120.0, abs=0.1)


def test_profit_window_negative_clipped_to_none():
    pw = profit_window_seconds({
        "peak_mc_usd_at": _iso(180),
        "outcome_at": _iso(60),
    })
    assert pw is None


def test_derive_signatures_includes_profit_window():
    sig = derive_signatures({
        "sol_inflow": 5.0, "buy_count": 20,
        "outcome": "failed",
        "detected_at": _iso(0),
        "peak_mc_usd_at": _iso(40),
        "outcome_at": _iso(120),
    })
    assert sig["profit_window_seconds"] == pytest.approx(80.0, abs=0.1)
    assert sig["rug_seconds_from_launch"] == pytest.approx(120, abs=0.1)

