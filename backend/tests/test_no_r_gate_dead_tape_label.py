"""target_r = 0 must not gate entries on R · dead tape labels instead of exiting · config dead-state warnings."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
import cost_gate  # noqa: E402
import exits  # noqa: E402
from book_params import first_target_r  # noqa: E402
from config_health import config_warnings  # noqa: E402
from models import BotConfig  # noqa: E402


def _cfg(**bx):
    return BotConfig(book_exits={"scalp": {"stop_loss_pct": 20.0, "target_r": 0.0, "take_profit_pct": 0.0, "trailing_stop_pct": 6.0, **bx.get("scalp", {})},
                                 "hunt": {"stop_loss_pct": 15.0, "target_r": 0.0, "take_profit_pct": 20.0, "ladder_1r_sell_pct": 0.0, "ladder_2r_sell_pct": 0.0, **bx.get("hunt", {})},
                                 "rh_pons": {"stop_loss_pct": 12.0, "target_r": 0.0, "take_profit_pct": 0.0, **bx.get("rh_pons", {})}})


def test_first_target_r_follows_the_operator_not_a_constant():
    cfg = _cfg()
    assert first_target_r(cfg, "scalp", 22.0) is None                       # trail only → no R gate
    assert abs(first_target_r(cfg, "hunt", 16.0) - 20.0 / 16.0) < 1e-9      # TP 20 % over a 16 % R → 1.25R
    assert first_target_r(_cfg(scalp={"target_r": 1.15}), "scalp", 22.0) == 1.15
    assert first_target_r(_cfg(hunt={"ladder_1r_sell_pct": 35.0}), "hunt", 16.0) == 1.0   # ladder leg is the first cash-out
    assert first_target_r(cfg, "rh_pons", 13.0) is None


def _quote(ftr):
    return cost_gate.quote(size_usd=10.0, r_usd=2.0, first_target_r=ftr, protocol="pumpfun", entry_slip_bps=300, exit_slip_bps=300,
                           fee_usd_round_trip=0.05, ladder=False, depth_usd=5000.0)


def test_cost_gate_without_r_target_checks_ceiling_only():
    with_r = _quote(1.0)        # 1R = 20 % target vs ~4–5 % cost → passes on payoff
    none = _quote(None)
    assert with_r["cost_gate_pass"] and none["cost_gate_pass"]
    assert none["expected_target_pct"] is None and "ceiling only" in none["cost_gate_reason"]
    tiny = _quote(0.1)          # 2 % target < 2× cost → refused when an R target exists …
    assert not tiny["cost_gate_pass"] and "first target" in tiny["cost_gate_reason"]
    # … but a position with no R target is never refused for "first target"
    assert "first target" not in none["cost_gate_reason"]


def test_dead_tape_label_step_sets_and_clears_without_exit():
    t = {"id": "x"}
    dead = exits.ExitDecision("exit", "search-dead-tape: no new buyer / inflow for 31s (≥30s) [venue=pumpfun]")
    assert exits.dead_tape_label_step(t, {"last_new_buyer_ts": 100.0}, dead, 131.0) == "set"
    assert t["dead_tape"]["reason"].startswith("search-dead-tape") and t["dead_tape"]["ts"] == 131.0
    assert exits.dead_tape_label_step(t, {"last_new_buyer_ts": 100.0}, dead, 140.0) is None      # already flagged
    assert exits.dead_tape_label_step(t, {"last_new_buyer_ts": 100.0}, None, 141.0) is None      # blind feed: flag stays
    assert exits.dead_tape_label_step(t, {"last_new_buyer_ts": 150.0}, None, 151.0) == "cleared"  # fresh buyer → revived
    assert t["dead_tape"] is None
    assert BotConfig().dead_tape_mode == "label"


def test_config_warnings_flag_dead_states():
    base = BotConfig(max_trade_usd=15.0, min_trade_usd=2.5, risk_per_trade_pct=0.0, smart_wallet_enabled=True, smart_wallet_mode="both")
    ok = config_warnings(base, bankroll_usd=100.0, manual_smart_wallets=1)
    assert not [w for w in ok if w["level"] == "dead"], ok
    cap0 = config_warnings(BotConfig(smart_wallet_enabled=True, smart_wallet_max_per_hour=0), manual_smart_wallets=1)
    assert any(w["key"] == "smart_wallet_max_per_hour" and w["level"] == "dead" for w in cap0)
    nobody = config_warnings(BotConfig(smart_wallet_enabled=True, smart_wallet_learn_enabled=False), manual_smart_wallets=0)
    assert any(w["key"] == "smart_wallets" for w in nobody)
    # R sizing on, SL 95 % → stake collapses under min trade
    tiny = config_warnings(BotConfig(max_trade_usd=15.0, min_trade_usd=6.0, risk_per_trade_pct=2.0, smart_wallet_enabled=False,
                                     book_exits={"scalp": {"stop_loss_pct": 95.0}}), bankroll_usd=100.0)
    assert any(w["key"] == "size_scalp" and w["level"] == "dead" and "risk" in w["fix"] for w in tiny)
    # fixed stake below min trade
    fixed = config_warnings(BotConfig(max_trade_usd=3.0, min_trade_usd=5.0, risk_per_trade_pct=0.0, smart_wallet_enabled=False), bankroll_usd=100.0)
    assert any(w["key"] == "size_hunt" and "fixed stake" in w["msg"] for w in fixed)
    # no profit exit at all
    noexit = config_warnings(BotConfig(smart_wallet_enabled=False, book_exits={"scalp": {"target_r": 0.0, "take_profit_pct": 0.0, "trailing_stop_pct": 0.0}}), bankroll_usd=100.0)
    assert any(w["key"] == "exits_scalp" for w in noexit)
