"""Bot OFF while manual / LTH holds stay attached: graceful stop finalises past holds, hard stop keeps them."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot import BotState  # noqa: E402
from models import BotConfig  # noqa: E402


class _Cfg:
    def __init__(self):
        self.saved = []

    async def update_one(self, q, u, upsert=False):
        self.saved.append(u["$set"])


def _state():
    st = BotState.__new__(BotState)
    st.config = BotConfig(enabled=True)
    st.stopping_gracefully = False
    st._graceful_stop_task = None
    st.db = type("DB", (), {"bot_config": _Cfg()})()
    st.exits = []

    async def _exit(mint, reason="", **kw):
        st.exits.append((mint, reason))
        st.active_trades.pop(mint, None)
    st._exit = _exit
    st._is_manual_hold = lambda slot: bool(slot["trade"].get("long_term_hold") or slot["trade"].get("classifier_action") == "manual")
    st.active_trades = {
        "BOT1": {"trade": {"id": "a", "classifier_action": "scanner_momentum"}},
        "LTH1": {"trade": {"id": "b", "classifier_action": "manual", "long_term_hold": True}},
        "LTH2": {"trade": {"id": "c", "classifier_action": "smart_wallet", "long_term_hold": True}},
    }
    return st


def test_counts_split_riding_from_holds():
    st = _state()
    assert st.manual_hold_count() == 2 and st.riding_position_count() == 1


def test_graceful_stop_finalises_once_only_holds_remain():
    st = _state()

    async def go():
        await st.begin_graceful_stop()
        assert st.stopping_gracefully and st.config.enabled            # BOT1 still riding
        st.active_trades.pop("BOT1")                                   # it reaches its own exit
        await asyncio.sleep(2.3)                                       # finaliser tick
        assert not st.config.enabled and not st.stopping_gracefully
        assert set(st.active_trades) == {"LTH1", "LTH2"}               # holds untouched, still attached
        st._graceful_stop_task.cancel()
    asyncio.run(go())
    assert {"enabled": False} in st.db.bot_config.saved


def test_graceful_stop_with_only_holds_is_immediate():
    st = _state()
    st.active_trades.pop("BOT1")
    asyncio.run(st.begin_graceful_stop())
    assert not st.config.enabled and not st.stopping_gracefully and len(st.active_trades) == 2
    if st._graceful_stop_task:
        st._graceful_stop_task.cancel()


def test_hard_stop_keeps_holds_unless_asked():
    st = _state()
    asyncio.run(st.hard_stop())
    assert [m for m, _ in st.exits] == ["BOT1"] and set(st.active_trades) == {"LTH1", "LTH2"} and not st.config.enabled
    st2 = _state()
    asyncio.run(st2.hard_stop(include_holds=True))
    assert sorted(m for m, _ in st2.exits) == ["BOT1", "LTH1", "LTH2"] and st2.active_trades == {}
