"""Strategy Doctor — MFE-based, chain-scoped rules + stale-card handling + load() re-entrancy."""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

import strategy_doctor as sd  # noqa: E402

NOW = datetime.now(timezone.utc)


def trade(pnl_pct, mfe, reason, chain="sol", hold=40, entry_usd=5.0, action="momentum_new"):
    ep = 1e-6
    t = {
        "chain": chain, "pnl_pct": pnl_pct, "pnl_usd": entry_usd * pnl_pct / 100, "entry_usd": entry_usd,
        "exit_reason": reason, "classifier_action": action,
        "entry_time": (NOW - timedelta(seconds=hold + 60)).isoformat(),
        "exit_time": (NOW - timedelta(seconds=60)).isoformat(),
    }
    if chain == "rh":
        t["entry_price_quote"], t["peak_price_quote"] = ep, ep * (1 + mfe / 100)
    else:
        t["entry_price_sol"], t["peak_price_sol"] = ep, ep * (1 + mfe / 100)
    return t


def test_helpers_normalise_both_vocabularies():
    assert sd._exit_class({"exit_reason": "stop-loss hit (-14.4%) [fast]"}) == "sl"
    assert sd._exit_class({"exit_reason": "stop_loss"}) == "sl"
    assert sd._exit_class({"exit_reason": "trailing-stop hit (peak +32%, now +18%)"}) == "trail"
    assert sd._exit_class({"exit_reason": "timeout after 135s"}) == "timeout"
    assert sd._exit_class({"exit_reason": "max_hold"}) == "timeout"
    assert sd._exit_class({"exit_reason": "no-momentum (peak +1.2% after 30s)"}) == "no_momentum"
    assert sd._exit_class({"exit_reason": "classifier exit_early: ['curve filled 59% in 8s']"}) == "churn"
    assert abs(sd._mfe_pct(trade(0, 25, "x")) - 25) < 1e-6
    assert abs(sd._mfe_pct(trade(0, 25, "x", chain="rh")) - 25) < 1e-6
    assert sd._mfe_pct({"chain": "sol"}) is None


def test_sl_too_wide_fires_only_when_wide_and_dominant():
    d = sd.StrategyDoctor(db=None)
    trades = [trade(-65, 2, "stop-loss hit (-62%)") for _ in range(4)] + [trade(8, 15, "trailing-stop hit") for _ in range(10)]
    out = d._rule_sl_too_wide(trades, {"stop_loss_pct": 56}, "sol")
    assert out and out[0]["actions"] == {"stop_loss_pct": 20.0} and "[Solana]" in out[0]["title"]
    assert d._rule_sl_too_wide(trades, {"stop_loss_pct": 20}, "sol") == []
    # SL exits are a minority of losses → no suggestion
    mixed = trades + [trade(-30, 1, "timeout after 90s") for _ in range(20)]
    assert d._rule_sl_too_wide(mixed, {"stop_loss_pct": 56}, "sol") == []


def test_tp_unreachable_uses_p75_of_peaks():
    d = sd.StrategyDoctor(db=None)
    trades = [trade(5, m, "trailing-stop hit") for m in range(10, 50, 2)]  # peaks 10..48
    out = d._rule_tp_unreachable(trades, {"take_profit_pct": 120, "partial_tp_pct": 50}, "sol")
    assert out and 25 <= out[0]["actions"]["take_profit_pct"] <= 45
    # if any TP actually hit, stay quiet
    trades[0]["exit_reason"] = "take-profit hit"
    assert d._rule_tp_unreachable(trades, {"take_profit_pct": 120}, "sol") == []


def test_flat_bleeders_enable_then_tighten():
    d = sd.StrategyDoctor(db=None)
    flat = [trade(-25, 1, "timeout after 135s", hold=135) for _ in range(6)]
    out = d._rule_flat_bleeders(flat, {"no_momentum_exit_enabled": False}, "rh")
    assert out and out[0]["actions"]["no_momentum_exit_enabled"] is True and "[RH" in out[0]["title"]
    out = d._rule_flat_bleeders(flat, {"no_momentum_exit_enabled": True, "no_momentum_after_s": 30}, "rh")
    assert out and out[0]["actions"] == {"no_momentum_after_s": 20}
    assert d._rule_flat_bleeders(flat, {"no_momentum_exit_enabled": True, "no_momentum_after_s": 15}, "rh") == []


