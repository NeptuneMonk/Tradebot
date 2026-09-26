"""Manual holds (Buy Now / ladder pin): no clock, no momentum kill, no pattern rip-cord, outside max positions."""
import asyncio
import time

import pytest

import exits
from bot import BotState
from models import BotConfig
from rh_paper import RHPaperTrader


class _DB:
    pass


def _state() -> BotState:
    st = BotState(_DB())
    st.config = BotConfig()
    st.config.max_concurrent_positions = 2
    return st


def _slot(action="momentum_new", **extra):
    return {"trade": {"mint": f"m-{action}-{len(extra)}", "classifier_action": action, "entry_price_sol": 1.0, "book": "scalp", **extra}}


def test_is_manual_hold_flags():
    assert exits.is_manual_hold({"classifier_action": "manual"})
    assert exits.is_manual_hold({"classifier_action": "rh_pons_manual"})
    assert exits.is_manual_hold({"manual": True})
    assert not exits.is_manual_hold({"classifier_action": "momentum_new"})
    assert not exits.is_manual_hold(None)


def test_scalp_clock_skipped_for_manual():
    cfg = BotConfig()
    cfg.book_exits = {"scalp": {**(cfg.book_exits or {}).get("scalp", {}), "hold_max_seconds": 10, "stop_loss_pct": 12, "target_r": 0, "trailing_stop_pct": 0}}
    fire = lambda *a, **k: False
    auto = _slot("momentum_new")
    d = exits.decide_scalp(cfg, auto, 1.0, 1.01, elapsed=999, sl_fire=fire, ts_fire=fire)
    assert d.kind == "exit" and "clock" in d.reason
    man = _slot("manual")
    d = exits.decide_scalp(cfg, man, 1.0, 1.01, elapsed=999, sl_fire=fire, ts_fire=fire)
    assert d.kind is None


def test_manual_hold_is_r_only():
    """Operator hold: no SL / trail / TP / clock — the only automatic exit is the scalp target R."""
    cfg = BotConfig()
    cfg.book_exits = {"scalp": {**(cfg.book_exits or {}).get("scalp", {}), "hold_max_seconds": 10, "stop_loss_pct": 12,
                                "target_r": 1.5, "trailing_stop_pct": 6, "trailing_arm_pct": 10}}
    man = _slot("manual", sl_pct_with_slip=20.0)          # 1R = 20% → target = +30%
    assert exits.decide_manual(cfg, man, -60.0).kind is None            # deep red: no stop
    man["peak_price_sol"] = 1.25                                        # +25% peak then back to +5%: no trail
    assert exits.decide_manual(cfg, man, 5.0).kind is None
    assert exits.decide_manual(cfg, man, 29.0).kind is None             # under target: hold
    d = exits.decide_manual(cfg, man, 31.0)
    assert d.kind == "exit" and "target +1.5R" in d.reason and "manual hold" in d.reason
    # a manual hold that lands in the hunt book still uses the SCALP target_r
    hunt = _slot("manual", sl_pct_with_slip=20.0, book="hunt")
    assert exits.decide_manual(cfg, hunt, 31.0).kind == "exit"


def test_dead_tape_skipped_for_manual():
    cfg = BotConfig()
    cfg.book_exits = {"scalp": {**(cfg.book_exits or {}).get("scalp", {}), "no_new_buyers_s": 5}}
    now = time.time()
    bucket = {"last_new_buyer_ts": now - 100, "last_inflow_ts": now - 100}
    assert exits.search_dead_tape(cfg, "scalp", bucket, now, entry_ts=now - 100) is not None
    assert exits.search_dead_tape(cfg, "scalp", bucket, now, entry_ts=now - 100, trade={"classifier_action": "manual"}) is None


def test_counted_open_excludes_manual_and_pinned():
    st = _state()
    st.active_trades = {
        "a": {"trade": {"mint": "a", "classifier_action": "momentum_new", "book": "scalp"}},
        "b": {"trade": {"mint": "b", "classifier_action": "manual", "book": "scalp"}},
        "c": {"trade": {"mint": "c", "classifier_action": "scanner_momentum", "book": "hunt"}},
    }
    st.tracking["c"] = {"pinned": True}
    assert len(st.active_trades) == 3
    assert st.counted_open() == 1
    assert not st._is_snipe(st.active_trades["b"])


