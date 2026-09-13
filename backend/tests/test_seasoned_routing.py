"""Seasoned = hunt exits (no clock) on PumpSwap; RH post-pool entries gated on a live v4 pool. New band unchanged."""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import exits
from book_params import book_for_action, HUNT_ACTIONS, exit_param
from models import BotConfig
from tests.test_profitability_refactor import _bot_stub
from tests.test_runner_book import _patched, _slot


def test_routing_new_band_scalp_seasoned_hunt_not_a_hunt_cap_action():
    assert book_for_action("momentum_new") == "scalp"
    assert book_for_action("scanner_momentum") == "hunt"
    assert "scanner_momentum" not in HUNT_ACTIONS and book_for_action("greylist_snipe") == "hunt"
    cfg = BotConfig(creator_solvency_enabled=False)
    assert exit_param(cfg, "scalp", "hold_max_seconds") > 0          # 40 s clock stays on new-band scalps
    assert exit_param(cfg, "hunt", "hold_max_seconds") == 0           # seasoned rides hunt: no clock


def test_hunt_cap_ignores_seasoned_hunt_rows():
    st = _patched(_bot_stub())
    s1 = _slot("hunt"); s1["trade"]["classifier_action"] = "greylist_snipe"
    s2 = _slot("hunt"); s2["trade"]["classifier_action"] = "scanner_momentum"
    st.active_trades["A"], st.active_trades["B"] = s1, s2
    assert st._hunt_open() == 1


def test_seasoned_hunt_has_no_clock_and_can_be_promoted():
    cfg = BotConfig(creator_solvency_enabled=False)
    s = _slot("hunt"); s["trade"]["classifier_action"] = "scanner_momentum"; s["trade"]["mode"] = "paper"
    d = exits.decide_hunt(cfg, s, 3.0, 1.03, 900.0, lambda *a: False, lambda *a: False)   # 15 min in, flat: nothing fires
    assert d.kind is None
    import runner
    runner.promote(s["trade"], s, 1.6, "pumpswap")
    assert s["trade"]["book"] == "runner" and s["trade"]["runner_stage"] == "graduated"


def test_no_pool_no_fill_and_tally():
    from unittest.mock import AsyncMock, patch
    from models import Launch
    import bot as botmod
    st = _patched(_bot_stub())
    st.config.paper_entry_latency_ms = 0
    st.leader_fence = AsyncMock(return_value=True)
    mint = "So11111111111111111111111111111111111111112"
    st.tracking[mint] = {"protocol": "pumpswap", "buyers": set(), "buy_count": 9}
    from unittest.mock import AsyncMock as _AM
    st.db.trades.find_one = _AM(return_value=None)
    launch = Launch(mint=mint, symbol="S", name="S", creator="C" * 44, bonding_curve="B" * 44, risk_score=10, classifier_action="scanner_momentum")
    with patch("bot.pumpswap.find_pool_for_mint", new=AsyncMock(return_value=None)), \
         patch("bot.pumpswap.fetch_pool_state", new=AsyncMock(return_value=None)):
        asyncio.run(botmod.BotState._enter_impl(st, launch, 10, "scanner_momentum"))
    assert mint not in st.active_trades
    assert ("skip", "seasoned-no-pool") in st.calls
    # the real _skip_event tallies by band
    import bot as _b
    st2 = _bot_stub()
    asyncio.run(_b.BotState._skip_event(st2, {"mint": mint, "band": "seasoned", "reason": "seasoned-no-pool"}))
    assert st2.skip_tallies()["seasoned"] == {"seasoned-no-pool": 1}
    assert not any(c[0] == "db" and c[1].get("status") == "active" for c in st.calls)


def test_rh_graduated_without_pool_skips_with_pool_may_pass():
    from tests.test_rh_paper import make_state, hot_bucket, TOKEN
    st = make_state()
    now = time.time()
    b = hot_bucket(st.rh_discovery, now)
    b["graduated"], b["graduated_at"] = True, now - 120
    assert st.rh_paper._gates(TOKEN, b, now) == "rh-grad-no-pool"
    b["pool_live"], b["last_pool_swap_ts"] = True, now - 5
    r = st.rh_paper._gates(TOKEN, b, now)
    assert r not in ("rh-grad-no-pool", "graduated", "curve", "age")      # curve/age gates no longer apply post-pool
    b["last_pool_swap_ts"] = now - 60
    assert st.rh_paper._gates(TOKEN, b, now) == "rh-seasoned-stale"
    b["last_pool_swap_ts"], b["graduated_at"] = now - 5, now - 2 * 3600
    assert st.rh_paper._gates(TOKEN, b, now) == "rh-seasoned-age"
    b["graduated_at"] = now - 120
    st.config.rh_live_trading = True
    b["quote_symbol"] = "ETH"
    assert st.rh_paper._gates(TOKEN, b, now) not in ("rh-seasoned-live-unsupported", "rh-grad-no-pool", "graduated")   # live via rh_dex.buy


def test_classifier_unused_on_pumpswap_and_no_trending_clients():
    src = Path(__file__).resolve().parents[1]
    bot_src = (src / "bot.py").read_text()
    assert 'if is_new_band and protocol == "pumpfun" and not bypass_gates:' in bot_src
    text = "".join(p.read_text() for p in src.glob("*.py"))
    for needle in ("dexscreener", "birdeye", "/trending"):
        assert needle not in text.lower(), needle
