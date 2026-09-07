"""Doctor v3 — technique first: per-book exits, counterfactual exit grid, data-driven entry filters."""
import asyncio
import time
from datetime import datetime, timedelta, timezone

import book_params as bp
import doctor_learning as dl
from models import BotConfig


def _t(pnl_usd, mfe, mae, book="rh_pons", entry_usd=10.0, peak_first=True, ctx=None, hold=60):
    now = datetime.now(timezone.utc)
    ep = 1e-9
    return {"book": book, "chain": "rh" if book == "rh_pons" else "sol", "classifier_action": "rh_pons" if book == "rh_pons" else book,
            "mode": "paper", "status": "closed", "pnl_usd": pnl_usd, "pnl_pct": pnl_usd / entry_usd * 100, "entry_usd": entry_usd,
            "entry_price_quote": ep, "peak_price_quote": ep * (1 + mfe / 100), "trough_price_quote": ep * (1 + mae / 100),
            "exit_price_quote": ep * (1 + pnl_usd / entry_usd), "peak_ts": 1.0 if peak_first else 2.0, "trough_ts": 2.0 if peak_first else 1.0,
            "entry_time": (now - timedelta(seconds=hold)).isoformat(), "exit_time": now.isoformat(), "entry_ctx": ctx or {}}


def test_exit_param_prefers_book_override_then_global_then_default():
    cfg = BotConfig(take_profit_pct=30.0, book_exits={"rh_pons": {"take_profit_pct": 18.0}})
    assert bp.exit_param(cfg, "rh_pons", "take_profit_pct") == 18.0
    assert bp.exit_param(cfg, "momentum", "take_profit_pct") == 30.0      # SOL untouched by the RH override
    assert bp.exit_param({"book_exits": {}}, "momentum", "stop_loss_pct") == BotConfig().stop_loss_pct
    assert bp.book_for_action("scanner_momentum") == "momentum" and bp.book_for_action("x", chain="rh") == "rh_pons"


def test_whatif_grid_finds_the_tp_the_fills_actually_supported():
    # 20 fills: every one ran to +40% MFE but the book's TP is 60 so they all faded and closed at -5%
    trades = [_t(-0.5, mfe=40, mae=-8) for _ in range(20)]
    cur = {"take_profit_pct": 60.0, "stop_loss_pct": 25.0, "hold_max_seconds": 60.0, "trailing_stop_pct": 50.0, "trailing_arm_pct": 12.0}
    wi = bp.whatif_exits(trades, cur)
    assert wi["n"] == 20 and wi["current"]["expectancy_usd"] == -0.5
    assert wi["best"]["param"] == "take_profit_pct" and wi["best"]["value"] == 40
    assert abs(wi["best"]["expectancy_usd"] - 10.0 * (40 - 2) / 100) < 1e-9   # +38% net of 2% fees on $10
    assert wi["gain_usd_per_fill"] > 4.0 and wi["mae_recorded"]


def test_whatif_respects_path_order_and_sl_candidates():
    # trough came first and went to -30%: with SL 12 these become -12% losses, TP is never reached first
    trades = [_t(2.0, mfe=25, mae=-30, peak_first=False) for _ in range(12)]
    cur = {"take_profit_pct": 20.0, "stop_loss_pct": 40.0, "hold_max_seconds": 60.0, "trailing_stop_pct": 50.0, "trailing_arm_pct": 12.0}
    wi = bp.whatif_exits(trades, cur)
    sl12 = next(r for r in wi["rows"] if r["param"] == "stop_loss_pct" and r["value"] == 12)
    assert abs(sl12["expectancy_usd"] - (-10.0 * (12 + 2) / 100)) < 1e-9
    base = wi["current"]["expectancy_usd"]
    assert abs(base - 10.0 * (20 - 2) / 100) < 1e-9   # SL 40 never hit → TP 20 fills


def test_entry_feature_split_proposes_measured_threshold():
    cfg = BotConfig(rh_min_inflow_usd=300.0)
    trades = [_t(-0.4, 5, -20, ctx={"inflow_usd": 300 + i * 10}) for i in range(10)] + \
             [_t(+0.6, 30, -5, ctx={"inflow_usd": 900 + i * 10}) for i in range(10)]
    splits = bp.entry_feature_splits(trades, "rh_pons", cfg)
    s = next(x for x in splits if x["key"] == "rh_min_inflow_usd")
    assert s["actionable"] and s["low_expectancy_usd"] < 0 < s["high_expectancy_usd"] and 390 <= s["split"] <= 910
    assert s["gain_usd_per_fill"] > 0.4


def test_technique_beats_size_cut_and_targets_the_right_book():
    cfg = BotConfig(take_profit_pct=60.0, book_snipe_size_mult=1.0).model_dump()
    rh = [_t(-0.5, mfe=40, mae=-8) for _ in range(20)]
    by_book = {"rh_pons": rh, "momentum": [], "greylist_snipe": [], "reentry": []}
    stats = {"rh_pons": dl.book_stats(rh), "momentum": {"n": 0}, "greylist_snipe": {"n": 0}, "reentry": {"n": 0}, "global": dl.book_stats(rh)}
    p = dl.propose(cfg, stats, 15, by_book)
    assert p["technique"] and p["key"] == "book_exits.rh_pons.take_profit_pct" and p["value"] == 40 and p["book"] == "rh_pons"
    assert dl.key_ok(p["key"]) and not dl.key_ok("book_exits.rh_pons.live_trading") and not dl.key_ok("max_trade_usd")
    # without trade paths the ladder falls through to the old rules — and the size cut needs 3× the sample
    assert dl.propose(cfg, stats, 15) is None