def test_trailing_giveback_and_churn_and_source_edge():
    d = sd.StrategyDoctor(db=None)
    runners = [trade(10, 40, "trailing-stop hit") for _ in range(10)]  # give back 75%
    out = d._rule_trailing_giveback(runners, {"trailing_stop_pct": 6, "trailing_arm_pct": 12}, "sol")
    assert out and out[0]["actions"] == {"trailing_stop_pct": 4.0}
    assert d._rule_trailing_giveback([trade(35, 40, "trailing-stop hit") for _ in range(10)], {"trailing_stop_pct": 6}, "sol") == []
    churn = [trade(1, 0, "classifier exit_early: ['curve filled 60% in 8s']", hold=6) for _ in range(9)] + [trade(5, 10, "trailing-stop hit") for _ in range(10)]
    out = d._rule_churn_exits(churn, {}, "sol")
    assert out and out[0]["actions"] == {} and out[0]["metrics"]["n_churn"] == 9
    re = [trade(15, 20, "trailing-stop hit", action="reentry") for _ in range(6)] + [trade(-5, 3, "timeout", action="momentum_new") for _ in range(10)]
    out = d._rule_source_edge(re, {"reentry_size_multiplier": 0.5}, "sol")
    assert out and out[0]["actions"] == {"reentry_size_multiplier": 1.0}
    assert d._rule_source_edge(re, {"reentry_size_multiplier": 1.0}, "sol") == []
    assert d._rule_source_edge(re, {"reentry_size_multiplier": 0.5}, "rh") == []


def test_take_profit_frequency_rule_is_data_driven():
    d = sd.StrategyDoctor(db=None)
    trades = [trade(5, m, "trailing-stop hit") for m in range(5, 65, 1)]  # 60 trades, peaks 5..64
    out = d._rule_take_profit_frequency(trades, {"take_profit_pct": 120})
    assert out and 15 <= out[0]["actions"]["take_profit_pct"] < 120 and out[0]["actions"]["take_profit_pct"] != 116


class _Coll:
    def __init__(self, docs=None):
        self.docs = docs or []
        self.calls = []

    async def update_many(self, flt, upd):
        self.calls.append((flt, upd))

    async def insert_many(self, docs):
        self.docs.extend(docs)

    def find(self, flt, proj=None):
        docs = self.docs
        class _C:
            async def to_list(self_inner, n):
                return list(docs)
            def __aiter__(self_inner):
                async def gen():
                    for d in docs:
                        yield d
                return gen()
        return _C()

    async def find_one(self, *a, **k):
        return {}


def test_run_once_retires_needs_more_data_card_when_enough_trades():
    trades = [trade(5, 10, "trailing-stop hit") for _ in range(35)]
    db = type("DB", (), {})()
    db.strategy_suggestions = _Coll()
    db.bot_config = _Coll()
    db.trades = _Coll(trades)
    d = sd.StrategyDoctor(db=db)
    asyncio.run(d.run_once())
    retired = [c for c in db.strategy_suggestions.calls
               if c[0].get("category") == "needs_more_data" and c[1]["$set"].get("status") == "expired"]
    assert retired, "pending needs_more_data card must be expired once data is sufficient"


def test_bot_load_is_reentrant():
    src = open(os.path.join(os.path.dirname(__file__), "..", "bot.py")).read()
    i = src.index("first_load = not getattr(self, \"_initial_load_done\", False)")
    seg = src[i:i + 9000]
    assert "was_running_before_restart = False" in seg
    assert seg.index("if first_load:\n            asyncio.create_task(self._active_trades_reconciler_loop())") > 0
    # the active-trade restore/respawn block is guarded
    j = seg.index("# Restart-only work.")
    assert "if first_load:" in seg[j:j + 600]
    assert "            for mint in list(self.active_trades.keys()):\n                asyncio.create_task(self._monitor_position(mint))" in seg


class _Sugg(_Coll):
    async def update_one(self, flt, upd):
        for d in self.docs:
            if d.get("id") == flt.get("id"):
                d.update(upd.get("$set", {}))
        self.calls.append((flt, upd))

    def find(self, flt, proj=None):
        docs = [d for d in self.docs if all(
            (d.get(k) == v) if not isinstance(v, dict) else (d.get(k) != v.get("$ne")) for k, v in flt.items())]
        class _C:
            def __aiter__(self_inner):
                async def gen():
                    for d in docs:
                        yield d
                return gen()
            async def to_list(self_inner, n):
                return list(docs)
        return _C()


class _Cfg(_Coll):
    def __init__(self, doc):
        super().__init__()
        self.doc = doc

    async def update_one(self, flt, upd):
        self.doc.update(upd.get("$set", {}))

    async def find_one(self, *a, **k):
        return dict(self.doc)


