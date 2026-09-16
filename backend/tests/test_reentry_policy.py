"""Universal re-entry policy: the existing reentry_* controls govern every second buy of a token, on both chains."""
import os
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")

from reentry_policy import ReentryLedger  # noqa: E402


def _cfg(**kw):
    base = dict(reentry_enabled=True, reentry_max_attempts=2, reentry_window_seconds=300, reentry_size_multiplier=0.5,
                reentry_min_wait_s=20, hot_token_pnl_pct=25.0, hot_reentry_size_mult=1.5)
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_fresh_token_has_no_policy():
    assert ReentryLedger().check("0x1", _cfg(), time.time()) == (None, None)


def test_min_wait_then_sized_then_capped():
    led, cfg, t0 = ReentryLedger(), _cfg(), 1000.0
    led.record_exit("0x1", -8.0, cfg, was_sl=True, now=t0)
    assert led.check("0x1", cfg, t0 + 5) == ("reentry-wait", None)
    assert led.check("0x1", cfg, t0 + 20) == (None, 0.5)             # gates pass → half size
    led.record_attempt("0x1")
    led.record_exit("0x1", 3.0, cfg, now=t0 + 60)                     # 2nd trade closed: attempts carry
    assert led.attempts("0x1") == 1
    assert led.check("0x1", cfg, t0 + 90) == (None, 0.5)
    led.record_attempt("0x1")
    led.record_exit("0x1", -2.0, cfg, now=t0 + 120)
    assert led.check("0x1", cfg, t0 + 150) == ("reentry-max", None)  # 2 of 2 used
    assert led.check("0x1", cfg, t0 + 120 + 301) == (None, None)     # window over → fresh again, memory dropped
    assert "0x1" not in led.exits


def test_disabled_blocks_inside_window_only():
    led, cfg, t0 = ReentryLedger(), _cfg(reentry_enabled=False), 1000.0
    led.record_exit("0x1", 10.0, cfg, now=t0)
    assert led.check("0x1", cfg, t0 + 30) == ("reentry-off", None)
    assert led.check("0x1", cfg, t0 + 400) == (None, None)


def test_hot_token_gets_sol_watcher_bonuses():
    led, cfg, t0 = ReentryLedger(), _cfg(), 1000.0
    led.record_exit("0x1", 40.0, cfg, now=t0)
    assert led.check("0x1", cfg, t0 + 20) == (None, 0.75)            # 0.5 × 1.5 hot mult
    for _ in range(3):
        led.record_attempt("0x1")
    assert led.check("0x1", cfg, t0 + 30) == (None, 0.75)            # cap 2 + 2 hot
    led.record_attempt("0x1")
    assert led.check("0x1", cfg, t0 + 30) == ("reentry-max", None)
    assert led.check("0x1", cfg, t0 + 500) == ("reentry-max", None)  # hot window is doubled (600 s)


def test_attempts_reset_when_exit_lands_outside_window():
    led, cfg, t0 = ReentryLedger(), _cfg(), 1000.0
    led.record_exit("0x1", 1.0, cfg, now=t0)
    led.record_attempt("0x1")
    led.record_exit("0x1", 1.0, cfg, now=t0 + 1000)
    assert led.attempts("0x1") == 0
