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


def t(pnl_sol, book="momentum", mfe=None, pnl_pct=None, reason="trailing-stop hit", hold=40, exit_ago_s=600, dec=None, fill=None):
    d = {
        "book": book, "classifier_action": "greylist_snipe" if book == "greylist_snipe" else "momentum_new",
        "pnl_sol": pnl_sol, "pnl_pct": pnl_pct if pnl_pct is not None else pnl_sol * 1000,
        "exit_reason": reason, "status": "closed",
        "entry_time": (NOW - timedelta(seconds=exit_ago_s + hold)).isoformat(),
        "exit_time": (NOW - timedelta(seconds=exit_ago_s)).isoformat(),
        "entry_price_sol": 1e-6,
    }
    if mfe is not None:
        d["peak_price_sol"] = 1e-6 * (1 + mfe / 100)
    if dec is not None:
        d["decision_price_sol"], d["fill_price_sol"] = dec, fill
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
    assert A({"doctor_auto_apply_enabled": True, "live_trading": False, "doctor_advisory_only": True}) is False
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
    assert dl.book_of({"chain": "rh"}) is None
    assert dl.book_of({"classifier_action": "greylist_snipe"}) == "greylist_snipe"
    assert dl.book_of({"book": "momentum", "classifier_action": "greylist_snipe"}) == "momentum"


def test_whitelist_never_contains_forbidden_keys():
    assert not (dl.ALLOWED_KEYS & dl.FORBIDDEN_KEYS)
    assert "max_trade_usd" not in dl.ALLOWED_KEYS and "live_trading" not in dl.ALLOWED_KEYS
