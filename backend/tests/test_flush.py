"""Flush detector: forensics, exit hold, re-entry priming, Doctor scorecard (offline)."""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import flush  # noqa: E402
from tests.test_rh_paper import TOKEN, enter, hot_bucket, make_state  # noqa: E402


def _dip(b, now, seller="0x" + "f" * 40, sellers=1, buyers=2):
    b["sell_events"].clear()
    for i in range(sellers):
        w = seller if i == 0 else f"0x{i:040x}"
        b["sell_events"].append((now - 3 + i * 0.1, 0.5 if i == 0 else 0.05, w))
    b["buy_events"].clear()
    for i in range(buyers):
        b["buy_events"].append((now - 1, 0.02, f"0xb{i:039x}"))


def test_dip_forensics_and_is_flush():
    st = make_state()
    now = time.time()
    b = hot_bucket(st.rh_discovery, now, price=1e-9)
    pos = {"peak_ts": now - 5, "peak_price": 1.3e-9, "trough_price": 1.0e-9, "_last_price": 1.02e-9}
    _dip(b, now, sellers=1, buyers=2)
    f = flush.dip_forensics(b, pos, now, 30)
    assert f["n_sellers"] == 1 and f["top_seller_share"] == 1.0 and f["buyers"] == 2
    assert abs(f["drop_pct"] - 23.08) < 0.1 and abs(f["bounce_pct"] - 2.0) < 0.01
    assert flush.is_flush(f, st.config)
    _dip(b, now, sellers=5, buyers=2)                      # five sellers → distribution
    f2 = flush.dip_forensics(b, pos, now, 30)
    assert f2["n_sellers"] == 5 and not flush.is_flush(f2, st.config)
    _dip(b, now, sellers=1, buyers=0)                      # nobody buying the dip → not a flush either
    assert not flush.is_flush(flush.dip_forensics(b, pos, now, 30), st.config)


def test_flush_holds_stop_then_floor_or_timeout():
    st = make_state(stop_loss_pct=12.0, flush_hold_s=10, flush_extra_drop_pct=5.0, flush_hold_scope="all",
                    no_momentum_exit_enabled=False, exit_momentum_gate_enabled=False)
    now = time.time()
    b = hot_bucket(st.rh_discovery, now, price=1e-9)
    enter(st)
    pos = st.rh_paper.positions[TOKEN]
    pos["peak_ts"] = now - 4
    _dip(b, now, sellers=1, buyers=2)
    b["last_price_quote"] = 0.86e-9                       # -14%: SL would fire
    assert st.rh_paper._decide_exit(pos, b, now) is None     # held — single-seller flush
    assert pos["_dip_forensics"]["flush"] and pos["_flush_hold_since"] == now and st.rh_paper.stats["flush_holds"] == 1
    assert st.rh_paper._decide_exit(pos, b, now + 6) is None  # still inside the hold
    b["last_price_quote"] = 0.80e-9                       # 7% under the flush trough → floor broken → sell
    assert st.rh_paper._decide_exit(pos, b, now + 7) == "stop_loss"
    assert pos["_dip_forensics"]["hold_broke_floor"] is True
    # timeout path
    st2 = make_state(stop_loss_pct=12.0, flush_hold_s=10, flush_hold_scope="all", no_momentum_exit_enabled=False, exit_momentum_gate_enabled=False)
    b2 = hot_bucket(st2.rh_discovery, now, price=1e-9)
    enter(st2)
    pos2 = st2.rh_paper.positions[TOKEN]
    pos2["peak_ts"] = now - 4
    _dip(b2, now)
    b2["last_price_quote"] = 0.86e-9
    assert st2.rh_paper._decide_exit(pos2, b2, now) is None
    assert st2.rh_paper._decide_exit(pos2, b2, now + 11) == "stop_loss"
    # scope: hot_reentry → an ordinary fresh position is NOT held
    st3 = make_state(stop_loss_pct=12.0, flush_hold_scope="hot_reentry", no_momentum_exit_enabled=False, exit_momentum_gate_enabled=False)
    b3 = hot_bucket(st3.rh_discovery, now, price=1e-9)
    enter(st3)
    pos3 = st3.rh_paper.positions[TOKEN]
    pos3["peak_ts"] = now - 4
    _dip(b3, now)
    b3["last_price_quote"] = 0.86e-9
    assert st3.rh_paper._decide_exit(pos3, b3, now) == "stop_loss"
    pos3["trade"]["reentry"] = "breakout"                 # …but a re-entry leg is in scope
    assert st3.rh_paper._decide_exit(pos3, b3, now) is None


