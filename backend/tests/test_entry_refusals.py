"""Entry-path holds (inventory halt, doctor pause, caps, cooldowns, on-chain liquidity) must never be silent:
the candidate row that shows `gate ✓` carries the refusal so the operator can see why nothing is entered."""
import asyncio
import os
import sys
import time
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_entry_refusals")

import bot as bot_mod
from models import BotConfig
from scanner import MomentumScanner


def _state():
    st = bot_mod.BotState.__new__(bot_mod.BotState)
    st.config = BotConfig()
    st.tracking = {}
    st.stats = {}
    st.active_trades = {}
    st.entered_mints = set()
    return st


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_refuse_stamps_bucket_and_dedupes_broadcast(monkeypatch):
    st = _state()
    st.tracking["M"] = {"symbol": "DEGENT"}
    sent = []

    async def fake_skip(payload):
        sent.append(payload)
    st._skip_event = fake_skip
    _run(st._refuse("M", "seasoned", "inventory-halt", "5 loss closes inside 90m — 1200s left"))
    _run(st._refuse("M", "seasoned", "inventory-halt", "5 loss closes inside 90m — 1195s left"))   # same reason, 5s later
    assert len(sent) == 1 and sent[0]["reason"] == "inventory-halt"
    assert st.tracking["M"]["entry_refusal"]["reason"] == "inventory-halt"
    assert "1195s" in st.tracking["M"]["entry_refusal"]["detail"]                        # detail keeps refreshing
    _run(st._refuse("M", "seasoned", "max-positions", "3/3 slots in use"))
    assert len(sent) == 2 and st.tracking["M"]["entry_refusal"]["reason"] == "max-positions"


def test_snapshot_exposes_recent_refusal_on_passing_row():
    st = _state()
    cfg = st.config
    cfg.scanner_min_growth_pct = -1000
    cfg.min_curve_liquidity_sol = 0
    cfg.scanner_min_mc_usd_seasoned = 0
    cfg.scanner_min_mc_velocity_5m_pct_seasoned = -1000
    cfg.band_seasoned_min_age_min, cfg.band_seasoned_max_age_min = 0, 10_000
    now = time.time()
    st.tracking["M"] = {
        "symbol": "DEGENT", "name": "Degent", "protocol": "pumpswap", "start": now - 600, "graduated_at": now - 500,
        "buyers": {"a", "b"}, "buy_events": deque(), "sell_events": deque(), "price_samples": deque([(now - 100, 1.0), (now - 1, 2.0)]),
        "mc_samples": deque([(now - 100, 1000.0), (now - 1, 2000.0)]), "usd_market_cap": 50_000.0, "last_price_sol": 2.0, "first_price_sol": 1.0,
        "real_sol_reserves": 50 * 10**9, "gate_reason": "pass", "last_trade_ms": int(now * 1000),
        "entry_refusal": {"reason": "inventory-halt", "detail": "5 loss closes inside 90m", "ts": now - 10},
    }
    sc = MomentumScanner(st)
    rows = {r["mint"]: r for r in sc.candidates_snapshot()}
    assert rows["M"]["blocked"]["reason"] == "inventory-halt"
    st.tracking["M"]["entry_refusal"]["ts"] = now - 200                                   # stale → not shown
    rows = {r["mint"]: r for r in sc.candidates_snapshot()}
    assert rows["M"]["blocked"] is None
