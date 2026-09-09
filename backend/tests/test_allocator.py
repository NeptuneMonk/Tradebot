"""Desk allocator: targets, stepping, floor/cap, and the retired hard-off rule."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import allocator  # noqa: E402
from doctor_learning import propose  # noqa: E402






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
