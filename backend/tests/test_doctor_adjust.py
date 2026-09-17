"""Live Doctor: paper-mode breakers adjust instead of benching; peak-hour profile loosens gates / grows size."""
import asyncio
import os
import sys
import time
import types
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")

from live_doctor import LiveDoctor  # noqa: E402


def _doctor(paused_books=()):
    ld = LiveDoctor.__new__(LiveDoctor)
    ld.breakers = {b: {"paused": True, "lift_after": time.time() + 3600, "expires_at": time.time() + 7200} for b in paused_books}
    ld._breakers_failed, ld._evaluated_once = False, True
    ld.hour_profile = {}
    return ld


def test_paper_breaker_adjusts_live_breaker_benches():
    ld = _doctor(paused_books=("scalp",))
    assert ld.book_paused("scalp") is True
    assert ld.book_benched("scalp", live=True) is True and ld.book_benched("scalp", live=False) is False
    assert ld.book_adjust("scalp", live=False) == (0.5, 1.25)          # half size, 25 % tighter gates on paper
    assert ld.book_adjust("scalp", live=True) == (1.0, 1.0)            # live: benched outright, no adjust needed
    assert ld.book_adjust("hunt", live=False) == (1.0, 1.0)            # other books untouched


def test_hour_profile_boosts_only_busy_profitable_hours():
    ld = _doctor()
    now = datetime.now(timezone.utc)
    busy = [(now.hour + k) % 24 for k in range(8)]

    class _Cur:
        def __init__(self, rows): self.rows = rows
        def __aiter__(self): return self
        async def __anext__(self):
            if not self.rows: raise StopAsyncIteration
            return self.rows.pop(0)

    launches = []
    for h in range(24):
        n = 40 if h in busy else 2
        launches += [{"detected_at": now.replace(hour=h, minute=0).isoformat()}] * n
    losing_hour = busy[1]
    trades = [{"entry_time": now.replace(hour=losing_hour).isoformat(), "pnl_pct": -10.0}] * 12
    ld.db = types.SimpleNamespace(launches=types.SimpleNamespace(find=lambda *a, **k: _Cur(list(launches))),
                                  trades=types.SimpleNamespace(find=lambda *a, **k: _Cur(list(trades))))
    prof = asyncio.run(ld.build_hour_profile())
    assert set(prof["peak_hours"]) == set(busy) - {losing_hour}        # busy hour with negative expectancy is not boosted
    assert ld.hour_mult(now.timestamp()) == (1.25, 0.85)               # current hour is a peak hour
    off = now.replace(hour=(now.hour + 12) % 24)
    assert ld.hour_mult(off.timestamp()) == (1.0, 1.0)
    ld.bot_state = None
    assert ld.book_adjust("scalp", live=False) == (1.25, 1.0)     # peak hour boosts size; gates are the tempo's job now
    ld2 = _doctor()
    ld2.db = types.SimpleNamespace(launches=types.SimpleNamespace(find=lambda *a, **k: _Cur([])), trades=types.SimpleNamespace(find=lambda *a, **k: _Cur([])))
    assert asyncio.run(ld2.build_hour_profile())["peak_hours"] == []  # not enough data → no profile


def test_market_tempo_scales_gates_continuously():
    ld = _doctor()
    now = time.time()
    st = types.SimpleNamespace(tracking={}, rh_discovery=types.SimpleNamespace(tracking={}), _launch_rate=lambda: 40.0)
    ld.bot_state = st
    # baseline: 40 buys / 2 min, 40 launches/h
    st.tracking = {f"m{i}": {"buy_events": [(now - 10, 1, "w")] * 4} for i in range(10)}
    ld.update_tempo(now)
    assert ld.tempo_gate_mult("sol") == 1.0                           # first sample defines the baseline → tempo 1
    # hot tape: 160 buys / 2 min and 160 launches/h → tempo clamps at 2 → gates × sqrt(2)
    st.tracking = {f"m{i}": {"buy_events": [(now + 40 - 10, 1, "w")] * 16} for i in range(10)}
    st._launch_rate = lambda: 160.0
    ld.update_tempo(now + 40)
    assert abs(ld.tempo_gate_mult("sol") - 2 ** 0.5) < 0.05
    # dead tape: 10 buys, 10 launches/h → tempo 0.25 → clamped 0.5 → gates × 0.71 (expectations lowered, no pause)
    st.tracking = {"m0": {"buy_events": [(now + 80 - 10, 1, "w")] * 10}}
    st._launch_rate = lambda: 10.0
    ld.update_tempo(now + 80)
    assert abs(ld.tempo_gate_mult("sol") - 0.5 ** 0.5) < 0.05
    snap = ld.tempo_snapshot()["sol"]
    assert snap["tempo"] == 0.5 and snap["buys_2m"] == 10
    assert ld.book_adjust("scalp", live=False)[1] == ld.tempo_gate_mult("sol")   # hour profile no longer touches gates
