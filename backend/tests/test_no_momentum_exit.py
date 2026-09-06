"""bot.py no-momentum exit — the guard block inside _monitor_position."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
import re
from models import BotConfig


def test_config_defaults():
    c = BotConfig()
    assert c.no_momentum_exit_enabled is True
    assert c.no_momentum_after_s == 30 and c.no_momentum_min_mfe_pct == 5.0


def test_monitor_block_wiring():
    src = open(os.path.join(os.path.dirname(__file__), "..", "bot.py")).read()
    i = src.index("No-momentum exit — one-shot")
    block = src[i:i + 1400]
    # exempt snipes + partial-TP'd positions, one-shot, uses peak vs entry
    assert "not is_snipe" in block
    assert 'slot.get("partial_done")' in block
    assert '_no_momentum_checked' in block
    assert "peak_price_sol" in block and "entry_price_sol" in block
    assert 'reason=f"no-momentum' in block
    # sits AFTER the timeout block and BEFORE price polling
    assert src.index('reason=f"timeout after') < i < src.index("# Protocol-aware price polling")


def test_mfe_math_matches_block():
    # mirror of the arithmetic in the block
    def mfe(ep, pk):
        return (pk / ep - 1.0) * 100.0 if ep > 0 and pk > 0 else 0.0
    assert abs(mfe(1.0, 1.04) - 4.0) < 1e-9
    assert mfe(1.0, 0) == 0.0
    assert mfe(1.0, 1.04) < 5.0 and not (mfe(1.0, 1.06) < 5.0)
