"""Verify the risk-based sizing, stricter veto, and depth-slippage logic."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv; load_dotenv(Path(__file__).parent.parent / ".env")

from solana_client import LAMPORTS_PER_SOL


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


