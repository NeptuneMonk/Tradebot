"""
Tests for `_rule_greylist_sniper_tuning` — Strategy Doctor's feedback rule
that auto-tunes `greylist_snipe_min_score` based on observed win-rate.
"""
from __future__ import annotations
import os
import sys

# Env stubs MUST be set before any bot/strategy_doctor import
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

from strategy_doctor import StrategyDoctor


def _doc(stub_db=None):
    """Build a StrategyDoctor with a stub DB. We only exercise the pure
    rule function (no async / no Mongo), so most attributes are irrelevant."""
    d = StrategyDoctor.__new__(StrategyDoctor)
    d.db = stub_db
    d.hub = None
    return d


def _trade(pnl_pct: float, action: str = "greylist_snipe") -> dict:
    return {
        "classifier_action": action,
        "pnl_pct": pnl_pct,
    }


# ----- threshold decisions ------------------------------------------------

def test_sniper_rule_tightens_when_winrate_under_35pct():
    d = _doc()
    # 12 trades, 3 winners → 25% WR
    trades = [_trade(15)] * 3 + [_trade(-12)] * 9
    out = d._rule_greylist_sniper_tuning(trades, {
        "greylist_snipe_enabled": True,
        "greylist_snipe_min_score": 45.0,
    })
    assert len(out) == 1
    s = out[0]
    assert s["category"] == "greylist_sniper"
    assert s["actions"] == {"greylist_snipe_min_score": 50.0}
    assert "tighten" in s["title"]
    assert s["metrics"]["direction"] == "tighten"
    assert s["confidence"] == "med"


def test_sniper_rule_loosens_when_winrate_above_55pct():
    d = _doc()
    # 12 trades, 8 winners → 66% WR
    trades = [_trade(20)] * 8 + [_trade(-10)] * 4
    out = d._rule_greylist_sniper_tuning(trades, {
        "greylist_snipe_enabled": True,
        "greylist_snipe_min_score": 45.0,
    })
    assert len(out) == 1
    assert out[0]["actions"] == {"greylist_snipe_min_score": 40.0}
    assert "loosen" in out[0]["title"]


def test_sniper_rule_no_change_in_dead_zone():
    d = _doc()
    # 12 trades, 6 winners → 50% WR (between 35 and 55, no change)
    trades = [_trade(15)] * 6 + [_trade(-12)] * 6
    out = d._rule_greylist_sniper_tuning(trades, {
        "greylist_snipe_enabled": True,
        "greylist_snipe_min_score": 45.0,
    })
    assert out == []


def test_sniper_rule_high_confidence_at_20_trades():
    d = _doc()
    # 25 trades, 5 winners → 20% WR → tighten
    trades = [_trade(15)] * 5 + [_trade(-10)] * 20
    out = d._rule_greylist_sniper_tuning(trades, {
        "greylist_snipe_enabled": True,
        "greylist_snipe_min_score": 45.0,
    })
    assert out[0]["confidence"] == "high"
    assert out[0]["metrics"]["n_sniper_trades"] == 25


# ----- sample-size + filter gates -----------------------------------------

def test_sniper_rule_skips_when_under_10_trades():
    d = _doc()
    trades = [_trade(-15)] * 8  # 8 trades, 0% WR — would tighten, but n<10
    out = d._rule_greylist_sniper_tuning(trades, {
        "greylist_snipe_enabled": True,
        "greylist_snipe_min_score": 45.0,
    })
    assert out == []


def test_sniper_rule_skips_when_disabled():
    d = _doc()
    trades = [_trade(-15)] * 15
    out = d._rule_greylist_sniper_tuning(trades, {
        "greylist_snipe_enabled": False,
        "greylist_snipe_min_score": 45.0,
    })
    assert out == []


def test_sniper_rule_only_counts_sniper_action():
    d = _doc()
    # Mostly momentum_new + only 5 sniper → below the n=10 floor
    trades = ([_trade(-15, action="momentum_new")] * 50
              + [_trade(-15, action="greylist_snipe")] * 5)
    out = d._rule_greylist_sniper_tuning(trades, {
        "greylist_snipe_enabled": True,
        "greylist_snipe_min_score": 45.0,
    })
    assert out == []


def test_sniper_rule_only_counts_sniper_action_passes_when_enough():
    d = _doc()
    # 50 momentum + 12 sniper, all losing → tighten
    trades = ([_trade(20, action="momentum_new")] * 50  # high WR momentum
              + [_trade(-15, action="greylist_snipe")] * 12)
    out = d._rule_greylist_sniper_tuning(trades, {
        "greylist_snipe_enabled": True,
        "greylist_snipe_min_score": 45.0,
    })
    # Sniper-only WR is 0%, should tighten
    assert len(out) == 1
    assert out[0]["actions"] == {"greylist_snipe_min_score": 50.0}


# ----- clamps -------------------------------------------------------------

def test_sniper_rule_clamps_at_upper_bound():
    d = _doc()
    # Bad sniper trades with min_score already at 90 → would push to 95
    # but clamp says max=90 → no suggestion (delta < 0.1).
    trades = [_trade(-15)] * 12
    out = d._rule_greylist_sniper_tuning(trades, {
        "greylist_snipe_enabled": True,
        "greylist_snipe_min_score": 90.0,
    })
    assert out == []


def test_sniper_rule_clamps_at_lower_bound():
    d = _doc()
    # Great sniper trades with min_score already at 25 → would push to 20
    # but clamp says min=25 → no suggestion.
    trades = [_trade(20)] * 10 + [_trade(-5)] * 2
    out = d._rule_greylist_sniper_tuning(trades, {
        "greylist_snipe_enabled": True,
        "greylist_snipe_min_score": 25.0,
    })
    assert out == []


def test_sniper_rule_partial_clamp_still_suggests():
    d = _doc()
    # Bad sniper trades with min_score at 87 → push to 92, clamped to 90.
    # That's still a real change (90 != 87), so suggestion fires.
    trades = [_trade(-15)] * 12
    out = d._rule_greylist_sniper_tuning(trades, {
        "greylist_snipe_enabled": True,
        "greylist_snipe_min_score": 87.0,
    })
    assert len(out) == 1
    assert out[0]["actions"] == {"greylist_snipe_min_score": 90.0}


# ----- rationale + metrics quality ----------------------------------------

def test_sniper_rule_rationale_includes_trade_count_and_wr():
    d = _doc()
    trades = [_trade(15)] * 3 + [_trade(-12)] * 9  # 25% WR, n=12
    out = d._rule_greylist_sniper_tuning(trades, {
        "greylist_snipe_enabled": True,
        "greylist_snipe_min_score": 45.0,
    })
    rationale = out[0]["rationale"]
    assert "12" in rationale  # trade count
    assert "25" in rationale  # WR
    assert "45" in rationale  # current threshold
    assert "50" in rationale  # proposed threshold
    metrics = out[0]["metrics"]
    assert metrics["sniper_wr_pct"] == 25.0
    assert metrics["n_sniper_trades"] == 12
    assert metrics["current_min_score"] == 45.0
    assert metrics["proposed_min_score"] == 50.0
