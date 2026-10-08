"""Cohort-unwind flush + buy-the-dip paths, replaying the COMPANY tape (2026-10-08 21:08–21:12 UTC):
a 20-wallet bundle buys the impulse we enter on, dumps 100s later straight back to the pre-impulse base (-35%),
fresh wallets buy the dip 7s later, the token graduates at 9x two minutes on."""
import os
import sys
import time
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_flush_cohort")

import bot as bot_mod
import flush
from models import BotConfig
from reentry_logic import decide_reentry, recent_buyers_and_inflow

LAMPORTS = 1_000_000_000
BUNDLE = [f"bundle{i}" for i in range(20)]
BASE = 2.70e-7            # where the token sat before the burst
ENTRY = 4.13e-7           # we bought the top of the burst


def _state(**cfg):
    st = bot_mod.BotState.__new__(bot_mod.BotState)
    base = dict(flush_hold_enabled=True, flush_hold_scope="all", flush_hold_s=27, flush_top_share=0.7, flush_max_sellers=2,
                flush_min_buyers=1, flush_extra_drop_pct=10.0, flush_window_s=30, flush_cohort_enabled=True, flush_cohort_share=0.6,
                flush_cohort_window_s=45, flush_dip_addon_enabled=False)
    st.config = BotConfig(**{**base, **cfg})
    st.stats = {}
    st.tracking = {}
    return st


def _company_bucket(now, dump_sellers=BUNDLE, fresh_dip_buyers=()):
    """entry at now-100 on the bundle's burst; bundle dumps at now-2; optional fresh buyers at now."""
    entry_ts = now - 100
    buys = deque([(entry_ts - 2 + i * 0.1, int(0.8 * LAMPORTS), w) for i, w in enumerate(BUNDLE)])
    buys.append((entry_ts - 60, int(0.5 * LAMPORTS), "organic1"))            # pre-burst organic buyer (not cohort)
    for w in fresh_dip_buyers:
        buys.append((now + 5, int(2.4 * LAMPORTS), w))          # dip buyers arrive AFTER the hold starts
    sells = deque([(now - 2, 0.9, w) for w in dump_sellers])
    samples = deque([(entry_ts - 90, BASE * 0.95), (entry_ts - 50, BASE), (entry_ts - 1, 3.5e-7), (entry_ts + 5, ENTRY)])
    return {"buyers": set(), "buy_events": buys, "sell_events": sells, "price_samples": samples}, entry_ts


def _slot(entry_ts):
    return {"trade": {"symbol": "COMPANY", "mint": "M", "entry_price_sol": ENTRY, "size_usd": 75.0, "entry_usd": 75.0,
                      "entry_sol": 0.4, "entry_tokens": 1_000_000, "mode": "paper"},
            "_entry_ts_mono": entry_ts, "peak_price_sol": ENTRY, "peak_ts": entry_ts + 5, "trough_price_sol": BASE}


def test_entry_cohort_and_pre_impulse_base():
    now = time.time()
    b, entry_ts = _company_bucket(now)
    ev = [(ts, lam / LAMPORTS, w) for ts, lam, w in b["buy_events"]]
    cohort = flush.entry_cohort(ev, entry_ts, 45)
    assert cohort == set(BUNDLE)                                   # organic1 bought 60s before → not the impulse
    assert flush.pre_impulse_base(b["price_samples"], entry_ts, 45) == BASE


def test_bundle_unwind_is_a_cohort_flush_not_distribution():
    st = _state()
    now = time.time()
    st.tracking["M"], entry_ts = _company_bucket(now)
    slot = _slot(entry_ts)
    assert st._flush_holds_sol("M", slot, "sl", -35.5, 2.72e-7) is True
    f = slot["_dip_forensics"]
    assert f["flush"] is True and f["flush_kind"] == "cohort-unwind"
    assert f["n_sellers"] == 20 and f["cohort_sellers"] == 20 and f["cohort_share"] == 1.0
    assert f["base_price"] == BASE and abs(f["floor_price"] - BASE * 0.9) < 1e-12   # floor = pre-impulse base − extra drop
    assert st.stats["flush_holds"] == 1


def test_cohort_flush_disabled_falls_back_to_old_rule():
    st = _state(flush_cohort_enabled=False)
    now = time.time()
    st.tracking["M"], entry_ts = _company_bucket(now)
    slot = _slot(entry_ts)
    assert st._flush_holds_sol("M", slot, "sl", -35.5, 2.72e-7) is False
    assert slot["_dip_forensics"]["flush"] is False and slot["_dip_forensics"].get("cohort_share") is None


