"""Runner book — promotion → stages → decide_runner → cap → scorecard split."""
import asyncio
import sys
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import exits
import runner
import scorecard
from book_params import BOOK_DEFAULTS, exit_param
from models import BotConfig
from tests.test_profitability_refactor import _bot_stub

MINT = "M" * 44


def _patched(st):
    import bot as botmod

    class _Trades:
        async def update_one(self, q, u, upsert=False): st.calls.append(("db", dict(u["$set"])))
    st.db = type("DB", (), {"trades": _Trades()})()

    class _Hub:
        async def broadcast(self, ev, payload): st.calls.append(("ws", ev))
    botmod.hub = _Hub()

    async def _price(): return 100.0
    botmod.get_sol_usd_price = _price
    st._resolve_fees = lambda: (500_000, 500, 800)
    st._buy_momentum_holds = lambda *a, **k: False

    async def _partial(mint, frac, reason=""):
        st.calls.append(("partial", round(frac, 2), reason))
        t = st.active_trades[mint]["trade"]
        t["partial_realized_usd"] = float(t.get("partial_realized_usd") or 0) + 3.0
        return True

    async def _exit(mint, reason=""):
        st.calls.append(("exit", reason)); st.active_trades.pop(mint, None)
    st._partial_exit, st._exit = _partial, _exit
    return st


def _slot(book, *, one_r=12.0, r_usd=2.0, entry_usd=16.0, peak=1.3, legs=0, protocol="pumpfun"):
    return {"trade": {"id": "t1", "mint": MINT, "book": book, "entry_price_sol": 1.0, "sl_pct_with_slip": one_r, "r_usd": r_usd,
                      "entry_usd": entry_usd, "expected_cost_pct": 4.0, "mode": "paper", "entry_tokens": 1_000_000, "entry_sol": 0.16,
                      "exit_liquidity_likeness_pct": 20.0, "entry_ctx": {"unique_buyers": 5, "sol_inflow": 2.0}},
            "peak_price_sol": peak, "ladder_legs_done": legs, "protocol": protocol, "_depth_sol": 40.0, "_entry_ts_mono": time.time() - 30}


def _expanding_bucket(now):
    return {"buyers": {f"u{i}" for i in range(12)}, "sol_inflow_lamports": int(6e9), "start": now - 120,
            "buy_events": deque((now - i, int(1e8), f"u{i}") for i in range(12))}


def test_defaults_and_cap_constants():
    r = BOOK_DEFAULTS["runner"]
    assert (r["stop_loss_pct"], r["target_r"], r["trailing_stop_pct"], r["trailing_arm_pct"], r["hold_max_seconds"]) == (25.0, 0.0, 15.0, 0.0, 0)
    assert (r["ladder_1r_sell_pct"], r["add_on_r"], r["giveback_pct"], r["dead_s"], r["grad_grace_s"]) == (0.0, 0.5, 25.0, 90, 45)
    assert runner.RUNNER_CAP == 1 and runner.HUNT_CAP_WITH_RUNNER == 1
    assert exit_param(BotConfig(), "runner", "grad_grace_s") == 45


def test_scalp_target_without_mfe_or_flow_is_a_plain_scalp_exit():
    st = _patched(_bot_stub())
    s = _slot("scalp", peak=1.2)           # +1R and MFE fine, but no tape and no velocity → flow rule fails
    st.active_trades[MINT] = s
    st.tracking[MINT] = {"buyers": {"a", "b"}, "sol_inflow_lamports": int(1e9), "buy_events": deque(), "start": time.time() - 60}
    closed = asyncio.run(st._run_ladder(MINT, s, 1.2, 30))
    assert closed is True
    assert any(c[0] == "exit" and "target" in c[1] for c in st.calls)
    assert not any(c[0] == "partial" for c in st.calls)
    # MFE below 1.5R with a perfect tape → still a plain scalp exit
    st2 = _patched(_bot_stub())
    s2 = _slot("scalp", peak=1.17)
    st2.active_trades[MINT] = s2
    st2.tracking[MINT] = _expanding_bucket(time.time())
    assert asyncio.run(st2._run_ladder(MINT, s2, 1.2, 30)) is True and s2["trade"]["book"] == "scalp"
    assert s["trade"]["book"] == "scalp" and "promoted_from" not in s["trade"]


