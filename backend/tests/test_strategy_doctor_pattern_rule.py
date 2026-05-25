"""
Tests for the Phase 2.8 Strategy Doctor rule `_rule_pattern_tp_calibration`.
Verifies the rule fires correctly on real trade-history shapes.
"""
import os
os.environ.setdefault("HELIUS_RPC_URL", "https://x")
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")

from strategy_doctor import StrategyDoctor  # noqa: E402


def _doc():
    return StrategyDoctor(db=None)


def _trade(pat, peak, pnl, exit_reason="trailing-stop"):
    return {
        "greylist_pattern_at_entry": pat,
        "peak_pct_pre_rug": peak,
        "pnl_pct": pnl,
        "exit_reason": exit_reason,
    }


def test_pattern_tp_calibration_no_suggestion_when_too_few_trades():
    """Need ≥6 closed pattern trades per bucket — below that, stay quiet."""
    d = _doc()
    trades = [_trade("slow_rug_tradeable", 25, 17) for _ in range(5)]
    out = d._rule_pattern_tp_calibration(trades, {"pattern_tp_buffer_pct": 2.0})
    assert out == []


def test_pattern_tp_calibration_no_suggestion_when_pnl_tracks_peak():
    """Winners cleanly exit near peak → buffer is right, stay quiet."""
    d = _doc()
    trades = [_trade("slow_rug_tradeable", 19, 18) for _ in range(8)]
    out = d._rule_pattern_tp_calibration(trades, {"pattern_tp_buffer_pct": 2.0})
    assert out == []


def test_pattern_tp_calibration_suggests_tighter_buffer_when_winners_run_past_tp():
    """Mean peak +25, mean pnl +18 → gap 7pp → tighten buffer."""
    d = _doc()
    trades = (
        [_trade("slow_rug_tradeable", 26, 18) for _ in range(6)]
        + [_trade("slow_rug_tradeable", 24, 18) for _ in range(6)]
    )
    out = d._rule_pattern_tp_calibration(trades, {"pattern_tp_buffer_pct": 2.0})
    assert len(out) == 1
    s = out[0]
    assert s["category"] == "tp"
    assert "tighten buffer" in s["title"].lower()
    assert s["actions"]["pattern_tp_buffer_pct"] == 1.0
    assert s["metrics"]["gap_pp"] >= 4.0
    assert s["confidence"] == "high"  # n>=12


def test_pattern_tp_calibration_suggests_looser_buffer_when_sl_dominates():
    """SL hit ≥35% AND mean peak close to mean pnl → loosen buffer."""
    d = _doc()
    # 8 SL hits + 4 small winners. Peak ≈ 14, pnl ≈ 13.5 (gap < 1).
    trades = (
        [_trade("predictable_dump_tradeable", 14, -12, "stop-loss hit") for _ in range(8)]
        + [_trade("predictable_dump_tradeable", 14, 13, "trailing-stop") for _ in range(4)]
    )
    out = d._rule_pattern_tp_calibration(trades, {"pattern_tp_buffer_pct": 2.0})
    assert len(out) == 1
    s = out[0]
    assert s["category"] == "tp"
    assert "loosen buffer" in s["title"].lower()
    assert s["actions"]["pattern_tp_buffer_pct"] == 3.0
    assert s["metrics"]["sl_rate_pct"] >= 35.0


def test_pattern_tp_calibration_ignores_unclassified_trades():
    """Trades w/o pattern_at_entry should NOT enter calibration buckets."""
    d = _doc()
    trades = [_trade(None, 30, 5) for _ in range(20)]  # unclassified
    out = d._rule_pattern_tp_calibration(trades, {"pattern_tp_buffer_pct": 2.0})
    assert out == []


def test_pattern_tp_calibration_per_pattern_independence():
    """Slow_rug suggesting tighten + dump suggesting loosen → 2 separate suggestions."""
    d = _doc()
    trades = (
        # slow_rug: winners run past TP (gap ≈ 7)
        [_trade("slow_rug_tradeable", 26, 18) for _ in range(8)]
        # dump: SL-dominated
        + [_trade("predictable_dump_tradeable", 14, -12, "stop-loss hit") for _ in range(8)]
        + [_trade("predictable_dump_tradeable", 14, 13) for _ in range(4)]
    )
    out = d._rule_pattern_tp_calibration(trades, {"pattern_tp_buffer_pct": 2.0})
    titles = [s["title"].lower() for s in out]
    assert len(out) == 2
    assert any("tighten" in t for t in titles)
    assert any("loosen" in t for t in titles)


def test_pattern_tp_calibration_does_not_lower_below_floor():
    """Buffer < 1.5 should not get further tightened."""
    d = _doc()
    trades = [_trade("slow_rug_tradeable", 26, 18) for _ in range(12)]
    out = d._rule_pattern_tp_calibration(trades, {"pattern_tp_buffer_pct": 1.0})
    assert out == []  # gap large but cur_buffer below the floor


def test_pattern_tp_calibration_does_not_raise_above_ceiling():
    """Buffer > 3.5 should not be loosened further."""
    d = _doc()
    trades = (
        [_trade("predictable_dump_tradeable", 14, -12, "stop-loss hit") for _ in range(8)]
        + [_trade("predictable_dump_tradeable", 14, 13) for _ in range(4)]
    )
    out = d._rule_pattern_tp_calibration(trades, {"pattern_tp_buffer_pct": 4.0})
    assert out == []  # sl_rate high but cur_buffer above the ceiling
