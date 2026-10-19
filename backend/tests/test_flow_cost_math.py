"""Flow-gate liquidity and cost-gate math (operator: 'cost-gate and flow gates holding us out — bad math')."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
import cost_gate  # noqa: E402
import flow  # noqa: E402
from models import BotConfig  # noqa: E402

L = 1_000_000_000


def test_pumpswap_liquidity_is_the_real_pool_quote_not_minus_30_sol():
    b = {"protocol": "pumpswap", "last_vsr_lamports": 40 * L, "last_real_sol_lamports": 40 * L}
    assert flow.liquidity(b, sol=True) == 40.0
    b = {"protocol": "pumpswap", "last_vsr_lamports": 25 * L}        # legacy field only, pool under 30 SOL
    assert flow.liquidity(b, sol=True) == 25.0                        # old code: None → gate 'unknown'


def test_curve_liquidity_still_strips_virtual_offset():
    assert flow.liquidity({"protocol": "pumpfun", "last_vsr_lamports": 100 * L}, sol=True) == 70.0
    assert flow.liquidity({"protocol": "pumpfun", "last_vsr_lamports": 100 * L, "last_real_sol_lamports": 70 * L}, sol=True) == 70.0


def test_flow_ratio_uses_corrected_liquidity():
    now = 1000.0
    b = {"protocol": "pumpswap", "last_vsr_lamports": 40 * L, "last_real_sol_lamports": 40 * L,
         "buy_events": [(now - 5, 2 * L, "a")], "sell_events": []}
    assert flow.flow_ratio_pct(b, now, 30, sol=True) == 5.0             # 2 SOL into 40 SOL (old code: 20 %)


def test_adverse_fill_is_tunable_and_moves_the_gate():
    sol = 104.0
    kw = dict(size_usd=10.0, r_usd=1.9, first_target_r=None, protocol="pumpfun", entry_slip_bps=300, exit_slip_bps=300,
              fee_usd_round_trip=0.065, ladder=False, depth_usd=20 * sol)
    tight = cost_gate.quote(**kw, cfg=BotConfig(protocol_fee_pct_pumpfun=1.25, cost_gate_adverse_fill_pct=1.0))
    loose = cost_gate.quote(**kw, cfg=BotConfig(protocol_fee_pct_pumpfun=1.25, cost_gate_adverse_fill_pct=0.5))
    assert not tight["cost_gate_pass"] and tight["expected_cost_pct"] > 8.0
    assert loose["cost_gate_pass"] and abs(tight["expected_cost_pct"] - loose["expected_cost_pct"] - 1.0) < 0.01   # 0.5 % × 2 sides


def test_expected_slip_default_unchanged():
    assert cost_gate.expected_slip_pct(10.0, 1000.0, 300) == 2.0          # 1 % impact + 1 % adverse
    assert cost_gate.expected_slip_pct(10.0, 1000.0, 300, 0.0) == 1.0