def test_scalp_target_with_rules_passing_banks_45_and_converts_to_runner():
    st = _patched(_bot_stub())
    now = time.time()
    s = _slot("scalp", peak=1.3)
    st.active_trades[MINT] = s
    st.tracking[MINT] = _expanding_bucket(now)
    closed = asyncio.run(st._run_ladder(MINT, s, 1.2, 30))     # +20% ≥ target +18%
    assert closed is False
    assert ("partial", 0.45, "promotion → runner: bank 45% (+20.0%)") in st.calls
    assert not any(c[0] == "exit" for c in st.calls)
    t = s["trade"]
    assert t["book"] == "runner" and t["promoted_from"] == "scalp" and t["runner_stage"] == "launch"
    assert t["promotion_price_sol"] == 1.2 and t["promotion_banked_usd"] == 3.0
    assert st._runner_open() == 1 and st._hunt_cap() == 1


def test_hunt_plus_1r_leg_then_rules_pass_promotes_remainder():
    st = _patched(_bot_stub())
    now = time.time()
    s = _slot("hunt", one_r=20.0, r_usd=3.0, entry_usd=15.0, peak=1.35, legs=1)
    s["ladder_stop_pct"] = 2.0
    s["trade"]["partial_realized_usd"] = 1.2
    st.active_trades[MINT] = s
    st.tracking[MINT] = _expanding_bucket(now)
    closed = asyncio.run(st._run_ladder(MINT, s, 1.3, 60))      # +30%: pnl 15×0.3+1.2 = 5.7 = 1.9R, MFE 1.75R
    assert closed is False
    assert not any(c[0] in ("partial", "exit") for c in st.calls)   # remainder converts, nothing sold
    assert s["trade"]["book"] == "runner" and s["trade"]["promoted_from"] == "hunt"
    assert st._runner_open() == 1 and st._hunt_open() == 0


def test_hunt_without_first_leg_is_not_promoted():
    st = _patched(_bot_stub())
    s = _slot("hunt", one_r=20.0, r_usd=3.0, entry_usd=15.0, peak=1.35, legs=0)
    st.active_trades[MINT] = s
    st.tracking[MINT] = _expanding_bucket(time.time())
    asyncio.run(st._run_ladder(MINT, s, 1.3, 60))
    assert s["trade"]["book"] == "hunt"
    assert ("partial", 0.35, "ladder +1R: sell 35% (+30.0%)") in st.calls   # the hunt ladder ran instead


def test_second_promotion_refused_while_runner_slot_full_scalp_exits_normally():
    st = _patched(_bot_stub())
    now = time.time()
    st.active_trades["R" * 44] = {"trade": {"book": "runner", "id": "r0"}}
    s = _slot("scalp", peak=1.3)
    st.active_trades[MINT] = s
    st.tracking[MINT] = _expanding_bucket(now)
    closed = asyncio.run(st._run_ladder(MINT, s, 1.2, 30))
    assert closed is True
    assert ("skip", "runner-cap") in st.calls
    assert any(c[0] == "exit" and "target" in c[1] for c in st.calls)
    assert s["trade"]["book"] == "scalp"
    assert st._hunt_cap() == 1


def test_runner_never_exits_on_scalp_clock():
    cfg = BotConfig()
    s = _slot("runner")
    runner.promote(s["trade"], s, 1.2, "pumpfun", time.time() - 600)
    flow = {"peak_price_sol": 1.25, "giveback_pct": 4.0, "reason": "retail"}
    for elapsed in (40, 41, 3600):
        d = exits.decide_runner(cfg, s, 1.2, lambda b, sev=0: b, lambda b: b, flow=flow, stage="retail")
        assert d.kind is None, elapsed
    assert exit_param(cfg, "runner", "hold_max_seconds") == 0


def test_runner_no_pool_after_grace_exits():
    cfg = BotConfig()
    s = _slot("runner")
    runner.promote(s["trade"], s, 1.2, "pumpfun")
    flow = {"peak_price_sol": 1.25, "giveback_pct": 4.0}
    assert exits.decide_runner(cfg, s, 1.2, lambda b, sev=0: b, lambda b: b, flow=flow, stage="graduating", pool_missing_s=44).kind is None
    d = exits.decide_runner(cfg, s, 1.2, lambda b, sev=0: b, lambda b: b, flow=flow, stage="graduating", pool_missing_s=45)
    assert d.kind == "exit" and d.reason.startswith("runner-no-pool")


