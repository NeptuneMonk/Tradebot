"""Lite-mode watchdog: trips on RAM / loop lag, clears with hysteresis, honours operator override and the config switch."""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

import lite_mode
from lite_mode import LiteMode


def _cfg(**kw):
    return SimpleNamespace(**{"lite_mode_enabled": True, "lite_mode_rss_mb": 400.0, "lite_mode_lag_ms": 500.0, **kw})


def test_trips_on_ram_and_clears_after_calm_period(monkeypatch):
    rss = {"v": 300.0}
    monkeypatch.setattr(lite_mode, "current_rss_mb", lambda: rss["v"])
    t = {"now": 1000.0}
    monkeypatch.setattr(lite_mode.time, "time", lambda: t["now"])
    lm = LiteMode()
    assert lm.evaluate(_cfg(), 10.0) is False
    rss["v"] = 450.0
    assert lm.evaluate(_cfg(), 10.0) is True and "RAM 450MB" in lm.reason and lm.trips == 1
    rss["v"] = 390.0                                   # under the line but not under 85% → stays lite
    assert lm.evaluate(_cfg(), 10.0) is True
    rss["v"] = 300.0
    assert lm.evaluate(_cfg(), 10.0) is True           # calm, but hysteresis window just started
    t["now"] += 61.0
    assert lm.evaluate(_cfg(), 10.0) is False and lm.reason is None


def test_trips_on_loop_lag(monkeypatch):
    monkeypatch.setattr(lite_mode, "current_rss_mb", lambda: 100.0)
    lm = LiteMode()
    assert lm.evaluate(_cfg(), 800.0) is True and "loop lag 800ms" in lm.reason


def test_disabled_switch_and_operator_override(monkeypatch):
    monkeypatch.setattr(lite_mode, "current_rss_mb", lambda: 900.0)
    lm = LiteMode()
    assert lm.evaluate(_cfg(lite_mode_enabled=False), 900.0) is False
    lm.forced = True
    assert lm.evaluate(_cfg(lite_mode_enabled=False), 0.0) is True and lm.reason == "operator override"
    lm.forced = False
    assert lm.evaluate(_cfg(), 900.0) is False
    snap = lm.snapshot()
    assert set(snap) >= {"active", "reason", "rss_mb", "lag_ms", "trips", "forced"}