def test_flush_stop_primes_recovery_reentry_and_spares_hot_strike():
    st = make_state(stop_loss_pct=12.0, flush_hold_s=0, flush_hold_scope="all", reentry_enabled=True, reentry_window_seconds=300,
                    no_momentum_exit_enabled=False, exit_momentum_gate_enabled=False)
    now = time.time()
    b = hot_bucket(st.rh_discovery, now, price=1e-9)
    enter(st)
    pos = st.rh_paper.positions[TOKEN]
    pos["peak_ts"] = now - 4
    _dip(b, now)
    b["last_price_quote"] = 0.86e-9
    assert st.rh_paper._decide_exit(pos, b, now) == "stop_loss"      # hold_s=0 → sells, but forensics say flush
    asyncio.run(st.rh_paper.exit(TOKEN, "stop_loss"))
    doc = st.db.trades.docs[pos["trade"]["id"]]
    assert doc["dip_forensics"]["flush"] and doc["flush_held"] is True
    w = st.rh_paper.watch[TOKEN]
    assert w["flush"] and not w["hot"] and w["last_exit_was_sl"] is False and w["max_attempts"] == 1
    assert st.rh_paper.stats["flush_reentry_watches"] == 1
    assert st.rh_paper.hot_board()[0]["flush"] is True
    # hot token: a flush-caused losing leg is NOT a strike
    st.rh_paper.watch[TOKEN].update(hot=True, strikes=0, max_attempts=None, window_s=None)
    st.rh_paper.entered.discard(TOKEN)
    b["last_price_quote"] = 1e-9
    enter(st)
    pos = st.rh_paper.positions[TOKEN]
    pos["peak_ts"] = now - 4
    _dip(b, now)
    b["last_price_quote"] = 0.86e-9
    st.rh_paper._decide_exit(pos, b, now)
    asyncio.run(st.rh_paper.exit(TOKEN, "stop_loss"))
    w = st.rh_paper.watch[TOKEN]
    assert w["hot"] and w["strikes"] == 0 and w["flush_exits"] == 1 and w["last_exit_was_sl"] is False


def test_flush_scorecard_proposes_longer_hold_when_flushes_run():
    cfg = {"flush_hold_s": 10}
    rows, peaks = [], {}
    for i in range(6):
        rows.append({"id": f"f{i}", "exit_reason": "stop_loss", "pnl_pct": -13.0, "dip_forensics": {"flush": True}})
        peaks[f"f{i}"] = 30.0 if i < 4 else 2.0
    for i in range(4):
        rows.append({"id": f"d{i}", "exit_reason": "trailing_stop", "pnl_pct": 4.0, "dip_forensics": {"flush": False}})
        peaks[f"d{i}"] = -5.0
    sc = flush.flush_scorecard(rows, peaks, cfg)
    assert sc["flush"]["n"] == 6 and sc["flush"]["ran_after"] == 4 and abs(sc["flush"]["ran_share"] - 0.667) < 0.01
    assert sc["distributed"]["ran_after"] == 0 and sc["proposal"] == 15
    held = [{"id": f"h{i}", "exit_reason": "stop_loss", "pnl_pct": -18.0, "flush_held": True, "flush_hold_at_pnl_pct": -12.0,
             "dip_forensics": {"flush": True}} for i in range(5)]
    sc2 = flush.flush_scorecard(held, {}, cfg)
    assert sc2["held_n"] == 5 and sc2["held_avg_delta_pct"] == -6.0 and sc2["proposal"] == 5
