"""Profitability refactor — cost gate, R sizing, book ladders, routing, live-doctor policy, scorecard, Doctor rails."""
import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cost_gate
import r_sizer
import exits
import scorecard
from classifier import classify, default_rules, ACTIONS
from live_doctor import decide
from book_params import book_for_action, exit_param, BOOK_DEFAULTS, FIRST_TARGET_R
from doctor_learning import key_ok, ALLOWED_KEYS
from rails import clamp_actions, RAILS
from inventory import InventoryHalt, HALT_N
from models import BotConfig


# ---------------- cost gate ----------------
def test_cost_gate_skips_when_target_cannot_clear_2x_cost():
    # scalp: size $1, r_usd (12% sl + 1.5% slip) = $0.135, first target 1.5R = 20% of size
    # thin pool ($20 depth): a $1 order is 5% impact per side → cost > target/2 → skip
    q = cost_gate.quote(size_usd=1.0, r_usd=0.135, first_target_r=1.5, protocol="pumpfun",
                        entry_slip_bps=2500, exit_slip_bps=1500, fee_usd_round_trip=0.02, ladder=False, depth_usd=20.0)
    assert q["cost_gate_pass"] is False and "cost" in q["cost_gate_reason"]
    # deep pool ($6000): impact negligible → 2 × 1% adverse + 2% protocol + 0.5% shave + fees ≈ 5% < 20%/2
    q2 = cost_gate.quote(size_usd=1.0, r_usd=0.135, first_target_r=1.5, protocol="pumpfun",
                         entry_slip_bps=1000, exit_slip_bps=800, fee_usd_round_trip=0.005, ladder=False, depth_usd=6000.0)
    assert q2["cost_gate_pass"] is True and q2["expected_target_pct"] >= 2 * q2["expected_cost_pct"]


def test_cost_gate_hunt_first_scale_out_must_clear_friction():
    # thin pool ($25 depth): impact alone blows the 8% ceiling
    q = cost_gate.quote(size_usd=1.0, r_usd=0.21, first_target_r=FIRST_TARGET_R["hunt"], protocol="pumpfun",
                        entry_slip_bps=2500, exit_slip_bps=2500, fee_usd_round_trip=0.03, ladder=True, depth_usd=25.0)
    assert q["cost_gate_pass"] is False
    assert q["expected_cost_pct"] > cost_gate.MAX_ROUND_TRIP_PCT


def test_hunt_cost_gate_passes_on_typical_pumpfun_depth():
    # typical curve: 30 SOL ≈ $4,500 depth, $12 hunt entry, 20% SL + slip → r ≈ $2.55, first cash-out = +1R on the 35% leg
    q = cost_gate.quote(size_usd=12.0, r_usd=2.55, first_target_r=FIRST_TARGET_R["hunt"], protocol="pumpfun",
                        entry_slip_bps=1000, exit_slip_bps=800, fee_usd_round_trip=0.04, ladder=True, depth_usd=4500.0, first_leg_frac=0.35)
    assert q["expected_cost_pct"] < cost_gate.MAX_ROUND_TRIP_PCT and q["cost_gate_pass"] is True
    assert q["cost_breakdown"]["shave_pct"] < 1.0        # 1.5% dust on the 35% leg, not 5% of the full bag
    assert cost_gate.LADDER_SHAVE_PCT == 1.5 and cost_gate.MAX_ROUND_TRIP_PCT == 8.0 and cost_gate.COST_MULT == 2.0


# ---------------- R sizing ----------------
def test_r_size_respects_operator_cap_and_reports_actual_r():
    sz = r_sizer.size_trade(bankroll_usd=1000, risk_per_trade_pct=2.0, sl_pct=12.0, exit_slip_pct=1.5,
                            book_mult=1.0, doctor_mult=1.0, governor_mult=1.0, min_trade_usd=0.5, max_trade_usd=1.0)
    assert sz["skip"] is False and sz["size_usd"] == 1.0 and sz["size_clamped"] is True
    assert sz["r_usd_nominal"] == 20.0                      # bankroll × 2%
    assert abs(sz["r_usd"] - 1.0 * 13.5 / 100) < 1e-9       # ACTUAL cash at risk after the clamp