def test_hunt_open_excludes_manual():
    st = _state()
    st.active_trades = {
        "a": {"trade": {"mint": "a", "classifier_action": "greylist_snipe", "book": "hunt"}},
        "b": {"trade": {"mint": "b", "classifier_action": "manual", "book": "hunt"}},
    }
    assert st._hunt_open() == 1


def test_rh_counted_open_and_manual_gates():
    st = _state()
    rh = RHPaperTrader(st)
    rh.positions = {
        "0x1": {"trade": {"mint": "0x1", "classifier_action": "rh_pons_paper"}},
        "0x2": {"trade": {"mint": "0x2", "classifier_action": "rh_pons_manual"}},
    }
    assert rh.counted_open() == 1
    st.config.rh_max_positions = 1
    b = {"graduated": False}
    assert rh._gates("0x3", b, time.time()) == "max-positions"
    rh.positions.pop("0x1")
    b["manual"] = True
    assert rh._gates("0x3", b, time.time()) == "ladder-only"   # past max-positions with the manual hold still open


# ---------------------------------------------------------------- recovery watch (no-momentum on a recovering red tape)
def test_is_recovering_direction_test():
    now = 1000.0
    assert exits.is_recovering(now, 0.95, 0.90, now - 30, 0.92, 0)                 # climbing, no lower low for 30s
    assert exits.is_recovering(now, 0.95, 0.90, now - 5, 0.92, 2)                  # climbing, fresh buyers
    assert not exits.is_recovering(now, 0.95, 0.90, now - 5, 0.92, 1)              # lower low 5s ago, one buyer: still sliding
    assert not exits.is_recovering(now, 0.92, 0.90, now - 30, 0.92, 3)             # flat vs 30s ago
    assert not exits.is_recovering(now, 0.90, 0.90, now - 30, 0.95, 3)             # sitting on the low
    assert not exits.is_recovering(now, 0.95, 0.90, now - 30, None, 3)             # no history → no watch


def test_recovery_watch_lifecycle():
    cfg = BotConfig()
    now = 1000.0
    w = exits.start_recovery_watch(cfg, now, entry=1.0, price=0.94, trough=0.90)
    assert abs(w["stop"] - 0.873) < 1e-9 and abs(w["target"] - 0.95) < 1e-9 and w["deadline"] == now + 90
    assert exits.recovery_watch_step(w, now + 10, 0.945) == ("hold", None)
    assert exits.recovery_watch_step(w, now + 20, 0.951)[0] == "reclaimed"
    assert exits.recovery_watch_step(w, now + 30, 0.87)[0] == "exit" and "recovery-stop" in exits.recovery_watch_step(w, now + 30, 0.87)[1]
    assert exits.recovery_watch_step(w, now + 91, 0.94)[0] == "exit" and "recovery-timeout" in exits.recovery_watch_step(w, now + 91, 0.94)[1]


def test_price_ago_picks_nearest_sample():
    now = 1000.0
    samples = [(now - 60, 1.0), (now - 31, 0.9), (now - 2, 0.95)]
    assert exits.price_ago(samples, now, 30.0) == 0.9
    assert exits.price_ago([(now - 2, 0.95)], now, 30.0) is None


