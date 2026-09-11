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
    st.config.feed_autopause_on_doctor = True
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
    d.state = type("S", (), {"config": BotConfig(rh_feed_enabled=True, feed_autopause_on_doctor=True), "live_doctor": _LD(),
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


def test_feed_autopause_is_opt_in_and_breakers_can_be_lifted():
    st = _bot_stub()
    class _LD:
        def __init__(self): self.book_paused_until = {"scalp": time.time() + 3600, "hunt": time.time() + 3600}
        def book_paused(self, b): return time.time() < self.book_paused_until.get(b, 0)
    st.live_doctor = _LD()
    st.config.feed_autopause_on_doctor = False
    assert st.helius_autopause_state() == (False, "")            # default: tape keeps flowing while books are paused
    st.config.feed_autopause_on_doctor = True
    assert st.helius_autopause_state()[0] is True


class _FakeCfgStore:
    """Minimal bot_config collection: one document per _id."""
    def __init__(self): self.docs = {}
    async def find_one(self, q): return self.docs.get(q["_id"])
    async def update_one(self, q, u, upsert=False):
        self.docs[q["_id"]] = {**self.docs.get(q["_id"], {"_id": q["_id"]}), **u["$set"]}


def _fresh_doctor(store):
    from live_doctor import LiveDoctor
    ld = LiveDoctor.__new__(LiveDoctor)
    ld.db = type("DB", (), {"bot_config": store})()
    ld.breakers, ld._breakers_loaded, ld._breakers_failed, ld._evaluated_once = {}, False, False, False
    return ld


def test_breakers_persist_rehydrate_lift_and_expire():
    from live_doctor import BREAKER_PAUSE_S
    store = _FakeCfgStore()
    ld = _fresh_doctor(store)
    asyncio.run(ld.hydrate())
    asyncio.run(ld.arm_breaker("scalp", "payoff 0.34 < 1.0 after fees (4h, n=9)", 0.34))
    asyncio.run(ld.arm_breaker("hunt", "payoff 0.74 < 1.0", 0.74))
    doc = store.docs["breakers"]["books"]
    assert doc["scalp"]["paused"] is True and doc["scalp"]["payoff_at_pause"] == 0.34
    assert doc["scalp"]["lift_after"] - doc["scalp"]["paused_at"] == BREAKER_PAUSE_S
    assert doc["scalp"]["expires_at"] - doc["scalp"]["paused_at"] == 2 * BREAKER_PAUSE_S      # TTL guard

    # "restart": a brand-new doctor over the same store still blocks entries for both books
    ld2 = _fresh_doctor(store)
    assert asyncio.run(ld2.hydrate()) == 2
    assert ld2.book_paused("scalp") and ld2.book_paused("hunt") and not ld2.book_paused("rh_pons")
    assert set(ld2.book_paused_until) == {"scalp", "hunt"}

    # LIFT is an explicit durable write, survives the next restart
    assert asyncio.run(ld2.lift_breaker("scalp", by="user")) == ["scalp"]
    assert store.docs["breakers"]["books"]["scalp"]["paused"] is False and store.docs["breakers"]["books"]["scalp"]["lifted_by"] == "user"
    ld3 = _fresh_doctor(store)
    asyncio.run(ld3.hydrate())
    assert not ld3.book_paused("scalp") and ld3.book_paused("hunt")

    # window over / TTL over → not paused any more
    store.docs["breakers"]["books"]["hunt"]["lift_after"] = time.time() - 1
    ld4 = _fresh_doctor(store); asyncio.run(ld4.hydrate())
    assert not ld4.book_paused("hunt")
    store.docs["breakers"]["books"]["hunt"]["lift_after"] = time.time() + 3600
    store.docs["breakers"]["books"]["hunt"]["expires_at"] = time.time() - 1
    ld5 = _fresh_doctor(store); asyncio.run(ld5.hydrate())
    assert not ld5.book_paused("hunt")


def test_restart_with_armed_breakers_blocks_entry_but_keeps_feeds_on():
    """Entries closed (breaker rehydrated), Pump.fun + RH feeds stay live under the default feed_autopause_on_doctor=False."""
    import helius_gate
    store = _FakeCfgStore()
    ld = _fresh_doctor(store); asyncio.run(ld.hydrate())
    asyncio.run(ld.arm_breaker("scalp", "payoff 0.3", 0.3)); asyncio.run(ld.arm_breaker("hunt", "payoff 0.5", 0.5)); asyncio.run(ld.arm_breaker("rh_pons", "payoff 0.4", 0.4))
    st = _bot_stub()
    st.live_doctor = _fresh_doctor(store); asyncio.run(st.live_doctor.hydrate())
    assert all(st.live_doctor.book_paused(b) for b in ("scalp", "hunt", "rh_pons"))          # entries blocked
    assert st.config.feed_autopause_on_doctor is False
    assert st.helius_autopause_state() == (False, "")                                         # Helius stays live
    helius_gate.set_auto_paused(False)
    assert helius_gate.is_helius_paused() is False
    from rh_discovery import RHDiscovery
    d = RHDiscovery.__new__(RHDiscovery)
    d.state = type("S", (), {"config": st.config, "live_doctor": st.live_doctor, "rh_paper": type("P", (), {"positions": {}})()})()
    assert d._enabled() is True and d.doctor_paused() is None                                 # RH poller stays live


def test_breakers_fail_closed_when_store_unreadable():
    class _Broken:
        async def find_one(self, q): raise RuntimeError("mongo down")
    ld = _fresh_doctor(_Broken())
    asyncio.run(ld.hydrate())
    assert ld.breakers_fail_closed() and ld.book_paused("scalp") and ld.book_paused("hunt")   # unknown → closed
    ld._evaluated_once = True                                                                  # Doctor looked → normal rule
    assert not ld.book_paused("scalp")


def test_ws_hub_forwards_candidates_only():
    from ws_hub import WSHub
    h = WSHub.__new__(WSHub)
    h._seen = {}
    # raw launch with no tape → dropped
    assert h._gate_launch("launch", {"id": "a", "classifier_action": "pending", "unique_buyers": 1}) == (None, None)
    # pending with real tape → candidate (merged payload)
    ev, d = h._gate_launch("launch_update", {"id": "a", "classifier_action": "pending", "unique_buyers": 7})
    assert ev == "candidate" and d["unique_buyers"] == 7
    # follow-up metrics → candidate_update
    ev, d = h._gate_launch("launch_update", {"id": "a", "unique_buyers": 9})
    assert ev == "candidate_update" and d == {"id": "a", "unique_buyers": 9}
    # degraded to skip → one final update flagged dropped, then silence
    ev, d = h._gate_launch("launch_update", {"id": "a", "classifier_action": "skip"})
    assert ev == "candidate_update" and d["dropped"] is True
    assert h._gate_launch("launch_update", {"id": "a", "unique_buyers": 10}) == (None, None)
    # scalp / hunt / entered are always candidates; skip never
    assert h._gate_launch("launch", {"id": "b", "classifier_action": "scalp"})[0] == "candidate"
    assert h._gate_launch("launch", {"id": "c", "entered": True})[0] == "candidate"
    assert h._gate_launch("launch", {"id": "d", "classifier_action": "skip", "unique_buyers": 50}) == (None, None)


def test_search_books_capped_runner_is_the_only_lever():
    import allocator
    from book_params import book_size_mult, ENTRY_MULT_CAP, RUNNER_MULT_CAP
    cfg = BotConfig(book_scalp_size_mult=1.75, book_hunt_size_mult=2.0, book_rh_size_mult=1.5, book_runner_size_mult=2.0)
    assert book_size_mult(cfg, "scalp") == 1.0 and book_size_mult(cfg, "hunt") == 1.0 and book_size_mult(cfg, "rh_pons") == 1.0
    assert book_size_mult(cfg, "runner") == 2.0 and ENTRY_MULT_CAP == 1.0 and RUNNER_MULT_CAP == 2.0
    hot = {"n": 40, "expectancy_r": 0.9, "wr": 0.7}
    rows = allocator.plan({"book_scalp_size_mult": 1.0, "book_hunt_size_mult": 1.0, "book_rh_size_mult": 1.0, "book_runner_size_mult": 1.0},
                          {"scalp": hot, "hunt": hot, "rh_pons": hot, "runner": hot}, {"scalp": hot, "hunt": hot, "rh_pons": hot, "runner": hot},
                          15, {"scalp": True, "hunt": True, "rh_pons": True, "runner": True})
    by = {r["book"]: r for r in rows}
    for b in ("scalp", "hunt", "rh_pons"):
        assert by[b]["next"] <= 1.0 and by[b]["change"] is False and "capped" in by[b]["reason"], by[b]   # a hot hour never scales search
    assert by["runner"]["next"] == 1.25 and by["runner"]["change"] is True                             # harvest may step up
    cold = {"n": 6, "expectancy_r": 0.9, "wr": 0.7}
    rows = allocator.plan({"book_runner_size_mult": 1.0}, {"runner": cold}, {"runner": cold}, 15, {"runner": True})
    rr = next(r for r in rows if r["book"] == "runner")
    assert rr["change"] is False and "< 20" in rr["reason"]                                              # 12 blended fills → not before 20
    # red search book: allocator may shrink it, never "add size to beat fees"
    red = {"n": 40, "expectancy_r": -0.4, "wr": 0.3}
    rows = allocator.plan({"book_scalp_size_mult": 1.0}, {"scalp": red}, {"scalp": red}, 15, {"scalp": True})
    assert next(r for r in rows if r["book"] == "scalp")["next"] < 1.0


def test_discovery_clip_caps_entry_notional_but_not_runner_add_on():
    import r_sizer, runner
    cfg = BotConfig(max_trade_usd=25.0, discovery_clip_usd=10.0, book_runner_size_mult=2.0)
    sz = r_sizer.size_trade(bankroll_usd=1000.0, risk_per_trade_pct=2.0, sl_pct=12.0, exit_slip_pct=3.0, book_mult=1.0, doctor_mult=1.0, governor_mult=1.0,
                            min_trade_usd=0.5, max_trade_usd=min(cfg.max_trade_usd, cfg.discovery_clip_usd))
    assert sz["size_usd"] == 10.0                                          # R-size would be ~$133 → clipped to the discovery ceiling
    plan = runner.add_on_plan(cfg, {"r_usd": 4.0}, depth_usd=5000.0, exit_slip_bps=800, entry_slip_bps=500, fee_usd_round_trip=0.05, max_trade_usd=25.0)
    assert plan["size_usd"] == 4.0                                         # 0.5R × 4 × runner mult 2.0 — the only lever that scales
    assert "discovery_clip_usd" in __import__("rails").NEVER_TOUCH        # operator-owned, the Doctor cannot lift it


def test_sequencer_factory_calldata_wakes_the_poller_and_feed_idles_with_doctor():
    import rh_feed
    from rh_discovery import RHDiscovery, FACTORY
    d = RHDiscovery.__new__(RHDiscovery)
    d.stats, d._wake, d._last_poll_ts = {}, asyncio.Event(), 0.0
    d.state = type("S", (), {"config": BotConfig(rh_feed_enabled=True, feed_autopause_on_doctor=True),
                             "live_doctor": type("LD", (), {"book_paused": staticmethod(lambda b: b == "rh_pons")})(),
                             "rh_paper": type("P", (), {"positions": {}})()})()
    assert not d._wake.is_set()
    d.wake("factory tx seq=42")
    assert d._wake.is_set() and d.stats["wakes"] == 1 and d.stats["last_wake_reason"] == "factory tx seq=42"
    # the feed classifies FACTORY calldata as a wake, not as a curve trade
    feed = rh_feed.RHSequencerFeed.__new__(rh_feed.RHSequencerFeed)
    feed.state = type("S2", (), {"config": d.state.config, "rh_discovery": d, "rh_paper": d.state.rh_paper})()
    feed.stats = {"messages": 0, "txs": 0, "curve_sells": 0, "rug_alerts": 0, "factory_txs": 0, "last_seq": 0}
    d.tracking, d._curve_to_token = {}, {}
    rh_feed.decode_tx = lambda txb: {"to": FACTORY, "value": 0, "data": b"\\x00"}
    rh_feed._walk = lambda body: [b"tx"]
    import base64, json
    raw = json.dumps({"messages": [{"sequenceNumber": 7, "message": {"message": {"header": {"kind": 3}, "l2Msg": base64.b64encode(b"x").decode()}}}]})
    feed._on_message(raw)
    assert feed.stats["factory_txs"] == 1 and d.stats["wakes"] == 2
    # rh_pons benched + flat + autopause opt-in → the sequencer socket idles exactly like the poller
    assert d.doctor_paused() and feed._enabled() is False
    d.state.config.feed_autopause_on_doctor = False
    assert feed._enabled() is True


def test_poll_loop_wake_cuts_the_sleep_short():
    from rh_discovery import RHDiscovery
    d = RHDiscovery.__new__(RHDiscovery)
    d.stats, d._wake, d._last_poll_ts, d._consec_429, d._next_from = {}, asyncio.Event(), 0.0, 0, 0
    d._enabled = lambda: True
    polls = []
    async def _poll(): polls.append(time.time())
    d.poll_once = _poll

    async def run():
        task = asyncio.create_task(d._loop())
        await asyncio.sleep(3.3)            # initial 3 s delay + first poll
        n0 = len(polls)
        d.wake("factory")                   # should poll again well before the 2 s tick
        await asyncio.sleep(0.8)
        task.cancel()
        return n0, len(polls)
    n0, n1 = asyncio.run(run())
    assert n0 == 1 and n1 == 2
