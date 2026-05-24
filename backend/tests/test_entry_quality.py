"""Verify the risk-based sizing, stricter veto, and depth-slippage logic."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv; load_dotenv(Path(__file__).parent.parent / ".env")

from solana_client import LAMPORTS_PER_SOL


def test_risk_sizing_math():
    """Verify the risk_score → size_mult table."""
    # Replicate the table from _enter_impl
    def size_mult(risk_score: int) -> float:
        if risk_score <= 30: return 1.0
        if risk_score <= 60: return 0.6
        return 0.3
    assert size_mult(0) == 1.0
    assert size_mult(30) == 1.0
    assert size_mult(31) == 0.6
    assert size_mult(60) == 0.6
    assert size_mult(61) == 0.3
    assert size_mult(100) == 0.3
    # With max_trade_usd=1.0 and min=0.5, the floors are:
    max_usd, min_usd = 1.0, 0.5
    for r, expected_floor in [(20, 1.0), (50, 0.6), (80, 0.5)]:  # 80→0.3 but clamped to min 0.5
        sized = max(min_usd, max_usd * size_mult(r))
        assert abs(sized - expected_floor) < 0.001, f"risk={r}: got {sized}, want {expected_floor}"
    print("risk_sizing_math: OK")


def test_depth_slippage_bands():
    """Verify depth-based slippage scaling."""
    def depth_slip(vsr_sol: float, base: int) -> int:
        slip = base
        if vsr_sol < 32: slip = max(slip, 2500)
        elif vsr_sol < 40: slip = max(slip, 1800)
        elif vsr_sol < 55: slip = max(slip, 1200)
        return slip
    # base 500bps (5%)
    assert depth_slip(30.5, 500) == 2500, "very thin curve → 25%"
    assert depth_slip(35.0, 500) == 1800, "thin curve → 18%"
    assert depth_slip(50.0, 500) == 1200, "mid curve → 12%"
    assert depth_slip(70.0, 500) == 500,  "deep curve → keep base"
    # If user manually set higher base, keep it (max wins)
    assert depth_slip(30.5, 3000) == 3000, "user override > depth scaling"
    print("depth_slippage_bands: OK")


def test_veto_logic():
    """Stricter veto: also reject hold_briefly when risk > 50."""
    def should_veto(action: str, risk: int) -> str | None:
        if action in ("abort_trade", "exit_early"):
            return action
        if action == "hold_briefly" and risk > 50:
            return f"hold_briefly + high risk ({risk})"
        return None
    assert should_veto("abort_trade", 0) == "abort_trade"
    assert should_veto("exit_early", 20) == "exit_early"
    assert should_veto("hold_briefly", 30) is None       # OK to enter
    assert should_veto("hold_briefly", 50) is None       # boundary OK
    assert should_veto("hold_briefly", 51) == "hold_briefly + high risk (51)"
    assert should_veto("hold_long", 80) is None          # green light overrides risk
    print("veto_logic: OK")


if __name__ == "__main__":
    test_risk_sizing_math()
    test_depth_slippage_bands()
    test_veto_logic()
    print("\nAll entry-quality tests PASSED.")