def test_r_size_skips_below_min_trade():
    sz = r_sizer.size_trade(bankroll_usd=10, risk_per_trade_pct=0.25, sl_pct=12.0, exit_slip_pct=1.5,
                            book_mult=0.25, doctor_mult=0.5, governor_mult=1.0, min_trade_usd=0.5, max_trade_usd=1.0)
    assert sz["skip"] is True and sz["size_usd"] == 0.0
    assert r_sizer.size_trade(bankroll_usd=1000, risk_per_trade_pct=2.0, sl_pct=12.0, exit_slip_pct=1.0, book_mult=0.0,
                              doctor_mult=1.0, governor_mult=1.0, min_trade_usd=0.5, max_trade_usd=1.0)["skip"] is True


# ---------------- book ladders ----------------
def _slot(book, pct_peak=0.0, legs=0):
    return {"trade": {"book": book, "entry_price_sol": 1.0, "sl_pct_with_slip": BOOK_DEFAULTS[book]["stop_loss_pct"] + 1.0},
            "peak_price_sol": 1.0 + pct_peak / 100, "ladder_legs_done": legs}


def _fire(b, *_):
    return b


def test_hunt_trade_does_not_exit_on_hold_clock_but_scalp_does():
    cfg = BotConfig()
    hunt = exits.decide_hunt(cfg, _slot("hunt"), 2.0, 1.02, elapsed=3600, sl_fire=_fire, ts_fire=_fire)
    assert hunt.kind is None
    scalp = exits.decide_scalp(cfg, _slot("scalp"), 2.0, 1.02, elapsed=41, sl_fire=_fire, ts_fire=_fire)
    assert scalp.kind == "exit" and "clock" in scalp.reason
    assert exit_param(cfg, "hunt", "hold_max_seconds") == 0


def test_scalp_single_exit_at_target_r_and_minus_1r():
    cfg = BotConfig()
    one_r = 13.0
    tgt = exits.decide_scalp(cfg, _slot("scalp", pct_peak=20), 1.5 * one_r + 0.1, 1.196, 5, _fire, _fire)
    assert tgt.kind == "exit" and "1.5R" in tgt.reason
    sl = exits.decide_scalp(cfg, _slot("scalp"), -12.5, 0.875, 5, _fire, _fire)
    assert sl.kind == "exit" and "stop-loss" in sl.reason
    assert exits.decide_scalp(cfg, _slot("scalp"), 5.0, 1.05, 5, _fire, _fire).kind is None


def test_hunt_ladder_partials_then_breakeven_stop():
    cfg = BotConfig()
    one_r = 21.0
    s = _slot("hunt", pct_peak=one_r + 1)
    leg1 = exits.decide_hunt(cfg, s, one_r + 0.5, 1.215, 30, _fire, _fire)
    assert leg1.kind == "partial" and abs(leg1.fraction - 0.35) < 1e-9
    exits.after_partial(s, expected_exit_cost_pct=2.0)
    assert s["ladder_legs_done"] == 1 and s["ladder_stop_pct"] == 2.0
    # back to +1% (below breakeven + cost) → ladder stop, not −20% SL
    stop = exits.decide_hunt(cfg, s, 1.0, 1.01, 40, _fire, _fire)
    assert stop.kind == "exit" and "ladder stop" in stop.reason
    s["peak_price_sol"] = 1.45
    leg2 = exits.decide_hunt(cfg, s, 2 * one_r + 0.5, 1.425, 50, _fire, _fire)
    assert leg2.kind == "partial" and abs(leg2.fraction - 0.30) < 1e-9


