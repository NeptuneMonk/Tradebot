"""Learning policy loop — proposals, canary, gates, sizing helper."""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

import doctor_learning as dl  # noqa: E402
from models import BotConfig  # noqa: E402

NOW = datetime.now(timezone.utc)


SOL_USD = 150.0


def t(pnl_sol, book="momentum", mfe=None, pnl_pct=None, reason="trailing-stop hit", hold=40, exit_ago_s=600, dec=None, fill=None, **extra):
    d = {
        "book": book, "classifier_action": "greylist_snipe" if book == "greylist_snipe" else "momentum_new",
        "pnl_sol": pnl_sol, "pnl_usd": pnl_sol * SOL_USD, "pnl_pct": pnl_pct if pnl_pct is not None else pnl_sol * 1000,
        "exit_reason": reason, "status": "closed",
        "entry_time": (NOW - timedelta(seconds=exit_ago_s + hold)).isoformat(),
        "exit_time": (NOW - timedelta(seconds=exit_ago_s)).isoformat(),
        "entry_price_sol": 1e-6,
    }
    if mfe is not None:
        d["peak_price_sol"] = 1e-6 * (1 + mfe / 100)
    if dec is not None:
        d["decision_price_sol"], d["fill_price_sol"] = dec, fill
    d.update(extra)
    return d


class Col:
    def __init__(self):
        self.doc = None
        self.sets = []

    async def find_one(self, *a, **k):
        return dict(self.doc) if self.doc else None

    async def update_one(self, flt, upd, upsert=False):
        self.sets.append(upd.get("$set", {}))
        self.doc = {**(self.doc or {}), **upd.get("$set", {})}

    async def update_many(self, *a, **k):
        pass

    async def delete_many(self, *a, **k):
        self.doc = None


class BL(Col):
    def __init__(self):
        super().__init__()
        self.items = {}

    async def find_one(self, flt, *a, **k):
        return self.items.get(flt.get("fingerprint"))

    async def update_one(self, flt, upd, upsert=False):
        self.items[flt["fingerprint"]] = upd["$set"]


def engine():
    db = type("DB", (), {})()
    db.bot_config, db.doctor_canary, db.doctor_blacklist = Col(), Col(), BL()
    e = dl.LearningEngine(db)
    e.reloads = 0

    async def rl():
        e.reloads += 1
    e.reload_cb = rl
    return e, db


def test_negative_snipe_expectancy_disables_book():
    trades = [t(-0.004, "greylist_snipe") for _ in range(12)] + [t(0.001, "greylist_snipe") for _ in range(4)]
    stats = {"greylist_snipe": dl.book_stats(trades), "momentum": {"n": 0}}
    p = dl.propose({"book_snipe_size_mult": 1.0, "greylist_snipe_min_score": 45}, stats, 15)
    assert p and p["key"] == "book_snipe_size_mult" and p["value"] == 0.0 and p["book"] == "greylist_snipe"
    # already disabled → nothing
    assert dl.propose({"book_snipe_size_mult": 0.0}, stats, 15) is None


def test_slightly_negative_large_snipe_sample_raises_score_first():
    trades = [t(-0.0005, "greylist_snipe") for _ in range(30)] + [t(0.0004, "greylist_snipe") for _ in range(20)]
    stats = {"greylist_snipe": dl.book_stats(trades), "momentum": {"n": 0}}
    p = dl.propose({"book_snipe_size_mult": 1.0, "greylist_snipe_min_score": 45}, stats, 15)
    assert p["key"] == "greylist_snipe_min_score" and p["value"] == 50.0


def test_giveback_rule_tightens_trail():
    trades = [t(0.002, "momentum", mfe=30, pnl_pct=12) for _ in range(16)]
    stats = {"momentum": dl.book_stats(trades), "greylist_snipe": {"n": 0}}
    p = dl.propose({"trailing_stop_pct": 6, "book_momentum_size_mult": 1.0}, stats, 15)
    assert p["key"] == "trailing_stop_pct" and p["value"] == 5.0
    assert dl.propose({"trailing_stop_pct": 4}, stats, 15) is None  # floor


