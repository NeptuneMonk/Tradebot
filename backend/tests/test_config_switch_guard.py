"""Operator switches (feeds, RH arming, live, enabled) may only be written by the config PUT — background
`save_config()` callers (bankroll governor, profit sweep, …) must not carry a stale snapshot of them."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_switch_guard")

import bot as bot_mod
from models import BotConfig


class _Col:
    def __init__(self):
        self.sets = []

    async def update_one(self, q, u, upsert=False):
        self.sets.append(u["$set"])


def _state():
    st = bot_mod.BotState.__new__(bot_mod.BotState)
    st.config = BotConfig(rh_paper_enabled=True, rh_feed_enabled=True, helius_tracker_enabled=True, enabled=True, live_trading=False)
    st.db = type("DB", (), {"bot_config": _Col()})()
    return st


def test_background_save_never_touches_switches():
    st = _state()
    asyncio.run(st.save_config())
    doc = st.db.bot_config.sets[-1]
    for k in bot_mod.BotState.SWITCH_KEYS:
        assert k not in doc, k
    assert "max_trade_usd" in doc


def test_put_path_writes_switches():
    st = _state()
    asyncio.run(st.save_config(include_switches=True))
    doc = st.db.bot_config.sets[-1]
    assert doc["rh_paper_enabled"] is True and doc["rh_feed_enabled"] is True and doc["enabled"] is True
