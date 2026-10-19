"""Velocity / rolling-growth baselines use the value as of `window` ago, not the first sample inside the window
(which, at 1/30 s sampling, was often the latest print itself → a rising token read a flat 0 %)."""
import os
import sys
import time
from collections import deque

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
from models import BotConfig  # noqa: E402
from scanner import MomentumScanner, _mc_velocity, velocity_pct_strict  # noqa: E402

NOW = time.time()


def _ramp(step_s, n, start=1.0, pct_per_step=1.0):
    # n samples ending at NOW, each `pct_per_step` % above the previous
    return deque([(NOW - (n - 1 - i) * step_s, start * (1 + pct_per_step / 100) ** i) for i in range(n)])


def test_strict_velocity_at_sparse_sampling_is_not_zero():
    s = _ramp(30, 12, pct_per_step=2.0)                   # a print every 30 s, +2 % each → +2 %/30 s
    v = velocity_pct_strict(s, NOW, 30)
    assert v is not None and 1.9 < v < 2.1                 # old code: baseline = latest sample → 0.0


def test_strict_velocity_still_none_without_history():
    assert velocity_pct_strict(_ramp(5, 3), NOW, 30) is None
    assert velocity_pct_strict(deque([(NOW, 1.0)]), NOW, 30) is None


def test_mc_velocity_uses_value_five_minutes_ago():
    s = _ramp(60, 12, pct_per_step=5.0)                   # +5 %/min → +27.6 % over 5 min
    assert 27 < _mc_velocity(s, NOW, 300) < 28.5
    # partial window: only 2 min of history → measured over what we have
    assert 10 < _mc_velocity(_ramp(60, 3, pct_per_step=5.0), NOW, 300) < 10.5


def test_new_band_mc_velocity_prefers_tape_price_samples():
    cfg = BotConfig(scanner_min_mc_velocity_5m_pct_new=3)
    b = {"protocol": "pumpfun", "curve_fill_pct": 80.0, "price_samples": _ramp(30, 20, pct_per_step=1.0),
         "mc_samples": deque([(NOW - 600, 30000.0), (NOW - 540, 30000.0)])}   # stale API MC says flat
    m = {}
    assert MomentumScanner._new_band_shape_gate(cfg, b, m, NOW) is None
    assert m["mc_velocity_5m_pct"] > 9                     # 10 steps × 1 %


def test_rolling_growth_baseline_is_price_lookback_ago():
    cfg = BotConfig(scanner_growth_lookback_s=300)
    sc = MomentumScanner.__new__(MomentumScanner)
    sc.state = type("S", (), {"config": cfg})()
    b = {"start": NOW - 1800, "last_price_sol": 2.0, "first_seen_price_sol": 0.5, "protocol": "pumpfun",
         "price_samples": _ramp(30, 60, start=1.0, pct_per_step=1.0), "buy_events": (), "buyers": set()}
    b["last_price_sol"] = b["price_samples"][-1][1]
    m = sc.score(b, None, NOW)
    # 10 samples back (300 s) → (1.01^10 − 1) ≈ 10.5 %
    assert 10 < m["growth_pct_rolling"] < 11
