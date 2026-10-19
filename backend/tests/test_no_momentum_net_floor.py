"""No-momentum profit floor is judged on NET-if-sold, not the gross tape % (user: '+4.9 % exit filled −3.6 %')."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
import bot as botmod  # noqa: E402
from bot import BotState  # noqa: E402
from models import BotConfig  # noqa: E402

SOL_USD = 150.0


def _state():
    st = BotState.__new__(BotState)
    st.config = BotConfig()
    st.stats = {}
    st.tracking = {}
    return st


def _slot(stake_usd=2.50, entry=1e-9, depth_sol=30.0):
    entry_sol = stake_usd / SOL_USD
    return {"trade": {"id": "t", "symbol": "speed", "entry_price_sol": entry, "entry_sol": entry_sol, "entry_fee_sol": 0.0002,
                      "book": "hunt", "protocol": "pumpfun"},
            "protocol": "pumpfun", "_depth_sol": depth_sol}


def test_gross_green_net_red_is_held(monkeypatch):
    monkeypatch.setitem(botmod.solana_client._sol_price_cache, "price", SOL_USD)
    st = _state()
    gross, net = st._nm_pnl(_slot(), 1.049e-9, 1e-9)
    assert round(gross, 1) == 4.9
    assert net is not None and net < gross
    floor = float(st.config.no_momentum_min_profit_pct)
    # the user's case: +4.9 % gross on a $2.50 stake does NOT clear the floor once fees are counted
    assert net < floor <= gross


def test_clearly_green_still_exits(monkeypatch):
    monkeypatch.setitem(botmod.solana_client._sol_price_cache, "price", SOL_USD)
    st = _state()
    gross, net = st._nm_pnl(_slot(stake_usd=25.0), 1.30e-9, 1e-9)
    assert round(gross, 1) == 30.0 and net is not None
    assert net >= float(st.config.no_momentum_min_profit_pct)


def test_falls_back_to_gross_without_sol_price(monkeypatch):
    monkeypatch.setitem(botmod.solana_client._sol_price_cache, "price", 0.0)
    st = _state()
    gross, net = st._nm_pnl(_slot(), 1.049e-9, 1e-9)
    assert round(gross, 1) == 4.9 and net is None