# ---------------- routing / classifier ----------------
def test_classifier_closed_set_and_creator_routing():
    assert set(ACTIONS) == {"scalp", "hunt", "skip"}
    rules = default_rules()
    base = {"elapsed_s": 4, "curve_fill_pct": 5, "unique_buyers": 20, "sol_inflow": 2.0}
    patterned = classify({**base, "creator_rugs": 3, "creator_pattern": "slow_rug_tradeable"}, rules)
    assert patterned["action"] == "hunt"                                    # not aborted — routed
    untradeable = classify({**base, "creator_rugs": 2, "creator_pattern": "unpredictable_rug"}, rules)
    assert untradeable["action"] == "scalp" and untradeable["risk"] == 45   # prior non-graduated launches = risk bump, not a kill
    serial = classify({**base, "creator_rugs": 2, "creator_prior_launches": 5, "creator_graduated_before": False},
                      {**rules, "serial_creator_min_launches": 3})
    assert serial["action"] == "skip" and "serial creator" in serial["reasons"][0]   # the operator's creator gate still bites
    assert classify({**base, "creator_rugs": 0}, rules)["action"] == "scalp"
    late = classify({**base, "creator_rugs": 0, "curve_fill_pct": 45}, rules)
    assert late["action"] == "skip" and "late chase" in late["reasons"][0]
    assert book_for_action("greylist_snipe") == "hunt" and book_for_action("reentry") == "hunt"
    assert book_for_action("momentum_new") == "scalp" and book_for_action("manual") == "scalp"
    assert book_for_action("rh_pons_paper") == "rh_pons"
    assert "creator_rug_threshold" not in rules and "project_score_min" not in rules


# ---------------- live doctor ----------------
def test_live_doctor_total_order_skip_half_full():
    assert decide(39.9, 50) == "skip"            # weak winner that looks MORE like exit liquidity
    assert decide(39.9, 10) == "half" and decide(28, 28) == "half"   # weak but not worse than exit-liq / tie → no signal, half size
    assert decide(50, 80) == "half" and decide(50, 10) == "half"
    assert decide(80, 80) == "half"          # strong winner that also looks like exit liquidity
    assert decide(65, 49.9) == "full"
    assert decide(60, 50) == "half"


# ---------------- scorecard ----------------
class _Cursor:
    def __init__(self, rows): self.rows = rows
    def sort(self, *a): return self
    def limit(self, n): self.rows = self.rows[:n]; return self
    def __aiter__(self):
        async def gen():
            for r in self.rows:
                yield r
        return gen()


class _Coll:
    def __init__(self, rows=None): self.rows = rows or []; self.docs = {}
    def find(self, q=None, *a): return _Cursor(self.rows)
    async def find_one(self, q, *a): return self.docs.get(q["_id"])
    async def update_one(self, q, u, upsert=False): self.docs[q["_id"]] = {**self.docs.get(q["_id"], {}), **u.get("$set", {})}


class _DB:
    def __init__(self, trades):
        self.trades = _Coll(trades); self.scorecard = _Coll()
    def __getitem__(self, name): return getattr(self, name)


def test_scorecard_disables_cell_at_n30_negative_expectancy():
    rows = [{"id": f"t{i}", "book": "scalp", "status": "closed", "pnl_usd": -0.05, "r_usd": 0.13, "entry_time": "2026-09-09T03:00:00+00:00",
             "expected_cost_pct": 4.0, "entry_ctx": {"band": "new"}, "exit_reason": "stop-loss hit", "mode": "paper"} for i in range(30)]
    db = _DB(rows)
    sc = scorecard.Scorecard(db)
    key = asyncio.run(sc.record(rows[-1]))
    assert key == "scalp|none|new|h00|mid"
    assert sc.is_disabled(key) is True and db.scorecard.docs[key]["expectancy_r"] < 0
    rows29 = rows[:29]
    sc2 = scorecard.Scorecard(_DB(rows29))
    k2 = asyncio.run(sc2.record(rows29[-1]))
    assert sc2.is_disabled(k2) is False        # n < 30 never disables
    noise = [{**r, "pnl_usd": -0.005} for r in rows]   # −0.04R: breakeven noise never disables
    sc3 = scorecard.Scorecard(_DB(noise))
    assert sc3.is_disabled(asyncio.run(sc3.record(noise[-1]))) is False
    legacy = [{**r, "r_usd": None} for r in rows]      # pre-migration fills are ignored entirely
    assert scorecard.stats(legacy)["n"] == 0


