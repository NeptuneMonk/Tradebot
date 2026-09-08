"""Decision ledger (gate scorecard from what blocked tokens did next) + immutable rails."""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rails  # noqa: E402
import replay  # noqa: E402
from models import BotConfig  # noqa: E402
from tests.test_autopsy_replay import _path  # noqa: E402
from tests.test_rh_paper import TOKEN, hot_bucket, make_state  # noqa: E402


def test_ledger_records_only_transitions():
    st = make_state(rh_min_growth_pct=30)
    now = time.time()
    b = hot_bucket(st.rh_discovery, now, price=1e-9, first=1e-9)      # growth 0% → blocked by "growth"
    async def scan(t):
        st.rh_paper._scan_entries(t)
        await asyncio.sleep(0)
    asyncio.run(scan(now))
    asyncio.run(scan(now + 1))
    assert len(b["decisions"]) == 1                                     # same verdict twice → one row
    first = b["decisions"][0][1]
    b["last_price_quote"] = 1.5e-9
    b["usd_market_cap"] = 50_000.0
    asyncio.run(scan(now + 2))
    assert len(b["decisions"]) == 2 and b["decisions"][-1][1] != first


def test_gate_ledger_scores_blocked_tokens_by_outcome():
    cfg = BotConfig().model_dump()
    cfg.update(take_profit_pct=25, stop_loss_pct=15, trailing_arm_pct=15, trailing_stop_pct=6, hold_max_seconds=600)
    t0 = time.time() - 3600
    runner = _path("0xr", "RUN", t0, [1e-9 * (1 + g / 100) for g in (0, 10, 20, 32, 45, 60, 80, 80, 80, 80)])
    rug = _path("0xg", "RUG", t0, [1e-9 * (1 + g / 100) for g in (0, 15, 35, 40, -30, -70, -70, -70, -70, -70)])
    runner["decisions"] = [[runner["samples"][1][0], "buyers", 1.1e-9]]        # buyers gate blocked a winner → costing
    rug["decisions"] = [[rug["samples"][1][0], "inflow", 1.15e-9]]             # inflow gate blocked a rug → saving
    tokens = [replay._Token(d, cfg, 2500.0, "rh") for d in (runner, rug)]
    rows = {r["gate"]: r for r in replay.gate_ledger(tokens, [runner, rug], replay.book_exit_view(cfg, "rh_pons"), 10.0)}
    assert rows["inflow"]["cf_pnl_usd"] < 0 and rows["inflow"]["verdict"] == "saving"
    assert rows["buyers"]["cf_pnl_usd"] > 0 and rows["buyers"]["verdict"] == "costing" and rows["buyers"]["would_win"] == 1


def test_rails_clamp_and_never_touch():
    acts, notes = rails.clamp_actions({"book_momentum_size_mult": 0.0, "daily_kill_switch_usd": 999, "book_exits.rh_pons.stop_loss_pct": 2.0,
                                       "rh_min_unique_buyers": 11})
    assert acts["book_momentum_size_mult"] == 0.25 and "daily_kill_switch_usd" not in acts
    assert acts["book_exits.rh_pons.stop_loss_pct"] == 5.0 and acts["rh_min_unique_buyers"] == 11
    assert len(notes) == 3
    d = rails.describe()
    assert "book_rh_size_mult" in d["ranges"] and "live_trading" in d["never_touch"]


def test_doctor_apply_refuses_pure_rail_violations():
    from doctor_learning import LearningEngine

    class _DB:
        class _C:
            async def find_one(self, *a, **k):
                return None

            async def update_one(self, *a, **k):
                return None
        doctor_canary = _C()
        bot_config = _C()

    eng = LearningEngine(_DB())
    try:
        asyncio.run(eng.apply({"key": "rh_gas_reserve_eth", "value": 0.0, "book": "rh_pons", "actions": {"rh_gas_reserve_eth": 0.0}}, {}))
        raise AssertionError("must refuse")
    except ValueError as e:
        assert "immutable" in str(e) or "not allowed" in str(e)
