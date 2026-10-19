"""Published 2026-10-18 findings: max-hold clock must not depend on price ticks; MC floor must gate every automatic path."""
import os
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
from bot import BotState  # noqa: E402
from models import BotConfig  # noqa: E402


def _st(**cfg):
    st = BotState.__new__(BotState)
    st.config = BotConfig(**cfg)
    st._is_manual_hold = lambda slot: bool(slot["trade"].get("long_term_hold") or slot["trade"].get("classifier_action") == "manual")
    return st


def _slot(age_s, **t):
    entry = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat()
    return {"trade": {"id": "t", "entry_time": entry, "classifier_action": "scanner_momentum", **t}, "_entry_ts_mono": time.time() - age_s}


def test_max_hold_due_uses_persisted_entry_time_and_exempts_holds():
    st = _st(max_hold_any_book_s=900)
    assert st._max_hold_due(_slot(600)) is None
    due = st._max_hold_due(_slot(1187))
    assert due is not None and 1186 <= due <= 1188
    assert st._max_hold_due(_slot(1187, classifier_action="manual")) is None
    assert st._max_hold_due(_slot(1187, long_term_hold=True)) is None
    assert _st(max_hold_any_book_s=0)._max_hold_due(_slot(5000)) is None
    # restart-safe: no entry_time → falls back to the monotonic entry stamp
    s = _slot(1000); s["trade"].pop("entry_time")
    assert _st(max_hold_any_book_s=900)._max_hold_due(s) is not None


def test_monitor_loop_checks_the_clock_before_any_price_read():
    src = open(os.path.join(os.path.dirname(__file__), "..", "bot.py")).read()
    loop = src[src.index("async def _monitor_position"):]
    tick = loop.index('slot["last_monitor_tick"] = time.time()')
    clock = loop.index("_due = self._max_hold_due(slot)")
    first_price_read = min(loop.index("_monitor_pool_state(slot, pool)"), loop.index("fetch_bonding_curve_state"))
    assert tick < clock < first_price_read


def test_mc_floor_gate_covers_smart_wallet_and_top_dog_paths():
    src = open(os.path.join(os.path.dirname(__file__), "..", "bot.py")).read()
    body = src[src.index("async def _enter_impl"):]
    gate = body[body.index('"mc-floor"') - 1200: body.index('"mc-floor"') + 200]
    assert "action not in MANUAL_ENTRY_ACTIONS" in gate and "scanner_min_mc_usd_new" in gate and "scanner_min_mc_usd_seasoned" in gate
    assert "_manual_force" in gate                       # operator "buy anyway" is the only bypass