def test_runner_giveback_armed_only_after_plus_1r_from_promotion():
    cfg = BotConfig()
    s = _slot("runner")     # 1R = 12 %
    runner.promote(s["trade"], s, 1.0, "pumpswap")
    fire = (lambda b, sev=0: b, lambda b: b)
    # peak +10 % (< 1R) and a 16 % giveback → not armed, only the 25 % promotion stop can act
    assert exits.decide_runner(cfg, s, 0.924, *fire, flow={"peak_price_sol": 1.10, "giveback_pct": 16.0}, stage="retail").kind is None
    d = exits.decide_runner(cfg, s, 1.07, *fire, flow={"peak_price_sol": 1.30, "giveback_pct": 17.7}, stage="retail")
    assert d.kind == "exit" and "giveback" in d.reason
    d = exits.decide_runner(cfg, s, 0.74, *fire, flow={"peak_price_sol": 1.10, "giveback_pct": 32.7}, stage="retail")
    assert d.kind == "exit" and "stop-loss" in d.reason
    d = exits.decide_runner(cfg, s, 1.40, *fire, flow={"peak_price_sol": 1.40, "giveback_pct": 0.0}, stage="retail")
    assert d.kind == "partial" and d.fraction == 0.25 and "+3R" in d.reason
    s["trade"]["runner_3r_done"] = True
    d = exits.decide_runner(cfg, s, 1.40, *fire, flow={"peak_price_sol": 1.40, "giveback_pct": 0.0, "reason": "last trade 40s ago"}, stage="exhausted")
    assert d.kind == "exit" and d.reason.startswith("runner-exhausted")


def test_stages_launch_graduating_graduated_retail_exhausted():
    cfg = BotConfig()
    s = _slot("runner")
    runner.promote(s["trade"], s, 1.0, "pumpfun")
    now = time.time()
    dead = {"last_trade_age_s": 50.0, "new_buyers": 0, "mc_velocity_5m_pct": 0.0, "vacuum": False, "giveback_pct": 2.0, "peak_price_sol": 1.05}
    live = {"last_trade_age_s": 3.0, "new_buyers": 4, "mc_velocity_5m_pct": 6.0, "vacuum": False, "giveback_pct": 2.0, "peak_price_sol": 1.05}
    assert runner.update_stage(cfg, s["trade"], s, dead, now, curve_complete=False, pool_ready=False) == "launch"
    assert runner.update_stage(cfg, s["trade"], s, dead, now + 10, curve_complete=True, pool_ready=False) == "graduating"
    assert runner.update_stage(cfg, s["trade"], s, dead, now + 20, curve_complete=True, pool_ready=True) == "graduated"
    assert runner.update_stage(cfg, s["trade"], s, live, now + 30, curve_complete=True, pool_ready=True) == "retail"
    assert runner.update_stage(cfg, s["trade"], s, dead, now + 40, curve_complete=True, pool_ready=True) == "graduated"
    assert runner.update_stage(cfg, s["trade"], s, dead, now + 40 + 89, curve_complete=True, pool_ready=True) == "graduated"
    assert runner.update_stage(cfg, s["trade"], s, dead, now + 40 + 90, curve_complete=True, pool_ready=True) == "exhausted"
    assert runner.update_stage(cfg, s["trade"], s, live, now + 200, curve_complete=True, pool_ready=True) == "exhausted"   # terminal


def test_add_on_once_cost_gated_and_second_refused():
    cfg = BotConfig(max_trade_usd=10.0)
    t = {"r_usd": 4.0}
    plan = runner.add_on_plan(cfg, t, depth_usd=5000.0, exit_slip_bps=800, entry_slip_bps=500, fee_usd_round_trip=0.05, max_trade_usd=10.0)
    assert plan and plan["size_usd"] == 2.0 and plan["cost_gate_pass"] is True
    t["runner_add_on_done"] = True
    assert runner.add_on_plan(cfg, t, depth_usd=5000.0, exit_slip_bps=800, entry_slip_bps=500, fee_usd_round_trip=0.05, max_trade_usd=10.0) is None
    thin = runner.add_on_plan(cfg, {"r_usd": 40.0}, depth_usd=100.0, exit_slip_bps=800, entry_slip_bps=500, fee_usd_round_trip=0.05, max_trade_usd=10.0)
    assert thin["size_usd"] == 10.0 and thin["cost_gate_pass"] is False


