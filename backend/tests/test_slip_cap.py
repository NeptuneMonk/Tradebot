"""max_slippage_bps is a hard ceiling on every slippage the bot emits."""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from slippage import entry_slip_bps, auto_exit_slip_bps, capped, exit_capped, exit_slip_cap_bps  # noqa: E402


def _cfg(cap=300, exit_cap=0):
    return SimpleNamespace(max_slippage_bps=cap, max_exit_slippage_bps=exit_cap, auto_exit_slip_base_bps=300, auto_exit_slip_thin_pool_sol=8.0,
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


# ---- EMERGENT_CHANGES #1: exits have their own ceiling ----
def test_exit_cap_is_separate_from_entry_cap():
    c = _cfg(cap=300, exit_cap=2000)
    assert exit_slip_cap_bps(c) == 2000 and exit_capped(1500, c) == 1500 and exit_capped(2500, c) == 2000
    assert capped(1500, c) == 300                                              # entry side untouched at 3 %
    assert auto_exit_slip_bps(c, panic=True, pool_depth_sol=3.0, recent_vol_pct=20.0) == 1100   # panic exit no longer squashed to 3 %
    assert exit_slip_cap_bps(_cfg(cap=300, exit_cap=0)) == 300                # exit cap 0 → old behaviour (entry cap)


def test_exit_slip_ladder_terminal_rung():
    import asyncio
    from bot import BotState
    st = BotState.__new__(BotState)
    st.config = SimpleNamespace(max_slippage_bps=300, max_exit_slippage_bps=2000, intelligent_exit_v2=True,
                                auto_exit_retry_slip_floors_bps=[1500, 2500, 3000], exit_terminal_slippage_bps=3000)
    assert st._exit_slip_ladder(800) == [800, 1500, 2000]                      # every rung under the exit cap
    assert st._exit_slip_ladder(800, terminal=True) == [800, 1500, 2000, 3000]  # one extra capped terminal rung
    st.config.exit_terminal_slippage_bps = 9000
    assert st._exit_slip_ladder(800, terminal=True)[-1] == 5000               # never above 50 %, never unlimited
