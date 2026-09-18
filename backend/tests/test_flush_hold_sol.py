"""Solana scalp/hunt flush hold — twin of the RH one: a one-wallet dump with buyers still arriving holds the stop
(bounded by flush_hold_s + extra-drop floor); broad selling sells at once."""
import os
import sys
import time
from collections import deque
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_flush_sol")

import bot as bot_mod
from models import BotConfig

LAMPORTS = 1_000_000_000


def _state(**cfg):
    st = bot_mod.BotState.__new__(bot_mod.BotState)
    base = dict(flush_hold_enabled=True, flush_hold_scope="all", flush_hold_s=10, flush_top_share=0.7,
                flush_max_sellers=2, flush_min_buyers=1, flush_extra_drop_pct=5.0, flush_window_s=30)
    st.config = BotConfig(**{**base, **cfg})
    st.stats = {}
    st.tracking = {}
    return st


def _slot(peak=1e-7, trough=0.85e-7):
    now = time.time()
    return {"trade": {"symbol": "T", "mint": "M"}, "peak_price_sol": peak, "peak_ts": now - 10, "trough_price_sol": trough}


def _bucket(sells, buys):
    now = time.time()
    return {"buyers": set(), "buy_events": deque([(now - 3, int(q * LAMPORTS), w) for q, w in buys]),
            "sell_events": deque([(now - 5, q, w) for q, w in sells])}


def test_single_seller_flush_holds_then_expires():
    st = _state()
    st.tracking["M"] = _bucket(sells=[(2.0, "whale")], buys=[(0.1, "a"), (0.2, "b")])
    slot = _slot()
    assert st._flush_holds_sol("M", slot, "sl", -13.0, 0.86e-7) is True
    assert slot["_dip_forensics"]["flush"] is True and st.stats["flush_holds"] == 1
    slot["_flush_hold_since"] -= 11                               # hold budget spent
    assert st._flush_holds_sol("M", slot, "sl", -13.0, 0.86e-7) is False


def test_hold_breaks_when_price_falls_through_the_floor():
    st = _state()
    st.tracking["M"] = _bucket(sells=[(2.0, "whale")], buys=[(0.1, "a")])
    slot = _slot(trough=0.85e-7)
    assert st._flush_holds_sol("M", slot, "sl", -13.0, 0.86e-7) is True
    assert st._flush_holds_sol("M", slot, "sl", -20.0, 0.80e-7) is False   # -5.9% below the trough → distribution
    assert slot["_dip_forensics"].get("hold_broke_floor") is True


def test_many_sellers_is_distribution_not_a_flush():
    st = _state()
    st.tracking["M"] = _bucket(sells=[(0.3, f"w{i}") for i in range(6)], buys=[(0.1, "a")])
    slot = _slot()
    assert st._flush_holds_sol("M", slot, "sl", -13.0, 0.86e-7) is False
    assert slot["_dip_forensics"]["flush"] is False and slot["_dip_forensics"]["n_sellers"] == 6


def test_scope_hot_reentry_skips_plain_scalps():
    st = _state(flush_hold_scope="hot_reentry")
    st.tracking["M"] = _bucket(sells=[(2.0, "whale")], buys=[(0.1, "a")])
    assert st._flush_holds_sol("M", _slot(), "sl", -13.0, 0.86e-7) is False
    st.tracking["M"]["hot"] = True
    assert st._flush_holds_sol("M", _slot(), "sl", -13.0, 0.86e-7) is True


def test_tp_never_held_and_disabled_switch():
    st = _state()
    st.tracking["M"] = _bucket(sells=[(2.0, "whale")], buys=[(0.1, "a")])
    assert st._flush_holds_sol("M", _slot(), "tp", 40.0, 1.4e-7) is False
    st.config.flush_hold_enabled = False
    assert st._flush_holds_sol("M", _slot(), "sl", -13.0, 0.86e-7) is False
