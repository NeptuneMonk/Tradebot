"""Graduation is a venue change, not an exit: no -100% from a dead curve."""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot as botmod
from tests.test_profitability_refactor import _bot_stub
from tests.test_runner_book import _patched, _slot, MINT


def test_panic_list_no_longer_contains_graduation():
    st = _bot_stub()
    assert st._is_panic_exit("bonding curve completed (LP about to deploy)") is False
    assert st._is_panic_exit("stop-loss -12%") is True


def test_mark_graduating_keeps_position_active_and_books_nothing():
    st = _patched(_bot_stub())
    s = _slot("scalp")
    st.active_trades[MINT] = s
    asyncio.run(st._mark_graduating(MINT, s, "curve complete, waiting for PumpSwap pool"))
    assert MINT in st.active_trades and s["_curve_complete"] is True and s["trade"]["venue_stage"] == "graduating"
    assert not any(c[0] == "exit" for c in st.calls)
    assert ("db", {"venue_stage": "graduating", "graduating_since": s["trade"]["graduating_since"]}) in st.calls
    assert "pnl_pct" not in s["trade"] and "exit_time" not in s["trade"]
    # past grace, still no pool → stage pool-missing, STILL active, still no PnL
    s["_runner_pool_missing_since"] = time.time() - 120
    asyncio.run(st._mark_graduating(MINT, s, "still waiting"))
    assert s["trade"]["venue_stage"] == "pool-missing" and MINT in st.active_trades and "pnl_pct" not in s["trade"]


def test_hunt_ladder_still_runs_after_pool_migration_price_from_pool():
    """Once the pool is live the same monitor/ladder continues on pumpswap prices (TP later works)."""
    st = _patched(_bot_stub())
    s = _slot("hunt", one_r=20.0, r_usd=3.0, entry_usd=15.0, peak=1.3, legs=0, protocol="pumpswap")
    s["trade"]["venue_stage"] = "pumpswap"
    st.active_trades[MINT] = s
    st.tracking[MINT] = {"buyers": set(), "buy_events": [], "start": time.time() - 60}
    asyncio.run(st._run_ladder(MINT, s, 1.3, 60))            # +30 % on the AMM price → +1R leg fires normally
    assert ("partial", 0.35, "ladder +1R: sell 35% (+30.0%)") in st.calls


def test_runner_path_unchanged_runner_no_pool_after_grace():
    import exits, runner
    from models import BotConfig
    s = _slot("runner")
    runner.promote(s["trade"], s, 1.2, "pumpfun")
    d = exits.decide_runner(BotConfig(), s, 1.2, lambda b, sev=0: b, lambda b: b, flow={"peak_price_sol": 1.25, "giveback_pct": 4.0}, stage="graduating", pool_missing_s=45)
    assert d.kind == "exit" and d.reason.startswith("runner-no-pool")


def test_source_has_no_graduation_close_path():
    src = Path(botmod.__file__).read_text()
    assert 'reason="bonding curve completed (LP about to deploy)"' not in src
    assert 'reason=f"null curve state' not in src
    assert '"bonding curve completed"' not in src.split("def _is_panic_exit")[1].split("def ")[0]
