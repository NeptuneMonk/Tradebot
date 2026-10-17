"""Fill-cost model + price-sanity guard."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_fills")

import fills
import bot as bot_mod
from models import BotConfig

SOL = 10**9


def _curve(real_sol=12.0):
    vsr = int((30 + real_sol) * SOL)
    vtr = 10**15 * 30 * SOL // vsr                   # keep k ≈ constant-ish
    return {"virtual_sol_reserves": vsr, "virtual_token_reserves": vtr, "real_sol_reserves": int(real_sol * SOL)}


def test_curve_paper_fill_is_impact_plus_fee_plus_slip():
    cfg = BotConfig(paper_extra_slip_bps=100, protocol_fee_pct_pumpfun=1.0)
    st = _curve(12.0)                                 # 42 SOL virtual
    pf = fills.paper_fill("pumpfun", st, int(0.42 * SOL), cfg)
    assert abs(pf["impact_pct"] - 1.0) < 1e-6        # 0.42 / 42
    assert pf["fee_pct"] == 1.0 and pf["slip_pct"] == 1.0
    assert 3.0 <= pf["total_pct"] <= 3.2             # ≈ impact + fee + slip (haircut compounds slightly)
    assert pf["entry_price_sol"] > pf["spot_price_sol"] > 0


def test_pumpswap_paper_fill_no_longer_books_worst_case():
    cfg = BotConfig(paper_extra_slip_bps=100, slippage_bps=600, protocol_fee_pct_pumpswap=0.3)
    pool = {"base_reserves": 10**14, "quote_reserves": 200 * SOL}
    pf = fills.paper_fill("pumpswap", pool, int(0.25 * SOL), cfg)
    assert pf["fee_pct"] == 0.3 and abs(pf["impact_pct"] - 0.125) < 1e-6
    assert pf["total_pct"] < 1.6                      # was ≈ 7–10 % when the tolerance was booked as the fill
    live = fills.fill_cost_fields("pumpswap", pool, int(0.25 * SOL), pf["tokens_out"], cfg, expected=False)
    assert live["fill_expected"] is False and live["fill_slip_pct"] == 6.0      # live audit shows the tolerance


def test_projected_cost_for_candidates():
    cfg = BotConfig(paper_extra_slip_bps=100, protocol_fee_pct_pumpfun=1.0)
    c = fills.projected_cost_pct("pumpfun", 35 * SOL, int(3.5 * SOL), cfg)
    assert c["impact_pct"] == 10.0 and c["total_pct"] == 12.0
    assert fills.projected_cost_pct("pumpfun", 0, SOL, cfg) is None


def _state(ratio=4.0):
    st = bot_mod.BotState.__new__(bot_mod.BotState)
    st.config = BotConfig(price_sanity_ratio=ratio)
    st.stats = {}
    return st


def test_price_sanity_holds_single_outlier_then_accepts_confirmation():
    st = _state()
    slot = {"trade": {"symbol": "MONEROCHAN", "protocol": "pumpfun"}}
    assert st._price_sane("M", slot, 2.8e-13) is True
    assert st._price_sane("M", slot, 3.0e-13) is True
    assert st._price_sane("M", slot, 5.4e-11) is False          # 180× jump → held
    assert st.stats["price_outliers_held"] == 1
    assert st._price_sane("M", slot, 3.1e-13) is True           # back to normal — outlier discarded
    assert slot.get("_pending_outlier_sol") is None
    assert st._price_sane("M", slot, 1.5e-12) is False          # 4.8× — held once
    assert st._price_sane("M", slot, 1.6e-12) is True           # confirmed by the next sample → accepted
    assert st.stats["price_outliers_confirmed"] == 1


def test_price_sanity_off_when_ratio_zero():
    st = _state(ratio=0)
    slot = {"trade": {}}
    assert st._price_sane("M", slot, 1.0) and st._price_sane("M", slot, 1000.0)