# ---------------- doctor rails ----------------
def test_doctor_cannot_write_global_take_profit_only_book_scoped():
    assert key_ok("take_profit_pct") is False and key_ok("stop_loss_pct") is False and key_ok("hold_max_seconds") is False
    assert key_ok("book_exits.hunt.target_r") is True and key_ok("book_exits.scalp.hold_max_seconds") is True
    assert key_ok("book_exits.momentum.take_profit_pct") is False
    assert not ({"take_profit_pct", "stop_loss_pct", "trailing_stop_pct", "hold_max_seconds", "no_momentum_min_mfe_pct"} & ALLOWED_KEYS)
    out, notes = clamp_actions({"book_exits.hunt.target_r": 9.0, "max_trade_usd": 50, "max_concurrent_positions": 20}, "doctor")
    assert out["book_exits.hunt.target_r"] == RAILS["target_r"][1] and "max_trade_usd" not in out
    assert out["max_concurrent_positions"] == 8


def test_canary_promotion_uses_post_start_fills_only():
    from doctor_learning import LearningEngine, PROMOTION_MIN_FILLS
    eng = LearningEngine.__new__(LearningEngine)
    calls = {}

    async def _set(doc): calls["set"] = doc
    async def _revert(reason="", verdict=None): calls["revert"] = reason
    eng._set_canary, eng.revert = _set, _revert
    started = "2026-09-09T00:00:00+00:00"
    can = {"started_at": started, "book": "hunt", "baseline_expectancy_r": -0.2, "baseline_max_drawdown_usd": 1.0, "proposal": {"key": "k", "value": 1}}
    before = [{"book": "hunt", "classifier_action": "greylist_snipe", "exit_time": "2026-09-08T23:00:00+00:00", "pnl_usd": 1.0, "r_usd": 0.2, "entry_usd": 1}
              for _ in range(40)]   # huge win BEFORE the canary started — must not count
    asyncio.run(eng._evaluate_canary(can, {}, before))
    assert not calls, "pre-start fills must not promote"
    after = [{"book": "hunt", "classifier_action": "greylist_snipe", "exit_time": "2026-09-09T01:00:00+00:00", "pnl_usd": 0.1, "r_usd": 0.2, "entry_usd": 1,
              "entry_time": "2026-09-09T00:59:00+00:00"} for _ in range(PROMOTION_MIN_FILLS["hunt"])]
    asyncio.run(eng._evaluate_canary(can, {}, after))
    assert calls["set"]["state"] == "promote" and calls["set"]["n_since"] == PROMOTION_MIN_FILLS["hunt"]


# ---------------- inventory halt ----------------
def test_inventory_halt_after_n_loss_exits():
    inv = InventoryHalt()
    for _ in range(HALT_N - 1):
        assert inv.record_close("stop-loss hit (-12%)") is False
    assert inv.active() is False
    assert inv.record_close("rip-cord: curve past expected rug") is True and inv.active() is True
    inv2 = InventoryHalt()
    for _ in range(HALT_N):
        inv2.record_close("target +1.5R hit")
    assert inv2.active() is False


def test_config_has_no_global_exit_keys():
    d = BotConfig().model_dump()
    for k in ("take_profit_pct", "stop_loss_pct", "trailing_stop_pct", "hold_max_seconds", "partial_tp_pct", "winner_ride_enabled", "project_score_min"):
        assert k not in d
    assert d["max_concurrent_positions"] == 3 and "book_scalp_size_mult" in d and "book_momentum_size_mult" not in d


# ---------------- entry planner end-to-end (BotState._plan_entry) ----------------
class _FakeDoctor:
    def __init__(self, decision): self.decision = decision
    def book_adjust(self, book, live): return (1.0, 1.0)
    async def score_launch(self, mint, book="scalp"):
        return {"winner_likeness_pct": 70.0, "exit_liquidity_likeness_pct": 20.0, "doctor_decision": self.decision,
                "doctor_size_mult": {"skip": 0.0, "half": 0.5, "full": 1.0}[self.decision], "reason": "test"}


