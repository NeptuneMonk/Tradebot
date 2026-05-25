"""Strategy Doctor smoke tests — verifies rules fire correctly given
synthetic trade data, and that suggestions de-dupe properly."""
import os
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

from strategy_doctor import StrategyDoctor, _hash_signature


class _StubDB:
    """Minimal Mongo stub for unit testing the doctor's rule logic."""
    def __init__(self): pass


def _make_doctor():
    return StrategyDoctor(db=_StubDB())


def _trades_with(*, sizes_wr_pcts):
    """Build synthetic trade docs: list of (entry_usd, pnl_pct) tuples."""
    out = []
    base_t = datetime.now(timezone.utc) - timedelta(minutes=10)
    for i, (sz, p) in enumerate(sizes_wr_pcts):
        out.append({
            "id": f"t{i}",
            "entry_usd": sz,
            "entry_time": (base_t + timedelta(seconds=i)).isoformat(),
            "exit_time": (base_t + timedelta(seconds=i + 10)).isoformat(),
            "pnl_pct": p,
            "exit_reason": "take-profit hit" if p > 0 else "stop-loss hit",
            "classifier_action": "momentum_new",
            "protocol": "pumpfun",
        })
    return out


def test_sizing_rule_fires_when_small_beats_big():
    d = _make_doctor()
    # 20 small trades winning, 20 big trades losing
    trades = _trades_with(sizes_wr_pcts=(
        [(0.5, 5)] * 16 + [(0.5, -2)] * 4 +    # small: 16/20 = 80% WR
        [(2.0, -10)] * 18 + [(2.0, 3)] * 2     # big: 2/20 = 10% WR
    ))
    out = d._rule_sizing_advantage(trades, {"max_trade_usd": 2.0})
    assert len(out) == 1
    assert out[0]["category"] == "sizing"
    assert out[0]["actions"]["max_trade_usd"] == 0.9


def test_sizing_rule_silent_when_no_advantage():
    d = _make_doctor()
    trades = _trades_with(sizes_wr_pcts=(
        [(0.5, 2)] * 10 + [(0.5, -2)] * 10 +
        [(2.0, 3)] * 10 + [(2.0, -3)] * 10
    ))
    out = d._rule_sizing_advantage(trades, {"max_trade_usd": 1.5})
    assert out == []


def test_sl_tightness_rule_fires_on_deep_sl():
    d = _make_doctor()
    # 15 SL trades all hitting -25% (vs configured -10%) → 2.5x worse
    trades = [{
        "id": f"sl{i}", "pnl_pct": -25, "exit_reason": "stop-loss hit (-25%)",
        "entry_time": "2026-05-25T00:00:00+00:00",
        "exit_time": "2026-05-25T00:00:10+00:00",
    } for i in range(15)]
    out = d._rule_stop_loss_tightness(trades, {"stop_loss_pct": 10})
    assert len(out) == 1
    assert out[0]["category"] == "sl"


def test_tp_frequency_rule_fires_when_tp_rare():
    d = _make_doctor()
    # 100 trades, only 2 take-profit hits → 2% rate, way below 8% threshold
    trades = []
    for i in range(100):
        is_tp = i < 2
        trades.append({
            "id": f"x{i}",
            "pnl_pct": 12 if is_tp else -3,
            "exit_reason": "take-profit hit (12%)" if is_tp else "timeout",
            "entry_time": "2026-05-25T00:00:00+00:00",
            "exit_time": "2026-05-25T00:00:10+00:00",
        })
    out = d._rule_take_profit_frequency(trades, {"take_profit_pct": 15})
    assert len(out) == 1
    assert out[0]["category"] == "tp"
    assert out[0]["actions"]["take_profit_pct"] < 15


def test_signature_stable():
    """Same category + same action keys should produce the same signature
    across runs — so duplicates aren't re-inserted."""
    a = _hash_signature("sizing", ["max_trade_usd"])
    b = _hash_signature("sizing", ["max_trade_usd"])
    assert a == b
    # Different action keys → different signature
    c = _hash_signature("sizing", ["min_trade_usd"])
    assert a != c