def test_stale_and_latency_rules():
    trades = [t(-0.001, "greylist_snipe", reason="stale-snipe 60s") for _ in range(9)] + [t(0.003, "greylist_snipe", mfe=5, pnl_pct=3) for _ in range(7)]
    stats = {"greylist_snipe": dl.book_stats(trades), "momentum": {"n": 0}}
    p = dl.propose({"greylist_snipe_stale_seconds": 60}, stats, 15)
    assert p["key"] == "greylist_snipe_stale_seconds" and p["value"] == 45
    lat = [t(0.001, "momentum", mfe=5, pnl_pct=3, dec=1.1e-6, fill=1.0e-6) for _ in range(16)]  # 10pp tax
    stats = {"momentum": dl.book_stats(lat), "greylist_snipe": {"n": 0}}
    assert dl.propose({"speed_mode": "eco"}, stats, 15)["value"] == "fast"
    p = dl.propose({"speed_mode": "fast", "scanner_interval_s": 15, "take_profit_pct": 20}, stats, 15)
    assert p["key"] == "scanner_interval_s" and p["value"] == 20  # never loosens TP


def test_canary_revert_restores_prior_subset_and_blacklists():
    e, db = engine()
    db.bot_config.doc = {"book_snipe_size_mult": 1.0}
    cfg = {"book_snipe_size_mult": 1.0}
    prop = {"type": "flag", "book": "greylist_snipe", "key": "book_snipe_size_mult", "value": 0.0,
            "reason": "r", "expected_direction": "d", "evidence": {}}
    asyncio.run(e.apply(prop, cfg, {"greylist_snipe": {"n": 20, "expectancy_sol": -0.002, "max_drawdown_sol": 0.05}}))
    assert cfg["book_snipe_size_mult"] == 0.0 and db.bot_config.doc["book_snipe_size_mult"] == 0.0
    assert db.doctor_canary.doc["state"] == "running" and db.doctor_canary.doc["baseline_config_subset"] == {"book_snipe_size_mult": 1.0}
    # second apply blocked while running
    try:
        asyncio.run(e.apply(prop, cfg))
        assert False
    except RuntimeError:
        pass
    asyncio.run(e.revert("manual"))
    assert db.bot_config.doc["book_snipe_size_mult"] == 1.0
    assert db.doctor_canary.doc["state"] == "reverted" and e.reloads == 2
    assert asyncio.run(e._blacklisted(dl.fingerprint(prop))) is True


def test_canary_promote_and_auto_revert_on_worse_expectancy():
    e, db = engine()
    base = {"state": "running", "proposal": {"key": "trailing_stop_pct", "value": 5.0, "book": "momentum"}, "book": "momentum",
            "baseline_config_subset": {"trailing_stop_pct": 6.0}, "started_at": (NOW - timedelta(hours=1)).isoformat(),
            "baseline_expectancy_sol": 0.001, "baseline_max_drawdown_sol": 0.01}
    cfg = {"doctor_learning_canary_trades": 12, "doctor_learning_canary_hours": 6.0}
    db.doctor_canary.doc = dict(base)
    good = [t(0.003, "momentum", exit_ago_s=60) for _ in range(12)]
    asyncio.run(e._evaluate_canary(dict(base), cfg, good))
    assert db.doctor_canary.doc["state"] == "promote"
    db.doctor_canary.doc = dict(base)
    db.bot_config.doc = {"trailing_stop_pct": 5.0}
    bad = [t(-0.002, "momentum", exit_ago_s=60) for _ in range(12)]
    asyncio.run(e._evaluate_canary(dict(base), cfg, bad))
    assert db.doctor_canary.doc["state"] == "reverted" and db.bot_config.doc["trailing_stop_pct"] == 6.0
    # not enough trades / time → untouched
    db.doctor_canary.doc = dict(base)
    asyncio.run(e._evaluate_canary(dict(base), cfg, bad[:3]))
    assert db.doctor_canary.doc["state"] == "running"