def _planner(decision="full", disabled_cells=()):
    import os
    os.environ.setdefault("HELIUS_RPC_URL", "http://x"); os.environ.setdefault("HELIUS_API_KEY", "x")
    from bot import BotState
    st = BotState.__new__(BotState)
    st.config = BotConfig(max_trade_usd=1.0, min_trade_usd=0.4, paper_bankroll_usd=1000.0)
    st.scorecard = scorecard.Scorecard(None); st.scorecard._disabled = {c: {} for c in disabled_cells}
    st.live_doctor = _FakeDoctor(decision)
    st.skips = []
    async def _skip(ev): st.skips.append(ev)
    st._skip_event = _skip
    return st


def test_plan_entry_scalp_full_and_persisted_fields():
    st = _planner("full")
    plan = asyncio.run(st._plan_entry("M" * 44, "scalp", "pumpfun", 1000, 200_000, 150.0, depth_sol=40.0, band="new"))
    assert plan and plan["size_usd"] == 1.0
    f = plan["trade_fields"]
    assert f["cost_gate_pass"] is True and f["doctor_decision"] == "full" and f["size_clamped"] is True
    assert abs(f["r_usd"] - 1.0 * f["sl_pct_with_slip"] / 100) < 1e-3 and f["r_usd_nominal"] == 20.0
    assert f["scorecard_cell"].startswith("scalp|none|new|")


def test_plan_entry_half_size_thin_pool_skip_and_doctor_skip():
    half = asyncio.run(_planner("half")._plan_entry("M" * 44, "scalp", "pumpfun", 1000, 200_000, 150.0, depth_sol=40.0))
    assert half["size_usd"] == 1.0 and half["trade_fields"]["doctor_decision"] == "half"   # cap still binds at ×0.5
    st = _planner("full")
    assert asyncio.run(st._plan_entry("M" * 44, "hunt", "pumpfun", 2500, 200_000, 150.0, depth_sol=0.05)) is None
    assert st.skips and st.skips[-1]["reason"] == "cost-gate"
    st2 = _planner("skip")
    assert asyncio.run(st2._plan_entry("M" * 44, "scalp", "pumpfun", 1000, 200_000, 150.0, depth_sol=40.0)) is None
    assert st2.skips[-1]["reason"] == "live-doctor skip"


def test_plan_entry_respects_disabled_scorecard_cell():
    st = _planner("full")
    ok = asyncio.run(st._plan_entry("M" * 44, "scalp", "pumpfun", 1000, 200_000, 150.0, depth_sol=40.0, band="new"))
    cell = ok["trade_fields"]["scorecard_cell"]
    st2 = _planner("full", disabled_cells=(cell,))
    # paper keeps filling a disabled cell (that is how `paper_since_disable` reopens it) — only LIVE money is benched
    assert asyncio.run(st2._plan_entry("M" * 44, "scalp", "pumpfun", 1000, 200_000, 150.0, depth_sol=40.0, band="new")) is not None
    st2.config.live_trading = True
    assert asyncio.run(st2._plan_entry("M" * 44, "scalp", "pumpfun", 1000, 200_000, 150.0, depth_sol=40.0, band="new")) is None
    assert st2.skips[-1]["reason"] == "scorecard cell disabled"


# ---------------- pre-test patch: one hunt brain, hunt cap counts snipes, default skip ----------------
def _bot_stub():
    import os
    os.environ.setdefault("HELIUS_RPC_URL", "http://x"); os.environ.setdefault("HELIUS_API_KEY", "x")
    from bot import BotState
    from inventory import InventoryHalt
    st = BotState.__new__(BotState)
    st.config = BotConfig(max_trade_usd=1.0, min_trade_usd=0.4, intelligent_exit_v2=False, creator_solvency_enabled=False)
    st.inventory = InventoryHalt(); st.live_doctor = None; st.stopping_gracefully = False
    st.active_trades = {}; st._pending_entry_mints = set(); st.sl_cooldown_until = {}
    st._entry_gate_lock = asyncio.Lock(); st.calls = []; st.recent_exit_until = {}; st.entered_mints = set(); st.tracking = {}
    st.reentry_watch = {}
    from reentry_policy import ReentryLedger
    st.reentry = ReentryLedger(); st._reentry_gate_mult = {}
    async def _skip(ev): st.calls.append(("skip", ev["reason"]))
    st._skip_event = _skip
    return st


