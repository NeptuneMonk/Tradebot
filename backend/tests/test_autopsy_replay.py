"""Loss autopsy + tick store + universe replay (offline)."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import autopsy  # noqa: E402
import replay  # noqa: E402
import tick_store  # noqa: E402
from doctor_learning import propose_technique  # noqa: E402
from models import BotConfig  # noqa: E402


def _t(pnl_usd, pnl_pct, *, mfe=0.0, reason="stop_loss", fees=0.2, growth=None, rug=None, hold_s=60, trig_pnl=None, defer_s=0, chain="rh", **kw):
    ep = 1e-9
    t = {"id": f"t{pnl_usd}{pnl_pct}{mfe}{reason}{growth}", "chain": chain, "mint": "0xabc", "classifier_action": "rh_pons_paper",
         "pnl_usd": pnl_usd, "pnl_pct": pnl_pct, "fees_usd": fees, "exit_reason": reason, "rug_alert": rug,
         "entry_price_quote": ep, "peak_price_quote": ep * (1 + mfe / 100.0), "entry_time": "2026-09-08T10:00:00+00:00",
         "exit_time": f"2026-09-08T10:{hold_s // 60:02d}:{hold_s % 60:02d}+00:00", "entry_ctx": {"growth_pct": growth},
         "exit_trigger_pnl_pct": trig_pnl, "exit_deferred_s": defer_s}
    t.update(kw)
    return t


def test_classify_causes():
    c = lambda t, pk=None: autopsy.classify(t, pk)["cause"]  # noqa: E731
    assert c(_t(-5, -60, hold_s=30)) == "rugged"
    assert c(_t(-1, -10, rug={"kind": "creator_sell"})) == "rugged"
    assert c(_t(-0.05, -0.5, fees=0.2)) == "fee_drag"                    # +0.15 gross, red after fees
    assert c(_t(-1.5, -15, trig_pnl=-8.0)) == "slippage"
    assert c(_t(-1.5, -15, growth=90.0)) == "chased"
    assert c(_t(-1.5, -12, reason="stop_loss"), 35.0) == "stopped_then_ran"
    assert c(_t(-1.5, -12, reason="stop_loss"), 5.0) == "stopped"
    assert c(_t(-1.5, -12, mfe=18.0, reason="trailing_stop")) == "gave_back"
    assert c(_t(-1.2, -12, mfe=1.0, reason="no_momentum")) == "dead_entry"
    assert c(_t(-1.5, -15, trig_pnl=-9.0 + 0.0, defer_s=4.0, mfe=0.0, reason="stop_loss")) == "slippage"
    assert c(_t(-1.5, -15, trig_pnl=-11.0, defer_s=4.0, mfe=0.0, reason="max_hold")) == "deferred_loser"
    assert c(_t(3.0, 30, mfe=80.0, reason="trailing_stop")) == "runner"
    assert c(_t(1.0, 10, mfe=35.0, reason="trailing_stop")) == "left_on_table"
    assert c(_t(2.0, 20, mfe=21.0, reason="take_profit")) == "clean_tp"


def test_summarize_chased_whatif_proposal():
    cfg = BotConfig().model_dump()
    rows = [_t(-2.0, -20, growth=120.0 + i) for i in range(6)]          # chased losers
    rows += [_t(1.5, 15, growth=20.0 + i, reason="take_profit") for i in range(8)]
    rows += [_t(-0.8, -8, growth=25.0 + i) for i in range(3)]           # ordinary stops
    s = autopsy.summarize(rows, cfg, min_n=5)
    assert s["n_loss"] == 9 and s["causes"][0]["cause"] == "chased" and s["causes"][0]["n"] == 6
    p = s["proposal"]
    assert p and p["key"] == "rh_max_growth_pct" and p["value"] <= 100 and p["gain"] > 0.02
    assert p["evidence"]["kept"] == 11
    # rugs dominating → buyer floor proposal (estimated)
    rows2 = [_t(-6.0, -60, hold_s=20) for _ in range(4)] + [_t(1.0, 10, reason="take_profit") for _ in range(4)]
    s2 = autopsy.summarize(rows2, cfg, min_n=5)
    assert s2["proposal"]["key"] == "rh_min_unique_buyers" and s2["proposal"]["estimated"]


def _path(mint, symbol, start, prices, buys_per_step=2, q=0.002, age0=600.0):
    start = start + age0                      # samples begin age0 seconds after launch (past the PONS snipe tax)
    samples = [[start + i * 5.0, p] for i, p in enumerate(prices)]
    buys = []
    for i in range(len(prices)):
        for k in range(buys_per_step):
            buys.append([start + i * 5.0 - 0.5, q, f"w{mint}{i}{k}"])
    return {"chain": "rh", "mint": mint, "symbol": symbol, "start": start - age0, "first_price": prices[0], "quote_symbol": "ETH",
            "samples": samples, "buys": buys}


def test_replay_book_measures_gates_on_the_universe():
    cfg = BotConfig().model_dump()
    cfg.update(rh_min_age_s=10, rh_max_age_min=30, rh_min_mc_usd=0, rh_max_mc_usd=1e12, rh_min_unique_buyers=8,
               rh_min_growth_pct=30, rh_max_growth_pct=400, rh_min_new_buyers_1m=0, rh_min_inflow_usd=0,
               take_profit_pct=25, stop_loss_pct=15, trailing_arm_pct=15, trailing_stop_pct=6, hold_max_seconds=600, rh_max_trade_usd=10)
    t0 = time.time() - 3600
    # runner: +30% at sample 3 then to +80% ; rug: +35% then -70% ; dud: flat
    runner = _path("0xr", "RUN", t0, [1e-9 * (1 + g / 100) for g in (0, 10, 20, 32, 45, 60, 80, 80, 80, 80)])
    rug = _path("0xg", "RUG", t0 + 10, [1e-9 * (1 + g / 100) for g in (0, 15, 35, 40, -30, -70, -70, -70, -70, -70)])
    dud = _path("0xd", "DUD", t0 + 20, [1e-9] * 10, buys_per_step=0)
    res = replay.replay_book("rh_pons", [runner, rug, dud], cfg, {"ETH": 2500.0}, min_n=1, window_h=24)
    assert res["n_tokens"] == 3 and res["current"]["fills"] == 2                 # runner + rug enter (≥30% growth, ≥8 buyers)
    assert res["rows"] and all(r["fills"] <= 3 for r in res["rows"])
    assert res["ladder"]["take_profit_pct"] == 25 and res["stake_usd"] == 10
    # strict current gates → the runner becomes a "missed winner" a looser variant would have caught
    cfg2 = {**cfg, "rh_min_unique_buyers": 30}
    res2 = replay.replay_book("rh_pons", [runner, rug, dud], cfg2, {"ETH": 2500.0}, min_n=1, window_h=24)
    assert res2["current"]["fills"] == 0 and res2["missed_winners_n"] >= 1 and res2["missed_winners"][0]["symbol"] == "RUN"
    # …but every looser variant also lets the RUG in, whose gap-aware stop (−60% print) outweighs the runner → no proposal
    assert res2["best"] is None and res2["proposal"] is None and res2["dodged_rugs_n"] == 1


def test_replay_fills_are_gap_aware_and_calibration_withholds_rosy_proposals():
    cfg = BotConfig().model_dump()
    cfg.update(rh_min_age_s=0, rh_max_age_min=30, rh_min_mc_usd=0, rh_max_mc_usd=1e12, rh_min_unique_buyers=1, rh_min_growth_pct=0,
               rh_max_growth_pct=400, rh_min_new_buyers_1m=0, rh_min_inflow_usd=0, stop_loss_pct=12, take_profit_pct=25, hold_max_seconds=600)
    t0 = time.time() - 3600
    rug = _path("0xg", "RUG", t0, [1e-9, 1.05e-9, 0.4e-9, 0.4e-9, 0.4e-9, 0.4e-9])       # gaps −60% in one print
    tk = replay._Token(rug, cfg, 2500.0, "rh")
    o = tk.outcome(0, replay.book_exit_view(cfg, "rh_pons"), 10.0)
    assert o["reason"] == "sl" and o["ret_pct"] < -55                                    # not −12: the next print was −60%
    # calibration: our real fills on the same token lost far more than the replay claims → untrusted, no proposal
    docs = [_path(f"0x{i}", f"T{i}", t0 + i, [1e-9 * (1 + g / 100) for g in (0, 5, 12, 30, 40, 40, 40, 40)]) for i in range(6)]
    from datetime import datetime, timezone
    real = [{"mint": f"0x{i}", "pnl_usd": -6.0, "entry_time": datetime.fromtimestamp(t0 + 600 + i + 4.0, tz=timezone.utc).isoformat()} for i in range(6)]
    res = replay.replay_book("rh_pons", docs, cfg, {"ETH": 2500.0}, min_n=1, window_h=24, real_trades=real)
    cal = res["calibration"]
    assert cal and cal["n"] == 6 and cal["sim_usd_per_fill"] > cal["real_usd_per_fill"] and cal["trusted"] is False
    assert res["proposal"] is None and "rosier" in res["note"]


def test_universe_proposal_needs_no_fills():
    cfg = BotConfig().model_dump()
    extra = {"replay": {"rh_pons": {"proposal": {"key": "rh_min_unique_buyers", "value": 5, "gain": 0.4, "reason": "x",
                                                  "evidence": {"n_tokens": 100, "current": {}, "whatif": {}, "missed_winners_n": 3, "dodged_rugs_n": 0}}}}}
    prop, analysis = propose_technique(cfg, {"rh_pons": [], "momentum": [], "greylist_snipe": [], "reentry": []}, 15, extra)
    assert prop and prop["key"] == "rh_min_unique_buyers" and prop["value"] == 5 and prop["evidence"]["universe"]
    assert "autopsy" in analysis and "replay" in analysis and analysis["computed_at"]


class _State:
    def __init__(self):
        from collections import deque
        now = time.time()
        b = {"symbol": "TST", "start": now - 30, "first_price_quote": 1e-9, "quote_symbol": "ETH", "graduated": False, "curve_fill_pct": 12.0,
             "usd_market_cap": 5000.0, "creator": "0xc", "price_samples": deque([(now - 20, 1e-9), (now - 10, 1.1e-9)]),
             "buy_events": deque([(now - 21, 0.01, "0x" + "a" * 40)])}
        self.rh_discovery = type("D", (), {"tracking": {"0xtok": b}})()
        self.tracking = {}
        self.db = None


def test_tick_store_ops_are_incremental():
    st = _State()
    ts = tick_store.TickStore(st)
    ops = ts._ops(time.time())
    assert len(ops) == 1
    doc = ops[0]._doc
    assert doc["$set"]["symbol"] == "TST" and doc["$set"]["chain"] == "rh" and len(doc["$push"]["samples"]["$each"]) == 2
    assert doc["$push"]["buys"]["$each"][0][1] == 0.01 and len(doc["$push"]["buys"]["$each"][0][2]) == 8
    assert ts._ops(time.time()) == []                                       # nothing new → no write
    st.rh_discovery.tracking["0xtok"]["price_samples"].append((time.time(), 1.2e-9))
    ops = ts._ops(time.time())
    assert len(ops) == 1 and len(ops[0]._doc["$push"]["samples"]["$each"]) == 1 and "buys" not in ops[0]._doc["$push"]
