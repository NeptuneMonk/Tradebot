"""Re-entry audit fixes: real pullback (bounce + confirm), min wait, watch dies with a losing leg."""
import asyncio, os, sys, time
from collections import deque
from types import SimpleNamespace
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
from reentry_logic import decide_reentry, recent_buyers_and_inflow, update_watch_price
from models import BotConfig

CFG = BotConfig(reentry_pullback_pct=22.0)   # live setting governs open watches (matches the 22% the watches below carry)


def _w(exit_price=100.0, t0=1000.0, **kw):
    w = {"exit_price_sol": exit_price, "exit_time": t0, "last_exit_time": t0,
         "peak_price_after_exit": exit_price, "trough_after_peak": exit_price,
         "pullback_pct": 22.0, "last_exit_was_sl": False}
    w.update(kw); return w


def test_dump_after_exit_is_not_a_pullback():
    w = _w()
    # price falls straight down 40% below exit, buyers present — old code fired here
    for p in (90, 80, 70, 60):
        assert decide_reentry(w, p, 1100.0, buyers_recent=5, inflow_ok=True, cfg=CFG) is None
    # even a small bounce off the low doesn't count: the token never ran on after our exit
    assert decide_reentry(w, 64, 1101.0, 5, True, CFG) is None


def test_real_pullback_requires_run_on_pullback_bounce_and_buyers():
    w = _w()
    assert decide_reentry(w, 130, 1100.0, 1, False, CFG) is None      # ran on (+30%), no pullback yet
    assert decide_reentry(w, 100, 1110.0, 5, False, CFG) is None      # trough -23% from peak but still falling (no bounce)
    assert decide_reentry(w, 101, 1120.0, 5, False, CFG) is None      # only +1% off trough (< 3% confirm)
    assert decide_reentry(w, 103.5, 1125.0, 1, False, CFG) is None    # bouncing but only 1 buyer
    assert decide_reentry(w, 103.5, 1130.0, 2, False, CFG) == "pullback"  # +3.5% off trough, 2 buyers


def test_min_wait_blocks_instant_rebuy():
    w = _w(t0=1000.0, peak_price_after_exit=130.0, trough_after_peak=100.0)
    assert decide_reentry(w, 104, 1005.0, 5, True, CFG) is None        # 5s after exit
    assert decide_reentry(w, 104, 1021.0, 5, True, CFG) == "pullback"  # after 20s


def test_breakout_needs_strong_buyers_inflow_and_no_prior_sl():
    w = _w()
    assert decide_reentry(w, 106, 1100.0, 3, False, CFG) is None      # no inflow
    assert decide_reentry(w, 106, 1100.0, 2, True, CFG) is None       # 2 < exit_momentum_min_buyers (3)
    assert decide_reentry(w, 106, 1100.0, 3, True, CFG) == "breakout"
    w2 = _w(last_exit_was_sl=True)
    assert decide_reentry(w2, 106, 1100.0, 3, True, CFG) is None


def test_trough_resets_on_new_peak():
    w = _w()
    update_watch_price(w, 120); update_watch_price(w, 90); update_watch_price(w, 125)
    assert w["peak_price_after_exit"] == 125 and w["trough_after_peak"] == 125


def test_recent_buyers_and_inflow():
    now = 1000.0
    ev = deque([(now - 30, 5, "a"), (now - 5, 2, "b"), (now - 3, 1, "b"), (now - 1, 4, "c")])
    assert recent_buyers_and_inflow(ev, now, 10) == (2, 7.0)


def test_rh_watch_dies_with_losing_leg_and_carries_attempts():
    import rh_paper
    st = SimpleNamespace(config=BotConfig(), rh_discovery=SimpleNamespace(tracking={}, _quote_usd=lambda s: 2500.0))
    tr = rh_paper.RHPaperTrader(st)
    b = {"graduated": False}
    tr._watch_after_exit("T", {"pnl_pct": 10.0, "classifier_action": "rh_pons_paper"}, 1.0, b, 1000.0)
    assert "T" in tr.watch and tr.watch["T"]["attempts"] == 0 and tr.watch["T"]["last_exit_time"] == 1000.0
    tr.watch["T"]["attempts"] = 2
    tr._watch_after_exit("T", {"pnl_pct": 4.0, "classifier_action": "rh_pons_reentry"}, 1.2, b, 1100.0)
    assert tr.watch["T"]["attempts"] == 2 and tr.watch["T"]["exit_price_quote"] == 1.2
    tr._watch_after_exit("T", {"pnl_pct": -12.0, "classifier_action": "rh_pons_reentry"}, 0.9, b, 1200.0)
    assert "T" not in tr.watch


def test_config_defaults_and_clamps():
    c = BotConfig()
    assert (c.reentry_min_wait_s, c.reentry_min_bounce_pct, c.reentry_bounce_confirm_pct,
            c.reentry_min_buyers, c.reentry_breakout_pct) == (20, 5.0, 3.0, 2, 5.0)