def test_hunt_ladder_runs_when_ripcord_quiet_and_persists_legs():
    st = _bot_stub()
    one_r = 21.0
    slot = {"trade": {"id": "t1", "mint": "M" * 44, "book": "hunt", "entry_price_sol": 1.0, "sl_pct_with_slip": one_r, "expected_cost_pct": 4.0,
                      "classifier_action": "greylist_snipe"},
            "snipe_pattern_ctx": {"pattern": "slow_rug_tradeable"}, "peak_price_sol": 1.22, "ladder_legs_done": 0}
    class _Trades:
        async def update_one(self, q, u): st.calls.append(("db", u["$set"]))
    st.db = type("DB", (), {"trades": _Trades()})()
    async def _partial(mint, frac, reason=""): st.calls.append(("partial", round(frac, 2), reason)); return True
    async def _exit(mint, reason=""): st.calls.append(("exit", reason))
    st._partial_exit, st._exit = _partial, _exit
    st._buy_momentum_holds = lambda *a, **k: False
    assert st._check_snipe_pattern_exit(slot, 1.5)[0] is False        # +50% is NOT a rip-cord exit any more (no profit TP)
    closed = asyncio.run(st._run_ladder("M" * 44, slot, 1.215, 30))
    assert closed is False
    assert ("partial", 0.35, "ladder +1R: sell 35% (+21.5%)") in st.calls
    assert slot["ladder_legs_done"] == 1 and slot["ladder_stop_pct"] == 2.0   # BE + remaining expected exit cost (4%/2)
    assert any(c[0] == "db" and c[1].get("ladder_legs_done") == 1 for c in st.calls)


def test_ripcord_fire_is_a_full_exit_without_ladder():
    st = _bot_stub()
    slot = {"trade": {"id": "t2", "mint": "M" * 44, "book": "hunt", "entry_price_sol": 1.0, "sl_pct_with_slip": 21.0, "classifier_action": "greylist_snipe"},
            "snipe_pattern_ctx": {"pattern": "slow_rug_tradeable"}, "peak_price_sol": 1.6, "ladder_legs_done": 0, "_entry_ts_mono": time.time() - 30, "peak_ts": time.time() - 20,
            "_snipe_ripcord_start": time.time() - 10}
    st.config.greylist_snipe_ripcord_drawdown_pct = 40.0
    st.config.greylist_snipe_stale_seconds = 0
    st.config.greylist_snipe_ripcord_grace_seconds = 4
    fired, reason = st._check_snipe_pattern_exit(slot, 0.9)   # -44% from the 1.6 peak, sustained 10s > 4s grace
    assert fired is True and "rip-cord" in reason.lower()


def test_third_hunt_snipe_refused_by_hunt_cap(monkeypatch):
    from models import Launch
    import bot as bot_mod
    monkeypatch.setattr(bot_mod.reputation, "configured", lambda: False)   # the dev-reputation gate runs first when REPUTATION_BASE_URL is set
    st = _bot_stub()
    st.active_trades = {"a": {"trade": {"book": "hunt"}}, "b": {"trade": {"book": "hunt"}}}
    async def _impl(*a, **k): st.calls.append(("enter_impl",))
    st._enter_impl = _impl
    launch = Launch(mint="X" * 44, creator="C" * 44, name="x", symbol="x", bonding_curve="B" * 44)
    asyncio.run(st._enter(launch, 0, "greylist_snipe"))
    assert ("skip", "hunt-cap") in st.calls and ("enter_impl",) not in st.calls
    st.calls.clear()
    asyncio.run(st._enter(launch, 30, "momentum_new"))                # a scalp still has the 3rd slot
    assert ("skip", "hunt-cap") not in st.calls


def test_classifier_empty_tape_is_skip():
    rules = default_rules()
    quiet = classify({"elapsed_s": 4, "curve_fill_pct": 3, "unique_buyers": 2, "sol_inflow": 0.4, "creator_rugs": 0}, rules)
    assert quiet["action"] == "skip"
    inflow = classify({"elapsed_s": 4, "curve_fill_pct": 3, "unique_buyers": 2, "sol_inflow": 1.5, "creator_rugs": 0}, rules)
    assert inflow["action"] == "scalp"
    assert BotConfig().model_dump().get("greylist_snipe_profit_ripcord_pct") is None


