"""Governor release is honoured · doctor pause → Helius auto-pause · slim history projection."""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import helius_gate
from models import BotConfig
from tests.test_profitability_refactor import _bot_stub


def _engine(dd_pct: float):
    from bankroll import BankrollEngine
    eng = BankrollEngine.__new__(BankrollEngine)
    eng.state = type("S", (), {"config": BotConfig(bankroll_sizing_enabled=True, governor_drawdown_pct=5.0, governor_hours=6.0)})()
    eng.governor = {c: {"until": 0.0, "reason": "", "released_at": 0.0, "released_dd_pct": 0.0} for c in ("sol", "rh")}
    eng.snapshot = {"chains": {"rh": {"drawdown_24h_pct": dd_pct}}}
    eng.rh_fee_floor = {}
    calls = []

    class _St:
        async def update_one(self, q, u, upsert=False): calls.append(u)
    eng.db = type("DB", (), {"autopilot_state": _St(), "trades": None})()

    async def _bank(chain): return 1000.0, "paper"
    async def _pnl(hours, mode, chain): return dd_pct * 10.0   # dd% of a $1000 bankroll
    eng.bankroll_usd, eng._pnl_since = _bank, _pnl
    eng.calls = calls
    return eng


def test_governor_release_is_honoured_until_drawdown_deepens_another_step():
    eng = _engine(-7.2)
    asyncio.run(eng._refresh_chain("rh"))
    assert eng.governor_active("rh")                     # −7.2% ≤ −5% → engaged
    asyncio.run(eng.release_governor("rh"))
    assert not eng.governor_active("rh") and eng.governor["rh"]["released_dd_pct"] == -7.2
    snap, _ = asyncio.run(eng._refresh_chain("rh"))
    assert not eng.governor_active("rh") and snap["governor_active"] is False   # the old bug: re-armed on this refresh
    eng.snapshot["chains"]["rh"]["drawdown_24h_pct"] = -9.0
    async def _pnl9(h, m, c): return -90.0
    eng._pnl_since = _pnl9
    asyncio.run(eng._refresh_chain("rh"))
    assert not eng.governor_active("rh")                 # −9% is within the same step (−7.2 − 5 = −12.2)
    async def _pnl13(h, m, c): return -130.0
    eng._pnl_since = _pnl13
    asyncio.run(eng._refresh_chain("rh"))
    assert eng.governor_active("rh")                     # deepened past another full step → re-armed
    eng2 = _engine(-7.2)
    asyncio.run(eng2.release_governor("rh"))
    eng2.governor["rh"]["released_at"] = time.time() - 7 * 3600     # window over → normal rule again
    asyncio.run(eng2._refresh_chain("rh"))
    assert eng2.governor_active("rh")


def test_helius_gate_manual_or_auto():
    helius_gate.set_paused(False); helius_gate.set_auto_paused(False)
    assert helius_gate.is_helius_paused() is False
    assert helius_gate.set_auto_paused(True, "doctor") is True and helius_gate.is_helius_paused() is True
    assert helius_gate.set_auto_paused(True, "doctor") is False          # no flip
    assert helius_gate.snapshot() == {"paused": True, "manual": False, "auto": True, "auto_reason": "doctor"}
    helius_gate.set_auto_paused(False)
    helius_gate.set_paused(True)
    assert helius_gate.is_helius_paused() is True                        # operator switch always wins
    helius_gate.set_paused(False)


def test_autopause_only_when_both_books_paused_and_no_solana_position():
    st = _bot_stub()
    class _LD:
        def __init__(self, paused): self.p = paused
        def book_paused(self, b): return b in self.p
    st.live_doctor = _LD({"scalp"})
    assert st.helius_autopause_state()[0] is False                       # only scalp paused → keep the feed
    st.live_doctor = _LD({"scalp", "hunt"})
    on, why = st.helius_autopause_state()
    assert on is True and "scalp + hunt" in why
    st.active_trades["m"] = {"trade": {"book": "scalp", "chain": None}}
    assert st.helius_autopause_state()[0] is False                       # an open Solana position still needs the feed
    st.active_trades = {"r": {"trade": {"book": "rh_pons", "chain": "rh"}}}
    assert st.helius_autopause_state()[0] is True                        # RH positions don't need Helius
    st.live_doctor = None
    st.inventory.halted_until = time.time() + 600
    assert st.helius_autopause_state()[0] is True and "inventory halt" in st.helius_autopause_state()[1]


def test_rh_feed_idles_when_rh_pons_paused_and_flat():
    from rh_discovery import RHDiscovery
    d = RHDiscovery.__new__(RHDiscovery)
    class _LD:
        def book_paused(self, b): return b == "rh_pons"
    d.state = type("S", (), {"config": BotConfig(rh_feed_enabled=True), "live_doctor": _LD(),
                             "rh_paper": type("P", (), {"positions": {}})()})()
    assert d._enabled() is False
    d.state.rh_paper.positions["0xabc"] = {"trade": {}}
    assert d._enabled() is True
    d.state.config.rh_feed_enabled = False
    assert d._enabled() is False