def test_live_without_auto_apply_live_blocks_auto_apply():
    A = dl.LearningEngine.auto_apply_allowed
    assert A({"doctor_auto_apply_enabled": True, "live_trading": True, "doctor_auto_apply_live": False}) is False
    assert A({"doctor_auto_apply_enabled": True, "live_trading": True, "doctor_auto_apply_live": True}) is True
    assert A({"doctor_auto_apply_enabled": True, "live_trading": False}) is True
    assert A({"doctor_auto_apply_enabled": False}) is False
    c = BotConfig()
    assert c.doctor_auto_apply_enabled is False and c.doctor_auto_apply_live is False and c.doctor_learning_enabled is True


def test_cycle_does_not_auto_apply_when_live_and_emits_learning_suggestion():
    e, db = engine()
    db.bot_config.doc = {}
    cfg = {"doctor_learning_enabled": True, "doctor_learning_min_trades_per_book": 15, "doctor_auto_apply_enabled": True,
           "live_trading": True, "doctor_auto_apply_live": False, "book_snipe_size_mult": 1.0}
    trades = [t(-0.004, "greylist_snipe") for _ in range(16)]
    out = asyncio.run(e.cycle(cfg, trades, trades))
    assert len(out) == 1 and out[0]["category"] == "learning" and out[0]["actions"] == {"book_snipe_size_mult": 0.0}
    assert out[0].get("status") != "applied" and db.doctor_canary.doc is None and cfg["book_snipe_size_mult"] == 1.0


def test_size_mult_zero_skips_book():
    assert dl.book_size_mult(BotConfig(book_snipe_size_mult=0.0), "greylist_snipe") == 0.0
    assert dl.book_size_mult(BotConfig(book_snipe_size_mult=0.0), "momentum_new") == 1.0
    assert dl.book_size_mult({"book_momentum_size_mult": 0.5}, "reentry") == 0.5
    assert dl.book_size_mult({}, "momentum_new") == 1.0
    assert dl.book_of({"chain": "rh", "classifier_action": "rh_pons_paper"}) == "rh_pons"
    assert dl.book_of({"classifier_action": "reentry"}) == "reentry"
    assert dl.book_of({"classifier_action": "manual"}) is None and dl.book_of({"classifier_action": "rh_pons_manual"}) is None
    assert dl.book_of({"classifier_action": "greylist_snipe"}) == "greylist_snipe"
    assert dl.book_of({"book": "momentum", "classifier_action": "greylist_snipe"}) == "momentum"


def test_whitelist_never_contains_forbidden_keys():
    assert not (dl.ALLOWED_KEYS & dl.FORBIDDEN_KEYS)
    assert "max_trade_usd" not in dl.ALLOWED_KEYS and "live_trading" not in dl.ALLOWED_KEYS


def _re(pnl, trig, **k):
    return t(pnl, "reentry", classifier_action="reentry", reentry_trigger=trig, **k)


