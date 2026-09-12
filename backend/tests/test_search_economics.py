"""A+B+C: ledger math, dead regime skips search (not runner / manual), time-stop off by default."""
import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import exits
import regime
import search_ledger
from models import BotConfig, Launch
from tests.test_profitability_refactor import _bot_stub
from tests.test_runner_book import _patched, _slot


def test_ledger_math():
    rows = [
        {"book": "runner", "pnl_usd": 30.0, "promotion_banked_usd": 5.0},
        {"book": "runner", "pnl_usd": -4.0},
        {"book": "scalp", "pnl_usd": -6.0}, {"book": "scalp", "pnl_usd": 2.0},
        {"book": "hunt", "pnl_usd": -9.0}, {"book": "rh_pons", "pnl_usd": -1.0},
        {"book": "rh_pons", "pnl_usd": 0.5},
    ]
    led = search_ledger.compute(rows, search_budget_pct=0.40, window_days=7)
    assert led["harvest_realised_usd"] == 31.0 and led["runners_promoted"] == 2
    assert led["search_realised_usd"] == -13.5 and led["search_losses_usd"] == 16.0 and led["search_fills"] == 5
    assert led["seed_usd"] == 70.0 and led["remaining_usd"] == round(70 + 0.4 * 31 - 16, 4)
    assert led["cost_per_runner_usd"] == 8.0
    empty = search_ledger.compute([], search_budget_pct=0.4)
    assert empty["cost_per_runner_usd"] is None and empty["remaining_usd"] == 70.0
    neg = search_ledger.compute([{"book": "runner", "pnl_usd": -50.0}, {"book": "scalp", "pnl_usd": -80.0}])
    assert neg["remaining_usd"] == 70.0 - 80.0        # negative harvest adds nothing; budget can go negative (display)


def test_regime_bands():
    cfg = BotConfig()
    assert regime.market_regime(cfg, 3.0, 0.5, 1) == "dead"
    assert regime.market_regime(cfg, 12.0, 0.0, 0) == "dead"          # thin AND nothing hot
    assert regime.market_regime(cfg, 12.0, 0.2, 0) == "quiet"
    assert regime.market_regime(cfg, 45.0, 0.1, 0) == "busy"
    assert regime.market_regime(cfg, 45.0, 0.3, 1) == "hot"
    assert regime.market_regime(cfg, 100.0, 0.0, -1) == "hot"
    assert regime.sol_1h_sign() == 0                                  # no samples → neutral
    assert regime.market_regime(cfg, 0.0, 0.0, 0, warm=False) == "quiet"   # fresh process: never dead before warm-up


def _entry_stub(rate_h, hot_share):
    st = _patched(_bot_stub())
    st._launch_rate = lambda: rate_h
    st.process_started_ts = time.time() - 3600      # warmed up
    now = time.time()
    st.recent_launches = [{"_detected_ts": now, "unique_buyers": 9 if i < int(hot_share * 10) else 1} for i in range(10)]
    st._enter_impl = AsyncMock()
    st._reserve_position_slot = getattr(st, "_reserve_position_slot", None)
    return st


def test_dead_regime_skips_search_entries_but_not_manual_or_reentry():
    import bot as botmod
    st = _entry_stub(2.0, 0.0)
    assert st.market_regime()["regime"] == "dead" and st.search_regime_block() == "search-regime-dead"
    launch = Launch(mint="M" * 44, symbol="X", name="X", creator="C" * 44, bonding_curve="B" * 44, risk_score=10, classifier_action="scalp")
    asyncio.run(botmod.BotState._enter(st, launch, 10, "scalp"))
    assert st._enter_impl.await_count == 0 and st._skip_counts["search-regime-dead"] == 1
    st.config.regime_dead_blocks_search = False
    assert st.search_regime_block() is None
    st2 = _entry_stub(60.0, 0.3)
    assert st2.search_regime_block() is None


def test_rh_gates_return_dead_regime_and_runner_untouched():
    from tests.test_rh_paper import make_state, hot_bucket, TOKEN
    st = make_state()
    b = hot_bucket(st.rh_discovery, time.time())
    st.search_regime_block = lambda: "search-regime-dead"
    assert st.rh_paper._gates(TOKEN, b, time.time()) == "search-regime-dead"
    st.search_regime_block = lambda: None
    assert st.rh_paper._gates(TOKEN, b, time.time()) != "search-regime-dead"
    # runner decisions never consult the time-stop or the regime
    assert exits.search_dead_tape(BotConfig(), "runner", {"last_new_buyer_ts": 1.0}, time.time()) is None


def test_time_stop_off_by_default_and_fires_only_when_configured():
    cfg = BotConfig()
    now = time.time()
    b = {"last_new_buyer_ts": now - 300, "last_inflow_ts": now - 300}
    for book in ("scalp", "hunt", "rh_pons"):
        assert exits.search_dead_tape(cfg, book, b, now, entry_ts=now - 400) is None      # default 0 = off
    cfg.book_exits = {**(cfg.book_exits or {}), "scalp": {**((cfg.book_exits or {}).get("scalp") or {}), "no_new_buyers_s": 40}}
    d = exits.search_dead_tape(cfg, "scalp", b, now, entry_ts=now - 400)
    assert d is not None and d.kind == "exit" and d.reason.startswith("search-dead-tape")
    fresh = {"last_new_buyer_ts": now - 5, "last_inflow_ts": now - 300}
    assert exits.search_dead_tape(cfg, "scalp", fresh, now, entry_ts=now - 400) is None        # a new buyer resets it
    assert exits.search_dead_tape(cfg, "scalp", b, now, entry_ts=now - 10) is None             # entry itself counts as a tick
    assert exits.search_dead_tape(cfg, "hunt", b, now, entry_ts=now - 400) is None             # only scalp configured
