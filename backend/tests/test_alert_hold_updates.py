"""Bot OFF → Telegram unrealized P/L of manual / LTH holds: baseline once, then every ±alert_hold_update_pct move."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
from alerts import Alerts  # noqa: E402
from models import BotConfig  # noqa: E402


class _State:
    def __init__(self):
        self.config = BotConfig(enabled=False, alert_hold_update_pct=5.0)
        self.active_trades = {}
        self.sent = []

    def _is_manual_hold(self, slot):
        return bool(slot["trade"].get("long_term_hold"))

    def net_if_sold(self, t, slot, sol_usd):
        return {"net_pnl_usd": 1.23, "net_pnl_pct": 4.5}


def _alerts(st):
    a = Alerts(st)
    a.token, a.chat_id = "x", 1

    async def fake_send(kind, text):
        st.sent.append((kind, text))
        return True
    a.send = fake_send
    return a


def _slot(entry, cur, lth=True, sym="LUNA"):
    return {"trade": {"id": "t", "symbol": sym, "entry_price_sol": entry, "entry_usd": 10.0, "long_term_hold": lth}, "_last_price_sol": cur, "peak_price_sol": max(entry, cur)}


def test_baseline_then_only_on_5pt_moves_and_reset_on_start():
    st = _State()
    st.active_trades = {"L": _slot(1.0, 1.02), "B": _slot(1.0, 1.50, lth=False)}     # B is bot-managed → never reported
    a = _alerts(st)
    asyncio.run(a._poll_holds())
    assert len(st.sent) == 1 and "HOLD LUNA" in st.sent[0][1] and "+2.0 %" in st.sent[0][1] and abs(a._hold_last_sent["L"] - 2.0) < 1e-9
    st.active_trades["L"]["_last_price_sol"] = 1.06                                   # +6 % → only +4 pts since the baseline
    asyncio.run(a._poll_holds())
    assert len(st.sent) == 1
    st.active_trades["L"]["_last_price_sol"] = 1.075                                  # +7.5 % → +5.5 pts → update
    asyncio.run(a._poll_holds())
    assert len(st.sent) == 2 and "+5.5 pts since last update" in st.sent[1][1] and "net if sold now" in st.sent[1][1]
    st.active_trades["L"]["_last_price_sol"] = 1.02                                   # back down 5.5 pts → update
    asyncio.run(a._poll_holds())
    assert len(st.sent) == 3 and "📉" not in st.sent[2][1]                            # still green overall → 📈
    st.config.enabled = True                                                          # Start → baselines cleared, nothing sent while ON
    asyncio.run(a._poll_holds())
    assert a._hold_last_sent == {} and len(st.sent) == 3
    st.config.alert_hold_updates_enabled = False
    st.config.enabled = False
    asyncio.run(a._poll_holds())
    assert len(st.sent) == 3                                                          # switch off → silent


def test_hold_that_closed_is_forgotten():
    st = _State()
    st.active_trades = {"L": _slot(1.0, 0.9)}
    a = _alerts(st)
    asyncio.run(a._poll_holds())
    assert "📉" in st.sent[0][1] and "-10.0 %" in st.sent[0][1]
    st.active_trades.clear()
    asyncio.run(a._poll_holds())
    assert a._hold_last_sent == {}