# ---------------------------------------------------------------- net-flow momentum
def test_flow_ratio_and_flush_floor():
    import flow
    from solana_client import LAMPORTS_PER_SOL
    now = 1000.0
    b = {"last_vsr_lamports": 50 * LAMPORTS_PER_SOL,                           # 20 SOL real liquidity
         "buy_events": [(now - 5, 1 * LAMPORTS_PER_SOL, "a"), (now - 8, 0.5 * LAMPORTS_PER_SOL, "b")],
         "sell_events": [(now - 3, 0.5 * LAMPORTS_PER_SOL, "c")]}
    assert flow.liquidity(b, sol=True) == 20.0
    assert flow.flow_ratio_pct(b, now, 10.0, sol=True) == 5.0                   # (1.5 − 0.5) / 20
    assert flow.flow_ratio_pct({"buy_events": []}, now, 10.0, sol=True) is None   # unknown liquidity → callers fall back
    rh = {"net_quote": 2.0, "buy_events": [(now - 2, 0.1, "a")], "sell_events": [(now - 1, 0.3, "b")]}
    assert flow.flow_ratio_pct(rh, now, 10.0, sol=False) == -10.0
    cfg = BotConfig()
    calm = [(now - 50, 1.0), (now - 20, 1.02), (now - 1, 1.01)]
    wild = [(now - 50, 1.0), (now - 20, 0.6), (now - 1, 0.8)]
    assert flow.flush_floor_pct(cfg, calm, now) == 5.0                          # base floor on a calm tape
    assert abs(flow.flush_floor_pct(cfg, wild, now) - 14.0) < 1e-9              # 0.35 × 40% swing


def test_recovering_uses_net_flow_when_known():
    now = 1000.0
    assert exits.is_recovering(now, 0.95, 0.90, now - 5, 0.92, 0, net_flow=0.2)       # lower low 5s ago but money net-entering
    assert not exits.is_recovering(now, 0.95, 0.90, now - 5, 0.92, 5, net_flow=-0.1)  # 5 buyers but net outflow → not recovering


def test_long_term_hold_flags_and_rh_guard():
    assert exits.is_long_term_hold({"long_term_hold": True}) and not exits.is_long_term_hold({})
    assert exits.is_manual_hold({"classifier_action": "momentum_new", "long_term_hold": True})   # LTH gets every manual exemption
    st = _state()
    st.active_trades = {"a": _slot("momentum_new", long_term_hold=True), "b": _slot("momentum_new")}
    assert st.counted_open() == 1
    rh = RHPaperTrader.__new__(RHPaperTrader)
    rh.positions = {"x": {"trade": {"classifier_action": "rh_pons_paper", "long_term_hold": True}}, "y": {"trade": {"classifier_action": "rh_pons_paper"}}}
    assert rh.counted_open() == 1


def test_classify_buy_error_labels():
    c = BotState.classify_buy_error
    assert c("custom program error: 0x1772 slippage: TooMuchSolRequired").startswith("slippage")
    assert c("Blockhash not found").startswith("rpc: blockhash")
    assert c("HTTP 429 rate limit").startswith("rpc: rate-limited")
    assert c("insufficient lamports 1200").startswith("wallet")
    assert c("Custom:6023 BondingCurveComplete").startswith("curve")
    assert c("weird failure XYZ") == "buy failed: weird failure XYZ"


def test_add_to_position_paper_folds_into_average(monkeypatch):
    import bot as botmod

    class _Trades:
        def __init__(self): self.sets = []
        async def update_one(self, q, u): self.sets.append((q, u))

    class _DB2:
        def __init__(self): self.trades = _Trades()

    st = BotState(_DB2()); st.config = BotConfig()
    t = {"id": "T1", "mint": "M" * 44, "symbol": "ADD", "mode": "paper", "entry_tokens": 1_000_000, "entry_sol": 0.01, "entry_usd": 2.0,
         "entry_price_sol": 1e-8, "long_term_hold": True, "protocol": "pumpfun"}
    st.active_trades[t["mint"]] = {"trade": t, "protocol": "pumpfun"}
    curve = {"virtual_sol_reserves": 30 * 10**9, "virtual_token_reserves": 10**15, "real_sol_reserves": 5 * 10**9, "complete": False}
    async def _curve(m): return curve
    async def _px(): return 200.0
    async def _bc(*a, **k): return None
    monkeypatch.setattr(botmod.pumpfun, "fetch_bonding_curve_state", _curve)
    monkeypatch.setattr(botmod, "get_sol_usd_price", _px)
    monkeypatch.setattr(botmod.hub, "broadcast", _bc)
    monkeypatch.setattr(st, "_resolve_fees", lambda: (1000, 500, 0))
    res = asyncio.run(st.add_to_position(t["mint"], 2.0))
    assert res["ok"], res
    assert abs(t["entry_usd"] - 4.0) < 1e-9 and t["entry_sol"] > 0.01 and t["entry_tokens"] > 1_000_000
    assert len(t["adds"]) == 1 and t["adds"][0]["usd"] == 2.0 and t["adds"][0]["sig"] is None
    assert st.db.trades.sets and "adds" in st.db.trades.sets[-1][1]["$set"]
    curve["complete"] = True
    res = asyncio.run(st.add_to_position(t["mint"], 1.0))
    assert not res["ok"] and "complete" in res["reason"]
    assert not asyncio.run(st.add_to_position("nope", 1.0))["ok"]


