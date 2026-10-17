"""EMERGENT_CHANGES #4 — cost gate with real fee figures."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import cost_gate  # noqa: E402
from bot import BotState  # noqa: E402
from models import BotConfig  # noqa: E402


def _st(**over):
    st = BotState.__new__(BotState)
    st.config = BotConfig(**over)
    return st


def test_round_trip_fee_matches_spec_example():
    st = _st(exit_priority_floor_microlamports=500_000, exit_sender_mode_normal="swqos")
    fee_sol = st.round_trip_fee_sol("pumpfun", 150_000)
    assert abs(fee_sol - 0.00008025) < 1e-9                 # (5000 + 20250 + 5000 + 5000 + 40000 + 5000)/1e9
    fee_pct = fee_sol * 105 / 5 * 100
    assert 0.16 < fee_pct < 0.18


def test_gate_reads_cfg_and_adds_failed_tx_allowance():
    cfg = BotConfig(protocol_fee_pct_pumpfun=1.25, cost_gate_failed_tx_allowance_pct=1.5, cost_gate_cost_mult=2.0, cost_gate_max_round_trip_pct=8.0)
    q = cost_gate.quote(size_usd=5.0, r_usd=1.0, first_target_r=1.0, protocol="pumpfun", entry_slip_bps=300, exit_slip_bps=800,
                        fee_usd_round_trip=0.00008025 * 105, ladder=False, depth_usd=5000.0, cfg=cfg)
    b = q["cost_breakdown"]
    assert abs(b["protocol_pct"] - 2.5) < 1e-6 and abs(b["failed_pct"] - 1.5) < 1e-6 and 0.16 < b["fee_pct"] < 0.18
    # first target 20 % of size ≥ 2 × cost (~6.3 %) → pass
    assert q["cost_gate_pass"], q
    tight = cost_gate.quote(size_usd=5.0, r_usd=0.1, first_target_r=1.0, protocol="pumpfun", entry_slip_bps=300, exit_slip_bps=800,
                            fee_usd_round_trip=0.00008025 * 105, ladder=False, depth_usd=5000.0, cfg=cfg)
    assert not tight["cost_gate_pass"] and "2× cost" in tight["cost_gate_reason"]


def test_one_dollar_clip_with_old_fees_fails_ceiling():
    cfg = BotConfig()
    # old behaviour: exit 2M µL × 400k CU + dual tip (200k) + entry ≈ 0.001 SOL → ~10 % of a $1 clip at $105
    old_fee_sol = (5_000 + 150_000 * 200_000 / 1e6 + 5_000 + 5_000 + 2_000_000 * 400_000 / 1e6 + 200_000) / 1e9
    q = cost_gate.quote(size_usd=1.0, r_usd=0.2, first_target_r=1.0, protocol="pumpfun", entry_slip_bps=300, exit_slip_bps=800,
                        fee_usd_round_trip=old_fee_sol * 105, ladder=False, depth_usd=5000.0, cfg=cfg)
    assert not q["cost_gate_pass"] and "ceiling" in q["cost_gate_reason"]
    assert q["cost_breakdown"]["fee_pct"] > 8.0


def test_quote_without_cfg_keeps_constants():
    q = cost_gate.quote(size_usd=5.0, r_usd=1.0, first_target_r=1.0, protocol="pumpfun", entry_slip_bps=300, exit_slip_bps=800,
                        fee_usd_round_trip=0.01, ladder=False, depth_usd=5000.0)
    assert abs(q["cost_breakdown"]["protocol_pct"] - 2.0) < 1e-6 and q["cost_breakdown"]["failed_pct"] == 0.0
