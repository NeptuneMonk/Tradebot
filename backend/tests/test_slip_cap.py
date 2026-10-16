"""max_slippage_bps is a hard ceiling on every slippage the bot emits."""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from slippage import entry_slip_bps, auto_exit_slip_bps, capped  # noqa: E402


def _cfg(cap=300):
    return SimpleNamespace(max_slippage_bps=cap, auto_exit_slip_base_bps=300, auto_exit_slip_thin_pool_sol=8.0,
                           auto_exit_slip_thin_pool_extra_bps=200, auto_exit_slip_vol_threshold_pct=8.0,
                           auto_exit_slip_high_vol_extra_bps=200, auto_exit_slip_panic_extra_bps=400, auto_exit_slip_cap_bps=1200)


def test_entry_floor_is_capped():
    assert entry_slip_bps("pumpfun", 20.0, 600) == 2500                 # legacy: thin curve floor wins
    assert entry_slip_bps("pumpfun", 20.0, 600, _cfg()) == 300          # capped at 3 %
    assert entry_slip_bps("pumpswap", 100.0, 200, _cfg()) == 300        # deep-pool floor 800 → capped
    assert entry_slip_bps("pumpswap", 100.0, 200, _cfg(cap=1000)) == 800  # under a higher cap: floor untouched


def test_exit_auto_slip_is_capped():
    assert auto_exit_slip_bps(_cfg(), panic=True, pool_depth_sol=3.0, recent_vol_pct=20.0) == 300
    assert auto_exit_slip_bps(_cfg(cap=0), panic=True, pool_depth_sol=3.0, recent_vol_pct=20.0) == 1100   # cap off


def test_capped_helper():
    assert capped(1500, _cfg()) == 300 and capped(150, _cfg()) == 150 and capped(1500, _cfg(cap=0)) == 1500