def _doctor_with(cfg, trades, suggestions=None):
    db = type("DB", (), {})()
    db.strategy_suggestions = _Sugg(suggestions or [])
    db.bot_config = _Cfg(cfg)
    db.trades = _Coll(trades)
    d = sd.StrategyDoctor(db=db)
    d.reloads = 0
    async def reload():
        d.reloads += 1
    d.reload_cb = reload
    return d, db


def test_auto_apply_only_high_confidence_actionable_non_classifier():
    cfg = {"doctor_auto_apply_enabled": True, "stop_loss_pct": 56.0, "take_profit_pct": 120.0}
    trades = [trade(5, 10, "trailing-stop hit") for _ in range(20)]
    d, db = _doctor_with(cfg, trades)
    fresh = [
        {"id": "a", "title": "sl", "category": "sl", "confidence": "high", "actions": {"stop_loss_pct": 20.0}},
        {"id": "b", "title": "tp", "category": "tp", "confidence": "med", "actions": {"take_profit_pct": 40.0}},
        {"id": "c", "title": "cls", "category": "classifier", "confidence": "high", "actions": {"classifier_action_whitelist": ["x"]}},
        {"id": "d", "title": "info", "category": "classifier", "confidence": "high", "actions": {}},
    ]
    db.strategy_suggestions.docs = [dict(s, status="pending") for s in fresh]
    asyncio.run(d._auto_apply(fresh, cfg, trades))
    assert cfg["stop_loss_pct"] == 20.0 and cfg["take_profit_pct"] == 120.0 and "classifier_action_whitelist" not in cfg
    a = db.strategy_suggestions.docs[0]
    assert a["status"] == "applied" and a["auto_applied"] is True and a["applied_before"] == {"stop_loss_pct": 56.0}
    assert a["auto_baseline_wr"] == 100.0 and a["auto_baseline_n"] == 20
    assert d.reloads == 1
    # disabled → nothing happens
    cfg2 = {"doctor_auto_apply_enabled": False, "stop_loss_pct": 56.0}
    d2, _ = _doctor_with(cfg2, trades)
    asyncio.run(d2._auto_apply([dict(fresh[0])], cfg2, trades))
    assert cfg2["stop_loss_pct"] == 56.0


def test_watchdog_reverts_on_wr_drop_and_settles_otherwise():
    applied_at = (NOW - timedelta(hours=3)).isoformat()
    base_sugg = {"id": "a", "title": "sl", "category": "sl", "status": "applied", "auto_applied": True,
                 "applied_at": applied_at, "applied_before": {"stop_loss_pct": 56.0}, "auto_baseline_wr": 60.0,
                 "auto_watch_until": (NOW + timedelta(hours=21)).isoformat()}
    # 15 trades since apply, 3 winners → WR 20% (drop 40pp) → revert
    since = [trade(5, 10, "trailing-stop hit") for _ in range(3)] + [trade(-15, 2, "stop-loss hit") for _ in range(12)]
    cfg = {"doctor_auto_revert_min_trades": 12, "doctor_auto_revert_wr_drop_pp": 10.0, "stop_loss_pct": 20.0}
    d, db = _doctor_with(cfg, since, [dict(base_sugg)])
    asyncio.run(d._auto_revert_watchdog(cfg, since))
    s = db.strategy_suggestions.docs[0]
    assert s["status"] == "reverted" and s["auto_reverted"] is True and cfg["stop_loss_pct"] == 56.0 and d.reloads == 1
    # too few trades → untouched
    cfg = {"doctor_auto_revert_min_trades": 12, "doctor_auto_revert_wr_drop_pp": 10.0, "stop_loss_pct": 20.0}
    d, db = _doctor_with(cfg, since[:5], [dict(base_sugg)])
    asyncio.run(d._auto_revert_watchdog(cfg, since[:5]))
    assert db.strategy_suggestions.docs[0]["status"] == "applied" and cfg["stop_loss_pct"] == 20.0
    # WR fine and watch window over → settled, kept
    good = [trade(5, 10, "trailing-stop hit") for _ in range(14)]
    expired = dict(base_sugg, auto_watch_until=(NOW - timedelta(minutes=1)).isoformat())
    cfg = {"doctor_auto_revert_min_trades": 12, "doctor_auto_revert_wr_drop_pp": 10.0, "stop_loss_pct": 20.0}
    d, db = _doctor_with(cfg, good, [expired])
    asyncio.run(d._auto_revert_watchdog(cfg, good))
    s = db.strategy_suggestions.docs[0]
    assert s["status"] == "applied" and s["auto_settled"] is True and cfg["stop_loss_pct"] == 20.0
