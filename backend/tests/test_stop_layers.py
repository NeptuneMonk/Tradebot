"""EMERGENT_CHANGES #2 — switchable stop-delay layers; absolute hard stop + universal max hold that nothing can defer."""
import asyncio
import os
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot import BotState  # noqa: E402
from models import BotConfig  # noqa: E402
import exits  # noqa: E402


def _state(**over):
    st = BotState.__new__(BotState)
    st.config = BotConfig(**over)
    st.tracking = {}
    st.exits = []

    async def _exit(mint, reason="", **kw):
        st.exits.append((mint, reason))
    st._exit = _exit
    st._is_manual_hold = lambda slot: slot["trade"].get("classifier_action") == "manual"
    # every hold layer ENABLED and "buyers active" — the hard stop must not care
    st._buy_momentum_holds = lambda *a, **k: True
    st._flush_holds_sol = lambda *a, **k: True
    return st


def _slot(entry=1e-9, age_s=60, book="scalp", manual=False):
    return {"trade": {"id": "t", "mint": "m", "entry_price_sol": entry, "book": book,
                      "classifier_action": "manual" if manual else "scanner_momentum"},
            "_entry_ts_mono": time.time() - age_s}


def test_hard_stop_fires_same_tick_through_every_hold_layer():
    st = _state(hard_stop_extra_pct=5.0, exit_momentum_gate_enabled=True, flush_hold_enabled=True, recovery_watch_enabled=True)
    slot = _slot(age_s=60)
    sl = float(exits.levels(st.config, slot)["stop_loss_pct"])
    price = 1e-9 * (1 - (sl + 6) / 100)                                    # −(SL+6)%
    assert asyncio.run(st._run_ladder("m", slot, price, 60)) and st.exits[0][1].startswith("hard-stop")
    st.exits.clear()
    price_soft = 1e-9 * (1 - (sl + 2) / 100)                               # inside the extra band → normal (holdable) path
    asyncio.run(st._run_ladder("m", _slot(age_s=60), price_soft, 60))
    assert not any(r.startswith("hard-stop") for _, r in st.exits)
    st.exits.clear()
    assert not asyncio.run(st._run_ladder("m", _slot(age_s=60, manual=True), price, 60)) and st.exits == []   # manual exempt


def test_max_hold_any_book_fires_for_every_book_but_not_manual():
    st = _state(max_hold_any_book_s=120)
    for book in ("scalp", "hunt", "runner"):
        st.exits.clear()
        assert asyncio.run(st._run_ladder("m", _slot(age_s=200, book=book), 1.1e-9, 200)) and st.exits[0][1].startswith("max-hold")
    st.exits.clear()
    assert not asyncio.run(st._run_ladder("m", _slot(age_s=200, manual=True), 1.1e-9, 200)) and st.exits == []
    st2 = _state(max_hold_any_book_s=0)
    st2.exits.clear()
    asyncio.run(st2._run_ladder("m", _slot(age_s=5000, book="hunt"), 1.1e-9, 5000))
    assert not any(r.startswith("max-hold") for _, r in st2.exits)


def test_momentum_hold_reads_flag_at_call_time():
    st = BotState.__new__(BotState)
    st.config = BotConfig(exit_momentum_gate_enabled=False)
    st.tracking = {"m": {"buy_events": [(time.time(), 10**9, f"w{i}") for i in range(10)]}}
    assert st._buy_momentum_holds("m", _slot(), "sl", -12.0) is False
    st.config.flush_hold_enabled = False
    assert st._flush_holds_sol("m", _slot(), "sl", -12.0, 0.88e-9) is False


def test_dead_tape_exit_on_red():
    cfg = BotConfig(book_exits={"scalp": {"no_new_buyers_s": 10}})
    bucket = {"last_new_buyer_ts": time.time() - 30, "last_inflow_ts": time.time() - 30}
    assert exits.search_dead_tape(cfg, "scalp", bucket, time.time(), entry_ts=time.time() - 60, trade={}, pct=-5.0) is None
    assert exits.search_dead_tape(cfg, "scalp", bucket, time.time(), entry_ts=time.time() - 60, trade={}, pct=None) is not None
    assert BotConfig().dead_tape_exit_on_red is True and BotConfig().hard_stop_extra_pct == 5.0 and BotConfig().max_hold_any_book_s == 900
