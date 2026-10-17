"""EMERGENT_CHANGES #3 — CU limits per venue/side, fee ladder capped by position size, sender mode per exit kind."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot import BotState  # noqa: E402
from models import BotConfig  # noqa: E402
import multihop  # noqa: E402


def _st(**over):
    st = BotState.__new__(BotState)
    st.config = BotConfig(**over)
    return st


def test_cu_limit_four_combinations_and_fallback():
    st = _st()
    assert st.cu_limit("pumpfun", "buy") == 135_000 and st.cu_limit("pumpswap", "buy") == 150_000
    assert st.cu_limit("pumpfun", "sell") == 80_000 and st.cu_limit("pumpswap", "sell") == 130_000
    st.config.cu_limit_pumpfun_sell = 0
    assert st.cu_limit("pumpfun", "sell") == 200_000                    # CU_PUMPFUN fallback only when unset


def test_exit_fee_ladder_capped_by_position():
    st = _st(exit_priority_floor_microlamports=1_000_000, exit_priority_cap_microlamports=5_000_000, exit_fee_max_pct_of_position=2.0)
    big = st._exit_fee_ladder(1_000_000, False, position_sol=0.048, cu=80_000)
    assert big[-1] == 5_000_000 and big[0] == 1_000_000                 # 11.9M cap > 5M → config cap wins
    small = st._exit_fee_ladder(1_000_000, False, position_sol=0.009, cu=80_000)
    exp_cap = int((0.009e9 * 0.02 - 5_000) * 1e6 / 80_000)             # ≈ 2.19M µL
    assert small[-1] == exp_cap and 2_100_000 < exp_cap < 2_300_000 and all(x <= exp_cap for x in small)
    panic = st._exit_fee_ladder(20_000_000, True, position_sol=0.009, cu=80_000)   # panic start also limited by the cap
    assert panic == [250_000]        # dual tip (200k) eats the 2 % budget → floor // 4 is the hard minimum
    assert st._exit_fee_ladder(1_000_000, False)[-1] == 5_000_000      # unknown position → old behaviour


def test_buy_fee_ladder_capped_and_sender_modes():
    st = _st(entry_fee_max_pct_of_position=1.0)
    lad = st._buy_fee_ladder(2_000_000, 0.01, 135_000)
    cap = int((0.01e9 * 0.01 - 5_000) * 1e6 / 135_000)
    assert lad == [cap] and cap < 2_000_000
    st.config.buy_fee_ladder_rungs = 2
    assert st._buy_fee_ladder(100_000, 0.01, 135_000) == [100_000, 200_000]
    assert st._sender_mode(False) == "swqos" and st._sender_mode(True) == "dual"
    st.config.exit_sender_mode_normal = "dual"
    assert st._sender_mode(False) == "dual"


def test_routed_cu_from_simulation():
    multihop.LAST_UNITS_CONSUMED.clear()
    assert multihop.cu_from_simulation("m", 1.2, fallback=400_000) == 400_000
    multihop.LAST_UNITS_CONSUMED["m"] = 109_000
    assert multihop.cu_from_simulation("m", 1.2) == 130_800
    multihop.LAST_UNITS_CONSUMED["m"] = 10_000
    assert multihop.cu_from_simulation("m", 1.2) == 80_000


def test_sender_mode_literal_rejects_garbage():
    import pytest
    with pytest.raises(Exception):
        BotConfig(exit_sender_mode_normal="jito")