def test_apply_and_revert_nested_book_key():
    from tests.test_doctor_learning import engine
    e, db = engine()
    db.bot_config.doc = {}
    cfg = BotConfig().model_dump()
    prop = {"type": "threshold", "book": "rh_pons", "key": "book_exits.rh_pons.take_profit_pct", "value": 40,
            "reason": "r", "expected_direction": "d", "evidence": {}}
    can = asyncio.run(e.apply(prop, cfg, {"rh_pons": {"n": 20, "expectancy_usd": -0.5, "max_drawdown_usd": 10}}, auto=True))
    assert cfg["book_exits"]["rh_pons"]["take_profit_pct"] == 40
    assert can["baseline_config_subset"] == {"book_exits.rh_pons.take_profit_pct": None}
    assert db.bot_config.doc.get("book_exits.rh_pons.take_profit_pct") == 40 or db.bot_config.doc.get("book_exits", {}).get("rh_pons", {}).get("take_profit_pct") == 40
    done = asyncio.run(e.revert("manual"))
    assert done["state"] == "reverted"


def test_trailing_and_arm_grid_models_giveback_from_peak():
    # fills ran to +40% then gave back to +5% (35% off the peak). trail 5% armed at 10% would have kept ~+33%
    trades = [_t(0.5, mfe=40, mae=-3) for _ in range(15)]
    cur = {"take_profit_pct": 100.0, "stop_loss_pct": 40.0, "trailing_stop_pct": 50.0, "trailing_arm_pct": 12.0, "hold_max_seconds": 60.0}
    wi = bp.whatif_exits(trades, cur)
    params = {r["param"] for r in wi["rows"]}
    assert {"trailing_stop_pct", "trailing_arm_pct"} <= params
    t5 = next(r for r in wi["rows"] if r["param"] == "trailing_stop_pct" and r["value"] == 5)
    assert abs(t5["expectancy_usd"] - 10.0 * (((1.4 * 0.95) - 1) * 100 - 2) / 100) < 1e-9
    t3 = next(r for r in wi["rows"] if r["param"] == "trailing_stop_pct" and r["value"] == 3)
    assert t3["expectancy_usd"] > t5["expectancy_usd"] > 0.5      # tighter trail keeps more of the +40% run
    assert wi["best"]["param"] == "take_profit_pct" and wi["best"]["value"] == 40   # …but banking the peak beats both


def test_trail_does_not_fire_when_giveback_smaller_than_trail():
    trades = [_t(3.5, mfe=40, mae=-3) for _ in range(10)]        # exited at +35%: only 3.6% off the peak
    cur = {"take_profit_pct": 100.0, "stop_loss_pct": 40.0, "trailing_stop_pct": 10.0, "trailing_arm_pct": 12.0, "hold_max_seconds": 60.0}
    wi = bp.whatif_exits(trades, cur)
    assert abs(wi["current"]["expectancy_usd"] - 3.5) < 1e-9


def test_pair_split_used_only_when_single_split_is_not_clean():
    cfg = BotConfig(rh_min_inflow_usd=100.0, rh_min_unique_buyers=2).model_dump()
    # winners need BOTH high inflow and many buyers; either alone loses → single splits are muddy
    trades = []
    for i in range(8):
        trades.append(_t(+0.8, 30, -5, ctx={"inflow_usd": 900 + i, "unique_buyers": 20 + i}))   # high-high wins
        for _ in range(2):
            trades.append(_t(-0.5, 5, -20, ctx={"inflow_usd": 900 + i, "unique_buyers": 3 + i % 2}))  # high inflow, few buyers
            trades.append(_t(-0.5, 5, -20, ctx={"inflow_usd": 200 + i, "unique_buyers": 20 + i}))     # many buyers, low inflow
        trades.append(_t(-0.5, 5, -20, ctx={"inflow_usd": 200 + i, "unique_buyers": 3 + i % 2}))
    singles = bp.entry_feature_splits(trades, "rh_pons", cfg)
    assert not any(s["actionable"] for s in singles)
    pairs = bp.entry_pair_splits(trades, "rh_pons", cfg)
    top = pairs[0]
    assert top["actionable"] and set(top["features"]) == {"inflow_usd", "unique_buyers"}
    assert top["high_high_expectancy_usd"] > 0 > top["rest_expectancy_usd"]
    by_book = {"rh_pons": trades, "momentum": [], "greylist_snipe": [], "reentry": []}
    prop, analysis = dl.propose_technique(cfg, by_book, 15)
    assert prop and prop.get("actions") and set(prop["actions"]) == {"rh_min_inflow_usd", "rh_min_unique_buyers"}
    assert analysis["rh_pons"]["pairs"][0]["actionable"]


def test_apply_and_revert_two_key_proposal():
    from tests.test_doctor_learning import engine
    e, db = engine()
    db.bot_config.doc = {}
    cfg = BotConfig(rh_min_inflow_usd=100.0, rh_min_unique_buyers=2).model_dump()
    prop = {"type": "threshold", "book": "rh_pons", "key": "rh_min_inflow_usd+rh_min_unique_buyers",
            "value": {"rh_min_inflow_usd": 550.0, "rh_min_unique_buyers": 11}, "actions": {"rh_min_inflow_usd": 550.0, "rh_min_unique_buyers": 11},
            "reason": "r", "expected_direction": "d", "evidence": {}}
    can = asyncio.run(e.apply(prop, cfg, {"rh_pons": {"n": 32, "expectancy_usd": -0.1, "max_drawdown_usd": 5}}, auto=True))
    assert cfg["rh_min_inflow_usd"] == 550.0 and cfg["rh_min_unique_buyers"] == 11
    assert can["baseline_config_subset"] == {"rh_min_inflow_usd": 100.0, "rh_min_unique_buyers": 2}
    done = asyncio.run(e.revert("manual"))
    assert done["state"] == "reverted"
