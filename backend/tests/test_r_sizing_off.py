"""risk_per_trade_pct = 0 → R sizing OFF: fixed stake = max_trade_usd × multiplier chain."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
import r_sizer  # noqa: E402
from bankroll import BankrollEngine as Bankroll  # noqa: E402
from models import BotConfig  # noqa: E402


def _size(risk, **kw):
    base = dict(bankroll_usd=100.0, risk_per_trade_pct=risk, sl_pct=95.0, exit_slip_pct=2.0, book_mult=1.0, doctor_mult=1.0,
                governor_mult=1.0, min_trade_usd=2.5, max_trade_usd=15.0)
    base.update(kw)
    return r_sizer.size_trade(**base)


def test_r_sizing_on_with_huge_sl_gives_tiny_stake():
    sz = _size(5.0)
    assert not sz["skip"] and sz["size_usd"] < 6.0 and sz["r_usd_nominal"] == 5.0


def test_risk_zero_uses_max_trade_as_fixed_stake():
    sz = _size(0.0)
    assert not sz["skip"] and sz["size_usd"] == 15.0 and sz["r_usd_nominal"] == 0.0
    assert sz["reason"].startswith("fixed stake") and sz["size_clamped"] is False
    assert abs(sz["r_usd"] - 15.0 * 0.97) < 1e-6          # real cash at risk is still reported


def test_fixed_stake_respects_multiplier_chain_and_min():
    half = _size(0.0, book_mult=0.5)
    assert half["size_usd"] == 7.5
    tiny = _size(0.0, book_mult=0.1)
    assert tiny["skip"] and "fixed stake $1.50 < min $2.50" in tiny["reason"]
    assert _size(0.0, governor_mult=0.0)["skip"]


def test_bankroll_derive_leaves_max_trade_alone_when_risk_zero():
    cfg = BotConfig(risk_per_trade_pct=0.0, daily_loss_limit_pct=10.0)
    d = Bankroll.derive(100.0, cfg)
    assert "max_trade_usd" not in d and d["daily_kill_switch_usd"] == 10.0
    rh = Bankroll.derive_rh(100.0, cfg, {"min_stake_usd": 3.0})
    assert "rh_max_trade_usd" not in rh and rh["rh_daily_kill_switch_usd"] == 10.0
    on = Bankroll.derive(100.0, BotConfig(risk_per_trade_pct=5.0, daily_loss_limit_pct=10.0))
    assert on["max_trade_usd"] == 5.0
