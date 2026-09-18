"""Manual holds (Buy Now / ladder pin): no clock, no momentum kill, no pattern rip-cord, outside max positions."""
import asyncio
import time

import pytest

import exits
from bot import BotState
from models import BotConfig
from rh_paper import RHPaperTrader


class _DB:
    pass


def _state() -> BotState:
    st = BotState(_DB())
    st.config = BotConfig()
    st.config.max_concurrent_positions = 2
    return st


def _slot(action="momentum_new", **extra):
    return {"trade": {"mint": f"m-{action}-{len(extra)}", "classifier_action": action, "entry_price_sol": 1.0, "book": "scalp", **extra}}


def test_is_manual_hold_flags():
    assert exits.is_manual_hold({"classifier_action": "manual"})
    assert exits.is_manual_hold({"classifier_action": "rh_pons_manual"})
    assert exits.is_manual_hold({"manual": True})
    assert not exits.is_manual_hold({"classifier_action": "momentum_new"})
    assert not exits.is_manual_hold(None)


def test_scalp_clock_skipped_for_manual():
    cfg = BotConfig()
    cfg.book_exits = {"scalp": {**(cfg.book_exits or {}).get("scalp", {}), "hold_max_seconds": 10, "stop_loss_pct": 12, "target_r": 0, "trailing_stop_pct": 0}}
    fire = lambda *a, **k: False
    auto = _slot("momentum_new")
    d = exits.decide_scalp(cfg, auto, 1.0, 1.01, elapsed=999, sl_fire=fire, ts_fire=fire)
    assert d.kind == "exit" and "clock" in d.reason
    man = _slot("manual")
    d = exits.decide_scalp(cfg, man, 1.0, 1.01, elapsed=999, sl_fire=fire, ts_fire=fire)
    assert d.kind is None


def test_manual_still_stops_out():
    cfg = BotConfig()
    cfg.book_exits = {"scalp": {**(cfg.book_exits or {}).get("scalp", {}), "hold_max_seconds": 10, "stop_loss_pct": 12}}
    man = _slot("manual")
    d = exits.decide_scalp(cfg, man, -15.0, 0.85, elapsed=1, sl_fire=lambda b, s: b, ts_fire=lambda b: b)
    assert d.kind == "exit" and "stop-loss" in d.reason


def test_dead_tape_skipped_for_manual():
    cfg = BotConfig()
    cfg.book_exits = {"scalp": {**(cfg.book_exits or {}).get("scalp", {}), "no_new_buyers_s": 5}}
    now = time.time()
    bucket = {"last_new_buyer_ts": now - 100, "last_inflow_ts": now - 100}
    assert exits.search_dead_tape(cfg, "scalp", bucket, now, entry_ts=now - 100) is not None
    assert exits.search_dead_tape(cfg, "scalp", bucket, now, entry_ts=now - 100, trade={"classifier_action": "manual"}) is None


def test_counted_open_excludes_manual_and_pinned():
    st = _state()
    st.active_trades = {
        "a": {"trade": {"mint": "a", "classifier_action": "momentum_new", "book": "scalp"}},
        "b": {"trade": {"mint": "b", "classifier_action": "manual", "book": "scalp"}},
        "c": {"trade": {"mint": "c", "classifier_action": "scanner_momentum", "book": "hunt"}},
    }
    st.tracking["c"] = {"pinned": True}
    assert len(st.active_trades) == 3
    assert st.counted_open() == 1
    assert not st._is_snipe(st.active_trades["b"])


def test_hunt_open_excludes_manual():
    st = _state()
    st.active_trades = {
        "a": {"trade": {"mint": "a", "classifier_action": "greylist_snipe", "book": "hunt"}},
        "b": {"trade": {"mint": "b", "classifier_action": "manual", "book": "hunt"}},
    }
    assert st._hunt_open() == 1


def test_rh_counted_open_and_manual_gates():
    st = _state()
    rh = RHPaperTrader(st)
    rh.positions = {
        "0x1": {"trade": {"mint": "0x1", "classifier_action": "rh_pons_paper"}},
        "0x2": {"trade": {"mint": "0x2", "classifier_action": "rh_pons_manual"}},
    }
    assert rh.counted_open() == 1
    st.config.rh_max_positions = 1
    b = {"graduated": False}
    assert rh._gates("0x3", b, time.time()) == "max-positions"
    rh.positions.pop("0x1")
    b["manual"] = True
    assert rh._gates("0x3", b, time.time()) == "ladder-only"   # past max-positions with the manual hold still open
