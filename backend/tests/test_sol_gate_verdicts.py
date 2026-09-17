"""Pump.fun / PumpSwap rows carry the scanner's per-token gate verdict (like the RH badge)."""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")

from scanner import _Verdict  # noqa: E402


def test_verdict_stamps_reason_and_detail_and_returns_reason():
    b = {}
    v = _Verdict(b)
    assert v("growth", "+3.0% rolling < 45%") == "growth"
    assert b["gate_reason"] == "growth" and b["gate_detail"] == "+3.0% rolling < 45%"
    assert v("pass") == "pass" and b["gate_reason"] == "pass" and b["gate_detail"] is None


def test_scanner_loop_paths_all_stamp_a_verdict():
    import inspect
    import scanner
    src = inspect.getsource(scanner.MomentumScanner.loop)
    pre = src.split("if not scored:")[0].split("tally = st.prerank_skip")[1]
    # every pre-rank `tally(band, …)` call now goes through the verdict stamp
    assert 'tally(band, "' not in pre and pre.count("tally(band, verdict(") >= 8
    # and every silent `continue` in the on-chain re-check has a verdict before it
    post = src.split("for mint, b, _cached_m, _cached_score, band in top:")[1].split("action = ")[0]
    for reason in ("no-pool", "pool-state", "curve-complete", "liquidity", "growth", "mc", "mc-velocity", "inflow", "new-buyers"):
        assert f'"{reason}"' in post


def test_skip_event_stamps_tracked_bucket():
    import asyncio
    from bot import BotState
    st = BotState.__new__(BotState)
    st.tracking = {"M": {}}
    st._ledger_sol = lambda *a, **k: None

    async def run():
        import bot
        bot.hub = types.SimpleNamespace(broadcast=lambda *a, **k: asyncio.sleep(0))
        await st._skip_event({"mint": "M", "band": "new", "reason": "doctor-breaker:scalp", "details": ["scalp benched 12m"]})
    asyncio.run(run())
    assert st.tracking["M"]["gate_reason"] == "doctor-breaker:scalp" and st.tracking["M"]["gate_detail"] == "scalp benched 12m"
    assert st._skip_counts["new:doctor-breaker:scalp"] == 1
