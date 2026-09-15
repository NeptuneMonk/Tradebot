"""Hardwired (no knob): ratchet trail tiers and fast-fail half-size entry + add-on at +5 %."""
import asyncio
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import exits  # noqa: E402
from bot import BotState, FAST_FAIL_ADD_AT_PCT  # noqa: E402
from tests.test_rh_paper import make_state, hot_bucket, TOKEN  # noqa: E402


def test_ratchet_trail_tiers():
    assert exits.ratchet_trail(10.0, 5.0) == 10.0        # below first tier: configured trail
    assert exits.ratchet_trail(10.0, 15.0) == 6.0        # +15 % peak → at most 6 %
    assert exits.ratchet_trail(10.0, 30.0) == 4.0        # +30 % peak → at most 4 %
    assert exits.ratchet_trail(3.0, 40.0) == 3.0         # never loosens a tighter configured trail
    assert exits.ratchet_trail(0.0, 40.0) == 0.0         # disabled stays disabled


def test_levels_applies_ratchet_and_arms(monkeypatch):
    monkeypatch.setattr(exits, "exit_param", lambda cfg, book, k, reg=None: {"trailing_stop_pct": 10.0, "trailing_arm_pct": 20.0, "stop_loss_pct": 12.0}.get(k, 0.0))
    monkeypatch.setattr(exits, "trade_regime", lambda cfg, book, t: None)
    monkeypatch.setattr(exits, "r_pct", lambda slot: 12.0)
    slot = {"trade": {"book": "scalp", "entry_price_sol": 1.0}, "peak_price_sol": 1.32}
    lv = exits.levels(None, slot)
    assert lv["trailing_stop_pct"] == 4.0 and lv["trailing_arm_pct"] == 15.0


@pytest.mark.asyncio
async def test_sol_fast_fail_add_doubles_position_and_averages_entry(monkeypatch):
    import bot as bot_mod
    import pumpfun

    async def fake_sol_price():
        return 100.0

    monkeypatch.setattr(bot_mod, "get_sol_usd_price", fake_sol_price)
    monkeypatch.setattr(pumpfun, "quote_buy_tokens", lambda state, lamports, slip: (lamports * 10, lamports))   # 10 tokens per lamport
    writes = {}

    class _Trades:
        async def update_one(self, q, u):
            writes.update(u["$set"])

    class _Hub:
        async def broadcast(self, *a, **k):
            pass

    monkeypatch.setattr(bot_mod, "hub", _Hub())
    st = BotState(db=type("DB", (), {"trades": _Trades()})())
    st.config.min_trade_usd = 1.0
    t = {"id": "t1", "symbol": "X", "mode": "paper", "entry_tokens": 5_000_000_000, "entry_sol": 0.05, "entry_usd": 5.0, "entry_price_sol": 0.05 * 1e9 / 5_000_000_000 / 1e9}
    slot = {"trade": t, "_ff_remaining_usd": 5.0}
    await st._fast_fail_add("Mint", slot, "pumpfun", {}, cur_price_sol=1.05e-10)
    assert "_ff_remaining_usd" not in slot
    assert t["entry_usd"] == 10.0 and t["entry_sol"] == pytest.approx(0.10)
    assert t["entry_tokens"] == 5_000_000_000 + 0.05 * 1e9 * 10
    assert writes["fast_fail_add"]["usd"] == 5.0 and writes["entry_price_sol"] == t["entry_price_sol"]


@pytest.mark.asyncio
async def test_rh_add_triggers_at_plus_5_and_averages_entry(monkeypatch):
    st = make_state()
    paper = st.rh_paper
    now = time.time()
    b = hot_bucket(paper.state.rh_discovery, now)
    writes = {}

    class _Trades:
        async def update_one(self, q, u):
            writes.update(u["$set"])

    paper.state.db = type("DB", (), {"trades": _Trades()})()
    monkeypatch.setattr(paper, "_quote_usd", lambda sym: 1000.0)
    pos = {"trade": {"id": "r1", "mode": "paper", "entry_price_quote": 1e-6, "entry_tokens": 1000.0, "entry_usd": 5.0,
                     "ff_remaining_usd": 5.0, "venue": "curve"}, "peak_price": 1e-6, "opened": now}
    paper.positions[TOKEN] = pos
    paper._maybe_fast_fail_add(TOKEN, pos, b, 1.04e-6, now)          # +4 %: nothing happens
    assert not pos.get("_ff_adding") and not writes
    paper._maybe_fast_fail_add(TOKEN, pos, b, 1.06e-6, now)          # +6 %: second half is bought
    await asyncio.sleep(0.05)
    t = pos["trade"]
    assert t["ff_remaining_usd"] == 0.0 and t["entry_usd"] == pytest.approx(10.0)
    assert t["entry_tokens"] > 1000.0 and 1e-6 < t["entry_price_quote"] < 1.06e-6   # averaged up, below the add price
    assert writes["fast_fail_add"]["usd"] == pytest.approx(5.0)