def test_reentry_book_cuts_losing_trigger_before_disabling():
    trades = [_re(-0.002, "breakout") for _ in range(10)] + [_re(0.001, "pullback") for _ in range(8)]
    stats = {"reentry": dl.book_stats(trades)}
    assert stats["reentry"]["by_trigger"]["breakout"]["expectancy_usd"] < 0 < stats["reentry"]["by_trigger"]["pullback"]["expectancy_usd"]
    p = dl.propose({"reentry_enabled": True, "reentry_breakout_pct": 5.0}, stats, 15)
    assert p["key"] == "reentry_breakout_pct" and p["value"] == 15.0 and p["book"] == "reentry"
    # pullbacks losing → raise the run-on bar, then buyers, then switch off
    trades = [_re(-0.002, "pullback") for _ in range(16)]
    stats = {"reentry": dl.book_stats(trades)}
    assert dl.propose({"reentry_enabled": True, "reentry_min_bounce_pct": 5.0}, stats, 15)["key"] == "reentry_min_bounce_pct"
    assert dl.propose({"reentry_enabled": True, "reentry_min_bounce_pct": 30.0, "reentry_min_buyers": 2}, stats, 15)["key"] == "reentry_min_buyers"
    p = dl.propose({"reentry_enabled": True, "reentry_min_bounce_pct": 30.0, "reentry_min_buyers": 5}, stats, 15)
    assert p["key"] == "reentry_enabled" and p["value"] is False
    assert dl.propose({"reentry_enabled": False}, stats, 15) is None


def test_rh_book_is_audited_and_tightens_gates():
    trades = [t(-0.001, "rh_pons", classifier_action="rh_pons_paper", chain="rh") for _ in range(16)]
    for x in trades:
        x["pnl_usd"] = -0.2
    stats = {"rh_pons": dl.book_stats(trades)}
    p = dl.propose({"rh_min_growth_pct": 30.0}, stats, 15)
    assert p["key"] == "rh_min_growth_pct" and p["value"] == 40.0 and p["book"] == "rh_pons"
    assert dl.propose({"rh_min_growth_pct": 100.0, "rh_min_inflow_usd": 300.0}, stats, 15)["key"] == "rh_min_inflow_usd"


def test_global_profit_shape_rules_sl_and_tp():
    # winners small, losers big, mostly stop-loss exits → tighten SL
    trades = [t(0.001, reason="take_profit") for _ in range(8)] + [t(-0.004, reason="stop-loss hit (-20%)") for _ in range(8)]
    stats = {"momentum": {"n": 0}, "global": dl.book_stats(trades, {"take_profit_pct": 45})}
    p = dl.propose({"stop_loss_pct": 20.0}, stats, 15)
    assert p["key"] == "stop_loss_pct" and p["value"] == 17.0 and p["book"] == "global"
    assert dl.propose({"stop_loss_pct": 10.0}, stats, 15) is None  # floor
    # winners run far past TP and most exits are TP → let winners run
    trades = [t(0.002, mfe=90, pnl_pct=45, reason="take_profit") for _ in range(16)]
    stats = {"momentum": {"n": 0}, "global": dl.book_stats(trades, {"take_profit_pct": 45})}
    p = dl.propose({"take_profit_pct": 45.0, "stop_loss_pct": 20.0}, stats, 15)
    assert p["key"] == "take_profit_pct" and p["value"] == 50.0


def test_scales_a_profitable_book():
    trades = [t(0.002, mfe=6, pnl_pct=5) for _ in range(24)] + [t(-0.001, mfe=1, pnl_pct=-1) for _ in range(8)]
    st = dl.book_stats(trades)
    st["expectancy_7d"], st["n_7d"] = 0.1, 60
    p = dl.propose({"book_momentum_size_mult": 1.0, "trailing_stop_pct": 6}, {"momentum": st}, 15)
    assert p["key"] == "book_momentum_size_mult" and p["value"] == 1.25
    assert dl.propose({"book_momentum_size_mult": 2.0}, {"momentum": st}, 15) is None  # cap


def test_book_stats_is_usd_based_and_exit_classes():
    rows = [t(0.001, reason="take_profit hit"), t(-0.002, reason="stop-loss hit (-20%)"), {"pnl_sol": 1.0, "status": "closed"}]
    st = dl.book_stats(rows)
    assert st["n"] == 2 and abs(st["expectancy_usd"] - (-0.0005 * SOL_USD)) < 1e-9
    assert st["sl_share"] == 50.0 and st["tp_share"] == 50.0 and st["payoff_ratio"] == 0.5
