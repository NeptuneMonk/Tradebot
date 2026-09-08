"""Desk allocator: targets, stepping, floor/cap, and the retired hard-off rule."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import allocator  # noqa: E402
from doctor_learning import propose  # noqa: E402


def test_target_weight_floor_cap_and_thin_sample():
    assert allocator.target_weight(None, 0, 1.0, 15) == (1.0, "n=0 < 15: not enough fills to judge — keep exploring")
    assert allocator.target_weight(None, 0, 0.0, 15)[0] == allocator.FLOOR          # a benched book is lifted to the floor
    assert allocator.target_weight(-0.4, 66, 1.0, 15)[0] == allocator.FLOOR         # losing → floor, never 0
    assert allocator.target_weight(0.25, 40, 1.0, 15)[0] == 1.5                     # half the target expectancy → ×1.5
    assert allocator.target_weight(5.0, 40, 1.0, 15)[0] == allocator.CAP


def test_plan_steps_one_notch_per_cycle():
    cfg = {"book_momentum_size_mult": 0.0, "book_snipe_size_mult": 1.0, "reentry_size_multiplier": 0.5, "book_rh_size_mult": 1.0}
    b24 = {"momentum": {"expectancy_usd": -0.4, "n": 30}, "greylist_snipe": {"expectancy_usd": 0.6, "n": 20},
           "reentry": {"expectancy_usd": None, "n": 0}, "rh_pons": {"expectancy_usd": 0.1, "n": 10}}
    b7 = {"momentum": {"expectancy_usd": -0.3, "n": 66}, "greylist_snipe": {"expectancy_usd": 0.5, "n": 40},
          "reentry": {"expectancy_usd": None, "n": 0}, "rh_pons": {"expectancy_usd": 0.2, "n": 150}}
    rows = {r["book"]: r for r in allocator.plan(cfg, b24, b7, 15, {"momentum": True, "greylist_snipe": True, "reentry": True, "rh_pons": True})}
    m = rows["momentum"]
    assert m["current"] == 0.0 and m["target"] == 0.25 and m["next"] == 0.25 and m["change"]      # off → exploration floor
    g = rows["greylist_snipe"]
    assert g["target"] == 2.0 and g["next"] == 1.25 and g["change"]                                  # one step toward ×2
    assert rows["reentry"]["next"] == 0.5 and not rows["reentry"]["change"]                          # no data → hold
    rh = rows["rh_pons"]
    assert 1.0 < rh["target"] <= 1.5 and rh["next"] == 1.25
    assert allocator.plan(cfg, b24, b7, 15, {"momentum": False})[0]["book"] != "momentum"             # user-disabled book skipped


def test_last_resort_never_zeroes_a_book_when_allocator_on():
    cfg = {"allocator_enabled": True, "book_momentum_size_mult": 1.0, "book_snipe_size_mult": 1.0, "doctor_learning_min_trades_per_book": 5,
           "reentry_enabled": False, "creator_greylist_enabled": True}
    st = {"n": 60, "expectancy_usd": -0.4, "total_usd": -24.0, "winrate": 30.0, "expectancy_7d": -0.3, "n_7d": 60,
          "sl_share": 50.0, "tp_share": 10.0, "stale_timeout_share": 10.0, "payoff_ratio": 0.5, "avg_win_usd": 0.5, "avg_loss_usd": -1.0,
          "mfe_median_pct": 5.0, "mae_median_pct": -10.0, "by_trigger": {}}
    books = {"momentum": st, "greylist_snipe": {"n": 0, "expectancy_usd": None}, "reentry": {"n": 0, "expectancy_usd": None},
             "rh_pons": {"n": 0, "expectancy_usd": None}, "global": st}
    p = propose(cfg, books, 5)
    assert not (p and p.get("key") == "book_momentum_size_mult" and p.get("value") == 0.0)
    cfg["allocator_enabled"] = False
    p2 = propose(cfg, books, 5)
    if p2 and p2.get("key") == "book_momentum_size_mult":
        assert p2["value"] == 0.25
