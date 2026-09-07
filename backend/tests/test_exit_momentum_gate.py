"""Buy-momentum exit gate: SL/TP defer while buyers are still piling in."""
import os
import sys
import time
from collections import deque
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from bot import BotState  # noqa: E402
from models import BotConfig  # noqa: E402
from solana_client import LAMPORTS_PER_SOL  # noqa: E402

MINT = "So11111111111111111111111111111111111111112"


def make(cfg=None, buyers=0, sol_each=0.1, age_s=2):
    st = BotState.__new__(BotState)
    st.config = BotConfig(**{"stop_loss_pct": 20.0, **(cfg or {})})
    now = time.time()
    ev = deque([(now - age_s, int(sol_each * LAMPORTS_PER_SOL), f"w{i}") for i in range(buyers)])
    st.tracking = {MINT: {"buy_events": ev}}
    return st, {}


def test_strong_momentum_defers_sl_and_tp():
    st, slot = make(buyers=4, sol_each=0.1)
    assert st._buy_momentum_holds(MINT, slot, "sl", -22.0) is True
    assert "_mom_defer_sl" in slot
    assert st._buy_momentum_holds(MINT, slot, "tp", 45.0) is True


def test_weak_momentum_lets_exit_fire_and_clears_state():
    st, slot = make(buyers=2, sol_each=0.1)            # below min_buyers=3
    slot["_mom_defer_sl"] = time.time() - 5
    assert st._buy_momentum_holds(MINT, slot, "sl", -22.0) is False
    assert "_mom_defer_sl" not in slot
    st, slot = make(buyers=5, sol_each=0.01)           # 0.05 SOL < min_inflow 0.25
    assert st._buy_momentum_holds(MINT, slot, "sl", -22.0) is False
    st, slot = make(buyers=5, sol_each=0.1, age_s=30)  # outside 10s window
    assert st._buy_momentum_holds(MINT, slot, "sl", -22.0) is False


def test_deferred_sl_is_bounded_at_sl_plus_extra():
    st, slot = make({"stop_loss_pct": 20.0, "exit_momentum_max_extra_loss_pct": 5.0}, buyers=6, sol_each=0.2)
    assert st._buy_momentum_holds(MINT, slot, "sl", -22.0) is True
    assert st._buy_momentum_holds(MINT, slot, "sl", -24.9) is True
    assert st._buy_momentum_holds(MINT, slot, "sl", -25.0) is False   # SL 20 + 5 → fire
    assert st._buy_momentum_holds(MINT, slot, "tp", 45.0) is True      # TP deferral unaffected


def test_bound_is_the_only_floor_and_defer_budget():
    st, slot = make({"exit_momentum_max_extra_loss_pct": 50.0}, buyers=6, sol_each=0.2)   # SL 20 + 50 = -70
    assert st._buy_momentum_holds(MINT, slot, "sl", -71.0) is False  # past SL+extra
    assert st._buy_momentum_holds(MINT, slot, "sl", -61.0) is True   # no separate -60 hard floor any more
    assert st._buy_momentum_holds(MINT, slot, "sl", -30.0) is True
    slot["_mom_defer_sl"] = time.time() - 21                         # budget (20s) spent
    assert st._buy_momentum_holds(MINT, slot, "sl", -30.0) is False


def test_gate_disabled_and_missing_bucket():
    st, slot = make({"exit_momentum_gate_enabled": False}, buyers=6, sol_each=0.2)
    assert st._buy_momentum_holds(MINT, slot, "sl", -22.0) is False
    st, slot = make(buyers=6)
    assert st._buy_momentum_holds("unknownmint", slot, "tp", 40.0) is False


def test_all_five_exit_sites_are_gated():
    src = open(os.path.join(os.path.dirname(__file__), "..", "bot.py")).read()
    assert src.count("self._buy_momentum_holds(mint, slot, \"sl\", pct_change)") == 3
    assert src.count("self._buy_momentum_holds(mint, slot, \"tp\", pct_change)") == 2
    # trailing stop untouched (user asked for SL/TP only)
    assert "_buy_momentum_holds(mint, slot, \"trail\"" not in src
