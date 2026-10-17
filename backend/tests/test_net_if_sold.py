"""Net-if-sold-now: open position mark minus exit slip, protocol fee, dust shave, sell tx fee+tip and the entry fee."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
from bot import BotState  # noqa: E402
from models import BotConfig  # noqa: E402


def _st():
    st = BotState.__new__(BotState)
    st.config = BotConfig(speed_mode="manual", priority_fee_microlamports=200_000, exit_slippage_bps=1000)
    return st


def test_net_if_sold_is_below_gross_and_itemised():
    st = _st()
    d = {"entry_price_sol": 1e-7, "entry_sol": 0.01, "entry_fee_sol": 0.0001, "protocol": "pumpfun"}
    slot = {"_last_price_sol": 1.5e-7, "_depth_sol": 30.0, "protocol": "pumpfun"}
    n = st.net_if_sold(d, slot, 150.0)
    assert n is not None
    gross = 0.015 * 150.0
    assert abs(n["gross_usd"] - gross) < 1e-6
    assert n["net_usd"] < n["gross_usd"]
    c = n["costs_usd"]
    assert c["protocol"] > 0 and c["shave"] > 0 and c["tx_fee"] > 0 and c["slip"] > 0 and abs(c["entry_fee"] - 0.015) < 1e-9
    assert abs((n["gross_usd"] - n["net_usd"]) - (c["slip"] + c["protocol"] + c["shave"] + c["tx_fee"])) < 1e-3
    # net P/L = net proceeds − (stake + entry fee)
    assert abs(n["net_pnl_usd"] - (n["net_usd"] - (0.01 + 0.0001) * 150.0)) < 1e-3
    assert n["net_pnl_pct"] is not None and n["net_pnl_pct"] < 50.0      # gross is +50 %, net must be lower


def test_net_if_sold_none_without_mark():
    st = _st()
    assert st.net_if_sold({"entry_price_sol": 1e-7, "entry_sol": 0.01}, {}, 150.0) is None
    assert st.net_if_sold({"entry_price_sol": 1e-7, "entry_sol": 0.01}, {"_last_price_sol": 1e-7}, 0.0) is None


def test_thin_pool_slip_is_capped_at_exit_tolerance():
    st = _st()
    d = {"entry_price_sol": 1e-7, "entry_sol": 0.5, "entry_fee_sol": 0.0}
    n = st.net_if_sold(d, {"_last_price_sol": 1e-7, "_depth_sol": 0.2, "protocol": "pumpfun"}, 150.0)
    assert n["slip_pct"] == 10.0 and n["exit_slip_cap_bps"] == 1000
