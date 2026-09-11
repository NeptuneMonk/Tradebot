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
    assert d._enabled() is False and d.doctor_paused() == "live-doctor paused rh_pons · no open RH position"
    d.state.rh_paper.positions["0xabc"] = {"trade": {}}
    assert d._enabled() is True and d.doctor_paused() is None
    d.state.config.rh_feed_enabled = False
    assert d._enabled() is False


def test_pl_candles_ohlc_via_api():
    import os, requests
    tok = open("/app/memory/.tok").read().strip()
    base = os.environ.get("REACT_APP_BACKEND_URL") or [l.split("=", 1)[1].strip() for l in open("/app/frontend/.env") if l.startswith("REACT_APP_BACKEND_URL")][0]
    h = {"Authorization": f"Bearer {tok}"}
    for bs in (300, 900, 1800, 3600, 14400, 43200, 86400):
        d = requests.get(f"{base}/api/pl/buckets", params={"bucket_s": bs, "candles": 60, "days": 7}, headers=h, timeout=40).json()
        n = min(60, -(-7 * 86400 // bs))                                          # fixed candle width → fewer candles on big timeframes
        assert d["n"] == n and len(d["buckets"]) == n and d["bucket_s"] == bs
        ts = [c["t"] for c in d["buckets"]]
        assert all(b - a == bs for a, b in zip(ts, ts[1:])) and all(t % bs == 0 for t in ts)
        prev_close = None
        for c in d["buckets"]:
            assert c["low"] <= min(c["open"], c["close"]) <= max(c["open"], c["close"]) <= c["high"]
            assert abs(c["close"] - c["open"] - c["pnl_usd"]) < 1e-3              # body = P/L closed in the period
            if prev_close is not None:
                assert abs(c["open"] - prev_close) < 1e-6                          # candles chain like a price series
            if c["trades"] == 0:
                assert c["open"] == c["close"] == c["high"] == c["low"]            # doji when nothing closed
            prev_close = c["close"]
            assert c["range"] == [c["low"], c["high"]]
    assert requests.get(f"{base}/api/pl/buckets", params={"bucket_s": 123}, headers=h, timeout=20).status_code == 400


def test_equity_three_trades_one_15m_bucket_ohlc():
    from server import build_equity
    base = "2026-06-01T10:0"
    trades = [
        {"exit_time": f"{base}1:00+00:00", "pnl_usd": 4.0, "mode": "paper", "entry_usd": 20.0, "r_usd": 2.0, "mfe_pct": 40.0},   # ran to +8 before closing +4
        {"exit_time": f"{base}5:00+00:00", "pnl_usd": -6.0, "mode": "paper", "entry_usd": 20.0, "mae_pct": -50.0},              # dipped to -10 before -6
        {"exit_time": f"{base}9:00+00:00", "pnl_usd": 1.5, "mode": "live"},
        {"exit_time": None, "pnl_usd": 99.0, "mode": "paper"},                                                                   # ignored: no exit_time
    ]
    out = build_equity(trades, 900, now_ts=1_800_000_000, open_mark_usd=0.75)
    assert out["n_fills"] == 3 and len(out["candles"]) == 1
    c = out["candles"][0]
    assert c["open"] == 0.0 and c["close"] == -0.5 and c["n"] == 3
    assert c["high"] >= max(c["open"], c["close"]) and c["low"] <= min(c["open"], c["close"])
    assert c["high"] == 8.0            # 0 + MFE of trade 1
    assert c["low"] == -6.0            # 4 + MAE(-10) → -6, equal to the closed path low (4-6 = -2 is higher)
    assert c["paper_usd"] == -2.0 and c["live_usd"] == 1.5 and c["pnl_usd"] == -0.5
    assert [p["equity"] for p in out["points"][:3]] == [4.0, -2.0, -0.5]
    assert out["points"][-1]["mark"] is True and out["equity_usd"] == 0.25 and out["realized_usd"] == -0.5
    # trades that fall in two different buckets → two candles chained open = previous close, empty gaps → no candle
    trades2 = [{"exit_time": "2026-06-01T10:01:00+00:00", "pnl_usd": 2.0}, {"exit_time": "2026-06-01T11:31:00+00:00", "pnl_usd": -1.0}]
    out2 = build_equity(trades2, 900, now_ts=1_800_000_000)
    assert len(out2["candles"]) == 2 and out2["candles"][1]["open"] == out2["candles"][0]["close"] == 2.0
