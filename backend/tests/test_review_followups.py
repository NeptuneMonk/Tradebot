"""Follow-ups after the ba5782d review: latency stamps, exit-fee budget, parked losers in the scorecard."""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
from bot import BotState  # noqa: E402
from models import BotConfig, Trade  # noqa: E402
import scorecard  # noqa: E402


def test_latency_fields_computed_in_order():
    t0 = time.time()
    lat = BotState._latency_fields({"t_signal": t0, "t_decision": t0 + 0.4}, t0 + 0.5, t0 + 1.7)
    assert lat["signal_to_decision_ms"] == 400 and lat["decision_to_sent_ms"] == 100
    assert lat["sent_to_landed_ms"] == 1200 and lat["decision_to_landed_ms"] == 1300
    assert set(lat) >= {"t_signal", "t_decision", "t_sent", "t_landed"}
    # partial stamps never crash and never go negative
    lat2 = BotState._latency_fields({"t_decision": t0}, None, None)
    assert lat2["decision_to_sent_ms"] is None and lat2["sent_to_landed_ms"] is None
    assert Trade(mint="M" * 32, symbol="X", latency=lat).latency["decision_to_landed_ms"] == 1300


def test_exit_fee_budget_caps_total_fees_across_attempts():
    st = BotState.__new__(BotState)
    st.config = BotConfig(speed_mode="manual", exit_fee_max_pct_of_position=2.0, exit_fee_budget_pct_of_position=5.0,
                          exit_priority_floor_microlamports=100_000, exit_priority_cap_microlamports=20_000_000,
                          exit_sender_mode_normal="swqos")
    pos, cu = 0.05, 100_000                       # 0.05 SOL position → budget 0.0025 SOL, per-attempt cap 0.001 SOL
    fresh = st._exit_fee_ladder(500_000, False, position_sol=pos, cu=cu)
    nearly_spent = st._exit_fee_ladder(500_000, False, position_sol=pos, cu=cu, fee_spent_sol=0.0024)
    tip = 5_000
    per_attempt = lambda prio: (prio * cu / 1e6 + tip) / 1e9          # priority + tip (the 5,000-lamport base sig fee is outside the cap)
    assert max(per_attempt(p) for p in fresh) <= pos * 0.02 + 1e-9
    # with 0.0024 of the 0.0025 SOL budget gone, every rung must fit in the remaining 0.0001 SOL
    assert max(per_attempt(p) for p in nearly_spent) <= 0.0001 + 1e-9
    assert max(nearly_spent) < max(fresh)
    assert BotConfig().exit_fee_budget_pct_of_position >= BotConfig().exit_fee_max_pct_of_position


def test_scorecard_counts_parked_losers():
    rows = [
        {"status": "closed", "r_usd": 1.0, "pnl_usd": 2.0, "exit_reason": "tp", "book": "scalp"},
        {"status": "exit_failed_terminal", "r_usd": 1.0, "pnl_usd": -5.0, "pnl_provisional": True, "exit_reason": "sell reverted — held", "book": "scalp"},
    ]
    st = scorecard.stats(rows)
    assert st["n"] == 2 and st["wr"] == 0.5 and st["expectancy_usd"] == -1.5
    src = open(os.path.join(os.path.dirname(__file__), "..", "scorecard.py")).read()
    assert '"exit_failed_terminal"' in src
    bot_src = open(os.path.join(os.path.dirname(__file__), "..", "bot.py")).read()
    assert bot_src.count("await self.scorecard.record(trade_doc)") >= 2     # closed path + parked path