def _cfg_exits(**books):
    cfg = BotConfig()
    cfg.trail_ratchet_enabled = False
    be = dict(cfg.book_exits or {})
    for b, p in books.items():
        be[b] = {**be.get(b, {}), **p}
    cfg.book_exits = be
    return cfg


def test_every_book_honours_clock_tp_and_target():
    yes = lambda breached, *a: bool(breached)      # fire exactly when the level is breached
    # scalp: TP % honoured
    cfg = _cfg_exits(scalp={"take_profit_pct": 8.0, "target_r": 5.0, "hold_max_seconds": 0})
    d = exits.decide_scalp(cfg, _slot("momentum_new", sl_pct_with_slip=12.0), 9.0, 1.09, elapsed=5, sl_fire=yes, ts_fire=yes)
    assert d.kind == "exit" and "take-profit" in d.reason
    # hunt: clock honoured (was hard "no clock"); target R when ladder legs are 0
    cfg = _cfg_exits(hunt={"hold_max_seconds": 10, "ladder_1r_sell_pct": 0, "ladder_2r_sell_pct": 0, "target_r": 2.0, "trailing_stop_pct": 0})
    h = _slot("reentry", sl_pct_with_slip=10.0, book="hunt")
    assert exits.decide_hunt(cfg, h, 1.0, 1.01, elapsed=11, sl_fire=yes, ts_fire=yes).reason.startswith("hunt clock")
    assert "target +2R" in exits.decide_hunt(cfg, h, 21.0, 1.21, elapsed=1, sl_fire=yes, ts_fire=yes).reason
    # runner: clock, TP, target R and Arm % honoured (all from promotion)
    cfg = _cfg_exits(runner={"hold_max_seconds": 10, "take_profit_pct": 0, "target_r": 0, "trailing_stop_pct": 15, "trailing_arm_pct": 0, "stop_loss_pct": 25})
    r = _slot("momentum_new", sl_pct_with_slip=10.0, book="runner", promotion_price_sol=1.0)
    flow = {"peak_price_sol": 1.02, "giveback_pct": 0.0}
    assert exits.decide_runner(cfg, r, 1.01, yes, yes, flow=flow, stage="live", elapsed=11).reason.startswith("runner clock")
    assert exits.decide_runner(cfg, r, 1.01, yes, yes, flow=flow, stage="live", elapsed=9).kind is None
    cfg = _cfg_exits(runner={"hold_max_seconds": 0, "take_profit_pct": 30.0, "trailing_stop_pct": 15, "trailing_arm_pct": 0})
    assert "take-profit" in exits.decide_runner(cfg, r, 1.31, yes, yes, flow={"peak_price_sol": 1.31, "giveback_pct": 0.0}, stage="live").reason
    cfg = _cfg_exits(runner={"hold_max_seconds": 0, "take_profit_pct": 0, "trailing_stop_pct": 15, "trailing_arm_pct": 50.0})
    # peak +20% (≥ 1R) with a 16% giveback: trail NOT armed because Arm % is 50 → no exit
    assert exits.decide_runner(cfg, r, 1.0, yes, yes, flow={"peak_price_sol": 1.20, "giveback_pct": 16.0}, stage="live").kind is None
    cfg = _cfg_exits(runner={"hold_max_seconds": 0, "take_profit_pct": 0, "trailing_stop_pct": 15, "trailing_arm_pct": 0})
    assert "giveback" in exits.decide_runner(cfg, r, 1.0, yes, yes, flow={"peak_price_sol": 1.20, "giveback_pct": 16.0}, stage="live").reason
