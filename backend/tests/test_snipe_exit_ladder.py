"""
Tests for the pattern-based snipe exit ladder.

For greylist snipes the user wants:
  - NO entry-loss SL
  - NO max-hold timeout
  - NO momentum trailing stop
  - YES exit when curve fill approaches creator's typical rug curve %
  - YES exit when current MC approaches creator's typical peak
  - YES rip-cord on catastrophic drawdown FROM OBSERVED PEAK (NOT entry)
  - YES pattern-suggested TP (lock in profit on parabolic moves)
"""
from __future__ import annotations
import os
import sys
import time

os.environ.setdefault("HELIUS_RPC_URL", "https://x")
os.environ.setdefault("HELIUS_WSS_URL", "wss://x")
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")
os.environ.setdefault("CORS_ORIGINS", "http://localhost")
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except Exception:
    pass

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# Build a stub with just the config + tracking needed by the helper.
class _Stub:
    def __init__(self, **cfg_overrides):
        class _Cfg:
            greylist_snipe_pattern_exits = True
            greylist_snipe_peak_mc_proximity_pct = 85.0
            greylist_snipe_curve_buffer_pct = 5.0
            greylist_snipe_ripcord_drawdown_pct = 60.0
            greylist_snipe_ripcord_grace_seconds = 8
            # Defaults so the new gates (profit ripcord, velocity decay)
            # are inactive in these legacy tests — they target the older
            # pattern/curve/peak-MC paths only.
            greylist_snipe_profit_ripcord_pct = 0.0  # disabled
            greylist_snipe_velocity_exits_enabled = False
            greylist_snipe_velocity_window_s = 15
            greylist_snipe_velocity_baseline_s = 60
            greylist_snipe_velocity_min_buys = 8
            greylist_snipe_sol_vel_drop_pct = 70.0
            greylist_snipe_holder_vel_drop_pct = 70.0
        for k, v in cfg_overrides.items():
            setattr(_Cfg, k, v)
        self.config = _Cfg()
        self.tracking = {}


def _bind(stub, name):
    from bot import BotState
    return getattr(BotState, name).__get__(stub, type(stub))


def _slot(*, classifier_action="greylist_snipe", entry_price=0.0001,
          peak_price=None, snipe_ctx=None):
    slot = {
        "trade": {
            "mint": "MintX",
            "classifier_action": classifier_action,
            "entry_price_sol": entry_price,
        },
    }
    if peak_price is not None:
        slot["peak_price_sol"] = peak_price
    if snipe_ctx is not None:
        slot["snipe_pattern_ctx"] = snipe_ctx
    return slot


# ===== _is_snipe ==========================================================

def test_is_snipe_true_for_greylist_action():
    stub = _Stub()
    assert _bind(stub, "_is_snipe")(_slot(classifier_action="greylist_snipe")) is True


def test_is_snipe_false_for_momentum():
    stub = _Stub()
    assert _bind(stub, "_is_snipe")(_slot(classifier_action="momentum_new")) is False


def test_is_snipe_false_when_pattern_exits_disabled():
    stub = _Stub(greylist_snipe_pattern_exits=False)
    assert _bind(stub, "_is_snipe")(_slot()) is False


# ===== Peak-MC proximity exit ============================================

def test_peak_mc_proximity_triggers_at_85pct():
    stub = _Stub()
    slot = _slot(snipe_ctx={
        "expected_peak_mc_usd": 10_000,
        "expected_peak_mc_stddev": 1_000,
        "expected_rug_curve_pct": None,
    })
    # Current MC = 8500 → exactly at threshold
    stub.tracking["MintX"] = {"usd_market_cap": 8500, "curve_fill_pct": 0}
    should, reason = _bind(stub, "_check_snipe_pattern_exit")(slot, 0.0001)
    assert should is True
    assert "peak-MC exit" in reason


def test_peak_mc_proximity_doesnt_trigger_below():
    stub = _Stub()
    slot = _slot(snipe_ctx={
        "expected_peak_mc_usd": 10_000,
        "expected_rug_curve_pct": None,
    })
    stub.tracking["MintX"] = {"usd_market_cap": 5000, "curve_fill_pct": 0}
    should, _ = _bind(stub, "_check_snipe_pattern_exit")(slot, 0.0001)
    assert should is False


def test_peak_mc_proximity_custom_threshold():
    stub = _Stub(greylist_snipe_peak_mc_proximity_pct=70.0)
    slot = _slot(snipe_ctx={"expected_peak_mc_usd": 10_000})
    stub.tracking["MintX"] = {"usd_market_cap": 7000, "curve_fill_pct": 0}
    should, _ = _bind(stub, "_check_snipe_pattern_exit")(slot, 0.0001)
    assert should is True


# ===== Curve-fill exit ====================================================

def test_curve_fill_triggers_within_buffer():
    stub = _Stub()
    slot = _slot(snipe_ctx={"expected_rug_curve_pct": 65.0})
    # buffer=5pp → trigger at 60%
    stub.tracking["MintX"] = {"curve_fill_pct": 60.5, "usd_market_cap": 0}
    should, reason = _bind(stub, "_check_snipe_pattern_exit")(slot, 0.0001)
    assert should is True
    assert "curve-fill exit" in reason


def test_curve_fill_doesnt_trigger_below_buffer():
    stub = _Stub()
    slot = _slot(snipe_ctx={"expected_rug_curve_pct": 65.0})
    stub.tracking["MintX"] = {"curve_fill_pct": 50.0, "usd_market_cap": 0}
    should, _ = _bind(stub, "_check_snipe_pattern_exit")(slot, 0.0001)
    assert should is False


