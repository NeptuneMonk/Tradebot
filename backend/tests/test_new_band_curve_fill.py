"""NEW-band 'near graduation' gates: curve fill % window + 5-min MC velocity, and the config-health warning for a USD
MC floor that sits above today's graduation cap (user config 2026-10-19: min MC $45k with SOL at $104 → grad ≈ $42.9k)."""
import os
import sys
import time
from collections import deque

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
from config_health import config_warnings  # noqa: E402
from models import BotConfig  # noqa: E402
from scanner import MomentumScanner  # noqa: E402

NOW = time.time()


def _bucket(fill, mc_then=30000.0, mc_now=33000.0):
    return {"protocol": "pumpfun", "curve_fill_pct": fill,
            "mc_samples": deque([(NOW - 280, mc_then), (NOW - 5, mc_now)])}


def test_gates_off_by_default_pass_everything():
    cfg = BotConfig()
    assert MomentumScanner._new_band_shape_gate(cfg, _bucket(10.0), {}, NOW) is None
    assert MomentumScanner._new_band_shape_gate(cfg, _bucket(99.0, 40000, 20000), {}, NOW) is None


def test_curve_fill_window():
    cfg = BotConfig(scanner_min_curve_fill_pct_new=65, scanner_max_curve_fill_pct_new=95)
    assert MomentumScanner._new_band_shape_gate(cfg, _bucket(40.0), {}, NOW)[0] == "curve-fill"
    assert MomentumScanner._new_band_shape_gate(cfg, _bucket(97.0), {}, NOW)[0] == "curve-fill-max"
    assert MomentumScanner._new_band_shape_gate(cfg, _bucket(80.0), {}, NOW) is None


def test_mc_velocity_gate_new_band():
    cfg = BotConfig(scanner_min_mc_velocity_5m_pct_new=5)
    m = {}
    assert MomentumScanner._new_band_shape_gate(cfg, _bucket(80.0, 30000, 33000), m, NOW) is None       # +10 %
    assert round(m["mc_velocity_5m_pct"], 1) == 10.0
    blocked = MomentumScanner._new_band_shape_gate(cfg, _bucket(80.0, 30000, 30600), {}, NOW)        # +2 %
    assert blocked[0] == "mc-velocity" and "+2.0%/5m < 5%" in blocked[1]


def test_warning_when_new_mc_floor_above_graduation():
    cfg = BotConfig(scanner_min_mc_usd_new=45000)
    keys = [w["key"] for w in config_warnings(cfg, sol_usd=104.0)]
    assert "scanner_min_mc_usd_new" in keys                     # 411 × 104 ≈ $42.7k < $45k floor
    cfg2 = BotConfig(scanner_min_mc_usd_new=28000)
    assert "scanner_min_mc_usd_new" not in [w["key"] for w in config_warnings(cfg2, sol_usd=104.0)]
    assert "scanner_min_mc_usd_new" not in [w["key"] for w in config_warnings(cfg, sol_usd=0.0)]   # no price → no claim


def test_warning_for_age_gap_when_targeting_late_curve():
    cfg = BotConfig(scanner_min_curve_fill_pct_new=65, band_new_max_age_min=30, band_seasoned_min_age_min=30)
    assert "band_new_max_age_min" not in [w["key"] for w in config_warnings(cfg, sol_usd=100.0)]   # 30 ≥ 30: no gap
    cfg = BotConfig(scanner_min_curve_fill_pct_new=65, band_new_max_age_min=20, band_seasoned_min_age_min=30)
    assert "band_new_max_age_min" in [w["key"] for w in config_warnings(cfg, sol_usd=100.0)]