def test_second_add_on_refused_in_bot_path(monkeypatch):
    st = _patched(_bot_stub())
    s = _slot("runner", protocol="pumpswap")
    runner.promote(s["trade"], s, 1.0, "pumpswap")
    s["trade"]["runner_add_on_done"] = True
    st.active_trades[MINT] = s
    import bot as botmod
    async def _boom(*a, **k): raise AssertionError("pool state must not be fetched for a second add-on")
    monkeypatch.setattr(botmod.pumpswap, "fetch_pool_state", _boom)
    async def _plan(*a, **k): raise AssertionError("no second add-on")
    old = runner.add_on_plan
    runner.add_on_plan = _plan
    try:
        assert asyncio.run(st._run_runner(MINT, s, 1.1, lambda b, sev=0: b, lambda b: b)) is False
    finally:
        runner.add_on_plan = old


def test_entered_mints_and_tracker_drop_do_not_close_an_open_runner():
    st = _patched(_bot_stub())
    s = _slot("runner")
    runner.promote(s["trade"], s, 1.0, "pumpfun")
    st.active_trades[MINT] = s
    st.entered_mints.add(MINT)
    st.tracking.pop(MINT, None)                     # tracker aged the bucket out
    closed = asyncio.run(st._run_runner(MINT, s, 1.05, lambda b, sev=0: b, lambda b: b))
    assert closed is False and MINT in st.active_trades and not any(c[0] == "exit" for c in st.calls)
    assert st._runner_open() == 1


def test_scorecard_runner_cell_counts_only_post_promotion_leg():
    t = {"book": "runner", "r_usd": 2.0, "pnl_usd": 9.0, "promotion_banked_usd": 3.0, "runner_pnl_usd": 6.0, "exit_reason": "runner giveback"}
    st = scorecard.stats([t])
    assert st["n"] == 1 and abs(st["expectancy_r"] - 3.0) < 1e-9 and abs(st["expectancy_usd"] - 6.0) < 1e-9
    assert scorecard.cell_for_trade({**t, "entry_time": "2026-06-01T10:00:00+00:00", "expected_cost_pct": 4.0}).startswith("runner|")


def test_hunt_cap_drops_to_one_while_runner_open(monkeypatch):
    from models import Launch
    import bot as bot_mod
    monkeypatch.setattr(bot_mod.reputation, "configured", lambda: False)   # dev-reputation gate runs first when REPUTATION_BASE_URL is set
    st = _patched(_bot_stub())
    st.active_trades = {"a": {"trade": {"book": "hunt"}}, "r": {"trade": {"book": "runner"}}}
    async def _impl(*a, **k): st.calls.append(("enter_impl",))
    st._enter_impl = _impl
    launch = Launch(mint="X" * 44, creator="C" * 44, name="x", symbol="x", bonding_curve="B" * 44)
    asyncio.run(st._enter(launch, 0, "greylist_snipe"))
    assert ("skip", "hunt-cap") in st.calls and ("enter_impl",) not in st.calls
    st.active_trades.pop("r")
    st.calls.clear()
    asyncio.run(st._enter(launch, 0, "greylist_snipe"))            # no runner → 2 hunt slots again
    assert ("skip", "hunt-cap") not in st.calls