def test_curve_fill_zero_doesnt_crash():
    stub = _Stub()
    slot = _slot(snipe_ctx={"expected_rug_curve_pct": 65.0})
    stub.tracking["MintX"] = {"curve_fill_pct": 0.0, "usd_market_cap": 0}
    should, _ = _bind(stub, "_check_snipe_pattern_exit")(slot, 0.0001)
    assert should is False


# ===== Pattern-suggested TP ==============================================

def test_pattern_tp_locks_profit():
    stub = _Stub()
    slot = _slot(entry_price=0.0001, snipe_ctx={})
    slot["trade"]["greylist_pattern_suggested_tp_pct"] = 18.0
    stub.tracking["MintX"] = {"curve_fill_pct": 0, "usd_market_cap": 0}
    # 0.0001 + 20% → 0.00012
    should, reason = _bind(stub, "_check_snipe_pattern_exit")(slot, 0.00012)
    assert should is True
    assert "pattern-TP hit" in reason


def test_pattern_tp_no_trigger_below_threshold():
    stub = _Stub()
    slot = _slot(entry_price=0.0001, snipe_ctx={})
    slot["trade"]["greylist_pattern_suggested_tp_pct"] = 18.0
    stub.tracking["MintX"] = {"curve_fill_pct": 0, "usd_market_cap": 0}
    should, _ = _bind(stub, "_check_snipe_pattern_exit")(slot, 0.000105)  # +5%
    assert should is False


# ===== Rip-cord (drawdown from OBSERVED PEAK) ============================

def test_ripcord_requires_grace_period():
    stub = _Stub(greylist_snipe_ripcord_grace_seconds=8)
    slot = _slot(entry_price=0.0001, peak_price=0.0002, snipe_ctx={})
    stub.tracking["MintX"] = {"curve_fill_pct": 0, "usd_market_cap": 0}
    # Price = 0.00007 → 65% down from peak 0.0002 → above 60% rip threshold
    should, _ = _bind(stub, "_check_snipe_pattern_exit")(slot, 0.00007)
    assert should is False  # first breach starts timer, doesn't fire
    assert "_snipe_ripcord_start" in slot


def test_ripcord_fires_after_grace():
    stub = _Stub(greylist_snipe_ripcord_grace_seconds=1)
    slot = _slot(entry_price=0.0001, peak_price=0.0002, snipe_ctx={})
    stub.tracking["MintX"] = {"curve_fill_pct": 0, "usd_market_cap": 0}
    # Seed timer 2s in the past
    slot["_snipe_ripcord_start"] = time.time() - 2.0
    should, reason = _bind(stub, "_check_snipe_pattern_exit")(slot, 0.00007)
    assert should is True
    assert "rip-cord" in reason


def test_ripcord_timer_clears_on_recovery():
    stub = _Stub()
    slot = _slot(entry_price=0.0001, peak_price=0.0002, snipe_ctx={})
    stub.tracking["MintX"] = {"curve_fill_pct": 0, "usd_market_cap": 0}
    slot["_snipe_ripcord_start"] = time.time() - 2.0
    # Price recovered to 0.00018 (only 10% down from peak → no breach)
    should, _ = _bind(stub, "_check_snipe_pattern_exit")(slot, 0.00018)
    assert should is False
    assert "_snipe_ripcord_start" not in slot


def test_ripcord_drawdown_from_PEAK_not_entry():
    """The user's key requirement: snipe rip-cord is anchored on the
    OBSERVED PEAK, not on entry price. A trade that went +200% then dumped
    60% from the peak (still up +20% from entry) MUST rip-cord — vs the
    standard SL ladder which would see +20% PnL and let it ride."""
    stub = _Stub(greylist_snipe_ripcord_grace_seconds=1)
    slot = _slot(entry_price=0.0001, peak_price=0.0003, snipe_ctx={})  # peak = +200% from entry
    stub.tracking["MintX"] = {"curve_fill_pct": 0, "usd_market_cap": 0}
    slot["_snipe_ripcord_start"] = time.time() - 2.0
    # Current = 0.00012 → +20% from entry BUT -60% from peak → rip-cord must fire
    should, reason = _bind(stub, "_check_snipe_pattern_exit")(slot, 0.00012)
    assert should is True
    assert "drawdown" in reason


# ===== No-context safety ==================================================

def test_no_snipe_context_returns_false():
    """A trade tagged as a snipe but without snipe_pattern_ctx (e.g. due
    to data error) must NOT exit on every tick — the helper should just
    sit there and let the operator manually close."""
    stub = _Stub()
    slot = _slot(snipe_ctx=None)
    stub.tracking["MintX"] = {"curve_fill_pct": 80, "usd_market_cap": 100_000}
    should, _ = _bind(stub, "_check_snipe_pattern_exit")(slot, 0.0001)
    assert should is False


def test_all_gates_silent_returns_false():
    stub = _Stub()
    slot = _slot(entry_price=0.0001, peak_price=0.000105, snipe_ctx={
        "expected_peak_mc_usd": 100_000,
        "expected_rug_curve_pct": 65.0,
    })
    stub.tracking["MintX"] = {"curve_fill_pct": 30, "usd_market_cap": 20_000}
    should, _ = _bind(stub, "_check_snipe_pattern_exit")(slot, 0.000102)
    assert should is False
