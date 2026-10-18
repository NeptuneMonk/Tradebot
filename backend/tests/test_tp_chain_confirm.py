"""Fast-path take-profit must be confirmed on chain — a corrupt tape tick (+300 %…+12000 %) must not sell at −3 %."""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
import bot as botmod  # noqa: E402
from bot import BotState  # noqa: E402
from models import BotConfig  # noqa: E402


def _state(chain_px):
    st = BotState.__new__(BotState)
    st.config = BotConfig(book_exits={"hunt": {"stop_loss_pct": 15.0, "target_r": 0.0, "take_profit_pct": 30.0, "trailing_stop_pct": 0.0,
                                               "ladder_1r_sell_pct": 0.0, "ladder_2r_sell_pct": 0.0}})
    st.stats = {}
    st.tracking = {}

    async def fake_curve(mint):
        return {"complete": False, "virtual_sol_reserves": chain_px * 1e9 * 1e6, "virtual_token_reserves": 1e6}
    st._fake_curve = fake_curve
    return st


def _slot(entry=1e-9, peak=None):
    return {"trade": {"id": "t", "symbol": "PERRO", "entry_price_sol": entry, "book": "hunt", "protocol": "pumpfun", "classifier_action": "smart_wallet"},
            "_entry_ts_mono": time.time() - 20, "peak_price_sol": peak or entry, "protocol": "pumpfun"}


def test_bogus_tp_tick_rejected_and_baseline_reset(monkeypatch):
    st = _state(chain_px=0.97e-9)                          # chain says −3 %
    monkeypatch.setattr(botmod.pumpfun, "fetch_bonding_curve_state", st._fake_curve)
    slot = _slot(peak=125e-9)                              # tape printed a +12435 % tick
    ok = asyncio.run(st._confirm_tp_on_chain("m", slot, 125e-9, 12435.0))
    assert ok is False and st.stats["tp_ticks_rejected"] == 1
    assert abs(slot["_last_sane_price_sol"] - 0.97e-9) < 1e-20 and slot["peak_price_sol"] == 1e-9   # peak no longer the bogus tick
    # second ask inside 1 s is throttled (no sell either)
    assert asyncio.run(st._confirm_tp_on_chain("m", slot, 125e-9, 12435.0)) is False


def test_real_tp_confirmed(monkeypatch):
    st = _state(chain_px=1.32e-9)                          # chain +32 % ≥ +30 % target
    monkeypatch.setattr(botmod.pumpfun, "fetch_bonding_curve_state", st._fake_curve)
    assert asyncio.run(st._confirm_tp_on_chain("m", _slot(), 1.31e-9, 31.0)) is True
    assert st.stats.get("tp_ticks_rejected", 0) == 0


def test_unreadable_chain_trusts_only_plausible_ticks(monkeypatch):
    st = _state(chain_px=0)

    async def boom(mint):
        raise RuntimeError("rpc down")
    monkeypatch.setattr(botmod.pumpfun, "fetch_bonding_curve_state", boom)
    assert asyncio.run(st._confirm_tp_on_chain("m", _slot(), 1.31e-9, 31.0)) is True          # +31 % on a +30 % target: plausible
    s2 = _slot()
    assert asyncio.run(st._confirm_tp_on_chain("m2", s2, 4e-9, 300.0)) is False               # +300 %: not without the chain
