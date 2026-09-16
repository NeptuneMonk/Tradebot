"""RH feed gate labels: closed tokens read `traded`, post-gate skips (cost-gate, r-size…) surface and stick briefly."""
import os
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")

from rh_paper import RHPaperTrader  # noqa: E402


def _trader():
    cfg = types.SimpleNamespace(rh_max_positions=5, rh_min_age_s=0, rh_max_age_min=60, rh_min_curve_pct=0, rh_max_curve_pct=100,
                                rh_min_mc_usd=0, rh_max_mc_usd=1e12, rh_min_unique_buyers=0, rh_min_growth_pct=0,
                                scanner_recent_inflow_window_s=300, scanner_holder_velocity_window_s=60,
                                rh_min_new_buyers_1m=0, rh_min_inflow_usd=0, rh_max_last_trade_age_s=60)
    disc = types.SimpleNamespace(tracking={}, _dirty=set(), _quote_usd=lambda s: 3000.0)
    state = types.SimpleNamespace(config=cfg, rh_discovery=disc, search_regime_block=lambda: None)
    return RHPaperTrader(state)


def _bucket(now):
    return {"start": now - 10, "graduated": False, "quote_symbol": "ETH", "curve_fill_pct": 10.0, "usd_market_cap": 5000.0,
            "buyers": {"a"}, "last_price_quote": 2.0, "first_price_quote": 1.0, "buy_events": [], "last_trade_ms": int(now * 1000)}


def test_closed_token_is_back_in_play_after_cooldown():
    tr, now = _trader(), time.time()
    tr.entered.add("0xabc")                                   # traded before — no longer a block
    tr.last_exit_ts["0xabc"] = now - 5
    assert tr._gates("0xabc", _bucket(now), now) == "exit-cooldown"
    assert tr._gates("0xabc", _bucket(now), now + 30) is None
    tr.positions["0xdef"] = {}
    assert tr._gates("0xdef", _bucket(now), now) == "already-entered"


def test_post_gate_skip_is_visible_and_sticky():
    tr, now = _trader(), time.time()
    b = _bucket(now)
    tr.state.rh_discovery.tracking["0x1"] = b
    assert tr._gates("0x1", b, now) is None
    tr._block_entry("0x1", b, "cost-gate", now)
    assert b["gate_reason"] == "cost-gate" and "0x1" in tr.state.rh_discovery._dirty
    assert tr._gates("0x1", b, now + 1) == "cost-gate"
    assert tr._gates("0x1", b, now + 31) is None            # 30 s later the momentum gates decide again
    assert tr.stats["skip_reasons"]["curve:cost-gate"] == 1