# ---------------- feed labelling: pending vs final ----------------
def _feed_stub():
    st = _bot_stub()
    from models import ClassifierRules
    st.rules = ClassifierRules()
    st.recent_launches = []
    class _L:
        async def update_one(self, q, u): st.calls.append(("launch", u["$set"]["classifier_action"]))
    class _C:
        def __init__(self): self.doc = None
        async def find_one(self, q, *a): return self.doc
    st.db = type("DB", (), {})(); st.db.launches = _L(); st.db.creators = _C()
    return st


def test_feed_quiet_tape_is_pending_until_final_pass():
    st = _feed_stub()
    st.recent_launches = [{"id": "l1", "mint": "M" * 44, "creator": "C" * 44, "classifier_action": "pending", "creator_tokens_failed": 0}]
    st.tracking = {"M" * 44: {"start": time.time() - 3, "buyers": set(), "sol_inflow_lamports": 0, "curve_fill_pct": 1.0, "meta_seen": False}}
    assert asyncio.run(st._reclassify("M" * 44, source="schedule")) == "pending"
    assert "waiting for" in st.recent_launches[0]["classifier_reasons"][0]
    # 15s pass → final: still nothing on the tape → skip
    st.tracking["M" * 44]["start"] = time.time() - 15; st.tracking["M" * 44]["meta_seen"] = True
    assert asyncio.run(st._reclassify("M" * 44, final=True, source="schedule")) == "skip"


def test_feed_late_chase_skip_is_final_at_3s_and_scalp_overwrites_pending():
    st = _feed_stub()
    st.recent_launches = [{"id": "l1", "mint": "M" * 44, "creator": "C" * 44, "classifier_action": "pending", "creator_tokens_failed": 0}]
    st.tracking = {"M" * 44: {"start": time.time() - 3, "buyers": set(), "sol_inflow_lamports": 0, "curve_fill_pct": 45.0, "meta_seen": False}}
    assert asyncio.run(st._reclassify("M" * 44, source="schedule")) == "skip"          # late chase: final even at 3s
    assert asyncio.run(st._reclassify("M" * 44, source="tape")) == "skip"              # events never re-open a final label
    st.recent_launches[0]["classifier_action"] = "pending"
    st.tracking["M" * 44].update(curve_fill_pct=5.0, buyers={f"b{i}" for i in range(20)}, sol_inflow_lamports=2 * 10**9)
    assert asyncio.run(st._reclassify("M" * 44, source="tape")) == "scalp"
    assert st.calls[-1] == ("launch", "scalp")
    assert "pending" not in ACTIONS   # feed-only label, never an entry verdict


def test_hunt_spike_bank_sells_half_once_above_100pct():
    cfg = BotConfig()
    one_r = 21.0
    s = _slot("hunt", pct_peak=one_r + 1)
    exits.after_partial(s, 2.0); exits.after_partial(s, 2.0)                      # both ladder legs banked
    s["peak_price_sol"] = 3.2
    d = exits.decide_hunt(cfg, s, 218.0, 3.18, 300, _fire, lambda c: False)       # +218%, trail not firing yet
    assert d.kind == "partial" and d.fraction == 0.5 and d.reason.startswith("spike bank")
    s["spike_banked"] = True
    d2 = exits.decide_hunt(cfg, s, 230.0, 3.3, 310, _fire, lambda c: False)
    assert d2.kind is None                                                           # once per trade
    d3 = exits.decide_hunt(cfg, s, 95.0, 1.95, 200, _fire, lambda c: False)
    assert d3.kind is None                                                           # below the +100% / 5R line nothing fires
    s2 = _slot("hunt", pct_peak=50); exits.after_partial(s2, 2.0); exits.after_partial(s2, 2.0)
    assert exits.decide_hunt(cfg, s2, 99.0, 1.99, 200, _fire, lambda c: False).kind is None