def test_unrelated_holders_dumping_is_still_distribution():
    st = _state()
    now = time.time()
    st.tracking["M"], entry_ts = _company_bucket(now, dump_sellers=[f"holder{i}" for i in range(20)])
    slot = _slot(entry_ts)
    assert st._flush_holds_sol("M", slot, "sl", -35.5, 2.72e-7) is False
    assert slot["_dip_forensics"]["flush"] is False and slot["_dip_forensics"]["cohort_share"] == 0.0


def test_cohort_hold_breaks_when_price_falls_through_the_base():
    st = _state()
    now = time.time()
    st.tracking["M"], entry_ts = _company_bucket(now)
    slot = _slot(entry_ts)
    assert st._flush_holds_sol("M", slot, "sl", -35.5, 2.72e-7) is True
    assert st._flush_holds_sol("M", slot, "sl", -42.0, BASE * 0.88) is False      # under base − 10% → the pump is gone
    assert slot["_dip_forensics"]["hold_broke_floor"] is True


def test_hold_survives_the_7s_dip_and_arms_the_dip_addon():
    st = _state(flush_dip_addon_enabled=True, reentry_bounce_confirm_pct=3.0, flush_dip_addon_min_buyers=1)
    now = time.time()
    st.tracking["M"], entry_ts = _company_bucket(now, fresh_dip_buyers=("fresh1", "fresh2", "fresh3"))
    slot = _slot(entry_ts)
    assert st._flush_holds_sol("M", slot, "sl", -35.5, 2.72e-7) is True
    assert "_flush_addon_due" not in slot                                       # no bounce yet
    assert st._flush_holds_sol("M", slot, "sl", -17.0, 3.43e-7) is True          # 7s later: +27% off the trough, 3 fresh buyers
    assert slot.pop("_flush_addon_due") == 3.43e-7 and slot["_flush_addon_started"] is True
    assert st._flush_holds_sol("M", slot, "sl", -15.0, 3.5e-7) is True
    assert "_flush_addon_due" not in slot                                       # one add-on per position


def test_dip_addon_needs_fresh_buyers_not_the_flush_sellers():
    st = _state(flush_dip_addon_enabled=True)
    now = time.time()
    st.tracking["M"], entry_ts = _company_bucket(now, fresh_dip_buyers=("bundle3", "bundle4"))   # sellers re-buying
    slot = _slot(entry_ts)
    assert st._flush_holds_sol("M", slot, "sl", -35.5, 2.72e-7) is True
    assert st._flush_holds_sol("M", slot, "sl", -17.0, 3.43e-7) is True
    assert "_flush_addon_due" not in slot


def test_flush_reclaim_reentry_trigger():
    cfg = BotConfig(flush_reentry_wait_s=5, reentry_bounce_confirm_pct=3.0, reentry_min_buyers=2, flush_reentry_max_chase_pct=50.0)
    now = time.time()
    w = {"flush": True, "exit_price_sol": 2.54e-7, "exit_time": now - 20, "last_exit_time": now - 20, "last_exit_was_sl": False,
         "peak_price_after_exit": 2.54e-7, "trough_after_peak": 2.54e-7, "flush_trough": 2.54e-7, "cohort": BUNDLE}
    assert decide_reentry(w, 2.50e-7, now - 19, 3, True, cfg) is None          # inside the wait
    assert decide_reentry(w, 2.50e-7, now, 3, True, cfg) is None               # no bounce off the trough yet
    assert w["flush_trough"] == 2.50e-7
    assert decide_reentry(w, 3.40e-7, now, 1, True, cfg) is None               # bounce but only 1 fresh buyer
    assert decide_reentry(w, 3.40e-7, now, 3, True, cfg) == "flush-reclaim"    # COMPANY 21:10:38 → re-buy
    assert decide_reentry(w, 4.00e-7, now, 3, True, cfg) is None               # +57% above the fill = chasing


def test_recent_buyers_excludes_the_cohort():
    now = time.time()
    ev = [(now - 1, 2.4 * LAMPORTS, "fresh1"), (now - 1, 0.5 * LAMPORTS, "bundle1"), (now - 1, 0.6 * LAMPORTS, "bundle2")]
    assert recent_buyers_and_inflow(ev, now, 10)[0] == 3
    n, amt = recent_buyers_and_inflow(ev, now, 10, exclude=set(BUNDLE))
    assert n == 1 and abs(amt - 2.4 * LAMPORTS) < 1


def test_rh_single_seller_path_unchanged():
    f = flush.dip_forensics({"sell_events": [(time.time() - 1, 2.0, "whale")], "buy_events": [(time.time(), 0.1, "a")]},
                            {"peak_ts": 0, "peak_price": 1.0, "trough_price": 0.8, "_last_price": 0.82}, time.time(), 30)
    cfg = BotConfig()
    assert flush.flush_kind(f, cfg) == "single-seller" and flush.is_flush(f, cfg) is True and f.get("cohort_share") is None