def test_promotion_aborts_when_slot_closes_during_the_bank_sell():
    st = _patched(_bot_stub())
    now = time.time()
    s = _slot("scalp", peak=1.3)
    st.active_trades[MINT] = s
    st.tracking[MINT] = _expanding_bucket(now)

    async def _partial_then_exit(mint, frac, reason=""):
        st.calls.append(("partial", round(frac, 2)))
        st.active_trades.pop(mint, None)          # a concurrent full exit won the race while we were selling
        return True
    st._partial_exit = _partial_then_exit
    assert asyncio.run(st._try_promote(MINT, s, 1.2, 20.0)) is False
    assert s["trade"]["book"] == "scalp" and "promoted_from" not in s["trade"]
    assert not any(c[0] == "db" for c in st.calls)   # nothing persisted → the closed row stays closed
    # and a stale slot (already popped) can never be promoted at all
    st2 = _patched(_bot_stub())
    s2 = _slot("scalp", peak=1.3)
    st2.tracking[MINT] = _expanding_bucket(now)
    assert asyncio.run(st2._try_promote(MINT, s2, 1.2, 20.0)) is False and not st2.calls


def test_run_ladder_holds_exit_in_progress_during_promotion():
    st = _patched(_bot_stub())
    s = _slot("scalp", peak=1.3)
    st.active_trades[MINT] = s
    st.tracking[MINT] = _expanding_bucket(time.time())
    seen = {}

    async def _partial(mint, frac, reason=""):
        seen["in_progress"] = s.get("exit_in_progress")
        return True
    st._partial_exit = _partial
    asyncio.run(st._run_ladder(MINT, s, 1.2, 30))
    assert seen["in_progress"] is True and s["exit_in_progress"] is False and s["trade"]["book"] == "runner"


def test_runner_ignores_snipe_stale_and_velocity_exits_but_keeps_ripcord():
    st = _bot_stub()
    s = _slot("runner")
    s["snipe_pattern_ctx"] = {"pattern": "slow_rug_tradeable"}
    st._check_snipe_pattern_exit_impl = lambda slot, px: (True, "snipe stale-exit (held 120s ≥ 90s without a new high)")
    assert st._check_snipe_pattern_exit(s, 1.0) == (False, "")
    st._check_snipe_pattern_exit_impl = lambda slot, px: (True, "snipe SOL-velocity decay (0.1 SOL/s vs 2.0 = -95%)")
    assert st._check_snipe_pattern_exit(s, 1.0) == (False, "")
    st._check_snipe_pattern_exit_impl = lambda slot, px: (True, "snipe rip-cord (62% drawdown from peak, sustained 8s)")
    assert st._check_snipe_pattern_exit(s, 1.0)[0] is True
    s["trade"]["book"] = "hunt"
    st._check_snipe_pattern_exit_impl = lambda slot, px: (True, "snipe stale-exit (held 120s ≥ 90s)")
    assert st._check_snipe_pattern_exit(s, 1.0)[0] is True          # hunt keeps every pattern exit


def test_promotion_thresholds_and_cap_come_from_config():
    """Operator-tunable promotion rules (Advanced → Exits → Runner promotion); constants stay the defaults."""
    from models import BotConfig
    cfg = BotConfig()
    base = dict(book="scalp", buyers_now=5, buyers_entry=3, inflow_now=2.0, inflow_entry=1.0, has_tape=True, mc_velocity_5m_pct=0.0,
                exit_liq_pct=50.0, exit_cost_pct=3.0, ladder_legs_done=0)
    assert runner.promotion_ok(pnl_r=0.6, mfe_r=1.0, cfg=cfg, **base)[0] is False          # defaults: 1R / 1.5R
    assert runner.promotion_ok(pnl_r=0.6, mfe_r=1.0, cfg=None, **base)[1].startswith("pnl +0.60R < +1R")
    cfg.runner_promo_min_r, cfg.runner_promo_min_mfe_r = 0.5, 1.0
    assert runner.promotion_ok(pnl_r=0.6, mfe_r=1.0, cfg=cfg, **base) == (True, "promote")
    cfg.runner_promo_max_exit_liq_pct = 40.0
    assert "exit-liquidity" in runner.promotion_ok(pnl_r=0.6, mfe_r=1.0, cfg=cfg, **base)[1]
    cfg.runner_promo_max_exit_liq_pct, cfg.runner_promo_max_exit_cost_pct = 70.0, 2.0
    assert "exit cost" in runner.promotion_ok(pnl_r=0.6, mfe_r=1.0, cfg=cfg, **base)[1]
    assert runner.cap(cfg) == 1 and runner.cap(None) == 1
    cfg.runner_cap = 2
    assert runner.cap(cfg) == 2
