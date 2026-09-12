"""Integration: a recorded Pump.fun graduate walks complete → graduating → pool → exit, and exit_sol > 0 comes from the AMM."""
import asyncio
import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot as botmod
from tests.test_graduation_hold import _st, _run_exit, _db_sets

FIX = json.loads((Path(__file__).parent / "fixtures" / "graduation_recorded.json").read_text())


def test_recorded_graduate_complete_then_pool_then_exit_books_amm_proceeds():
    st, s = _st(book="hunt")
    t = s["trade"]
    t.update(FIX["entry"])
    curve = {**FIX["curve_open"], "creator": FIX["creator"]}
    pool_calls = {"n": 0}

    async def find_pool(_mint):
        pool_calls["n"] += 1
        return FIX["pool"] if pool_calls["n"] >= 3 else None      # pool shows up on the third probe (~20 s later)

    with patch("bot.pumpfun.fetch_bonding_curve_state", new=AsyncMock(side_effect=lambda m: curve)), \
         patch("bot.pumpswap.find_pool_for_mint", new=AsyncMock(side_effect=find_pool)), \
         patch("bot.pumpswap.fetch_pool_state", new=AsyncMock(return_value=FIX["pool_state"])):
        # 1) curve still open: a normal exit decision would sell on the curve — nothing graduation-related yet
        assert not s.get("_curve_complete")
        # 2) curve completes: exit hits a dead curve, no pool → row stays active as graduating, no PnL
        curve.update(FIX["curve_complete"])
        _run_exit(st, "max hold 45s")
        assert t["venue_stage"] == "graduating" and t.get("status") != "closed" and FIX["mint"] in st.active_trades
        assert not any(d.get("status") == "closed" for d in _db_sets(st))
        # 3) still inside grace, still no pool
        _run_exit(st, "max hold 45s")
        assert t["venue_stage"] == "graduating" and FIX["mint"] in st.active_trades
        # 4) pool live → same row flips to pumpswap and the exit fills there
        _run_exit(st, "max hold 45s")
    assert s["protocol"] == "pumpswap" and s["pumpswap_pool"] == FIX["pool"] and t["protocol"] == "pumpswap"
    assert t["status"] == "closed" and t["exit_sol"] > 0
    exp, _ = botmod.pumpswap.quote_sell_sol(FIX["pool_state"], FIX["entry"]["entry_tokens"], 800)
    assert abs(t["exit_sol"] - exp / 1e9) < 1e-12
    assert t["pnl_pct"] > 0 and t["pnl_pct"] != -100
    assert t["mint"] == FIX["mint"]                          # same CA end to end — only the venue changed
    persisted = [d for d in _db_sets(st) if d.get("status") == "closed"]
    assert persisted and persisted[-1]["protocol"] == "pumpswap" and persisted[-1]["exit_sol"] == t["exit_sol"]


def test_recorded_graduate_grace_expiry_parks_not_minus_100():
    st, s = _st(book="scalp")
    t = s["trade"]
    t.update(FIX["entry"])
    s["_runner_pool_missing_since"] = time.time() - 120
    with patch("bot.pumpfun.fetch_bonding_curve_state", new=AsyncMock(return_value={**FIX["curve_complete"], "creator": FIX["creator"]})), \
         patch("bot.pumpswap.find_pool_for_mint", new=AsyncMock(return_value=None)):
        _run_exit(st, "stop-loss hit -12%")
    assert t["status"] == "exit_failed_terminal" and t["venue_stage"] == "held_through_migrate"
    assert t["pnl_pct"] is None and t["held_intent"] == "exit"
