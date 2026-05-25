"""Unit tests for the Intelligent Exit v2 helpers.

Covers:
- `_compute_auto_exit_slip_bps` — exchange-style slip formula
- `_recent_vol_pct` — rolling volatility computation
- `_pool_depth_sol` — protocol-agnostic depth read
- `_check_breach_persistence` — sustained-breach gating with timer reset

All tests are sync (no I/O) because the helpers don't touch the network/DB.
"""
import time
import os

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

import pytest

from bot import BotState
from models import BotConfig


def _make_bot():
    bs = BotState(db=None)  # db is unused for these helpers
    bs.config = BotConfig()  # use defaults
    return bs


# ---------- auto-slip formula ----------
def test_auto_slip_base_thick_pool_no_panic():
    bs = _make_bot()
    bps = bs._compute_auto_exit_slip_bps(panic=False, pool_depth_sol=50.0, recent_vol_pct=2.0)
    assert bps == 300  # baseline only


def test_auto_slip_thin_pool_adds_extra():
    bs = _make_bot()
    bps = bs._compute_auto_exit_slip_bps(panic=False, pool_depth_sol=5.0, recent_vol_pct=2.0)
    assert bps == 500  # 300 + 200


def test_auto_slip_panic_no_other_factors():
    bs = _make_bot()
    bps = bs._compute_auto_exit_slip_bps(panic=True, pool_depth_sol=50.0, recent_vol_pct=2.0)
    assert bps == 700  # 300 + 400


def test_auto_slip_all_factors_active_capped():
    bs = _make_bot()
    # thin + high-vol + panic = 300 + 200 + 200 + 400 = 1100 (under cap)
    bps = bs._compute_auto_exit_slip_bps(panic=True, pool_depth_sol=3.0, recent_vol_pct=15.0)
    assert bps == 1100


def test_auto_slip_cap_enforced():
    bs = _make_bot()
    bs.config.auto_exit_slip_base_bps = 600
    bs.config.auto_exit_slip_panic_extra_bps = 700  # would push to 1500
    bps = bs._compute_auto_exit_slip_bps(panic=True, pool_depth_sol=50.0, recent_vol_pct=2.0)
    assert bps == bs.config.auto_exit_slip_cap_bps  # capped


# ---------- volatility ----------
def test_recent_vol_pct_insufficient_data():
    bs = _make_bot()
    assert bs._recent_vol_pct([], 5) is None
    assert bs._recent_vol_pct([(time.time(), 1.0)], 5) is None  # too few


def test_recent_vol_pct_low_vol():
    bs = _make_bot()
    now = time.time()
    samples = [(now - i * 0.5, 1.0 + i * 0.001) for i in range(8)]
    v = bs._recent_vol_pct(samples, 5)
    assert v is not None and v < 1.0  # under 1%


def test_recent_vol_pct_high_vol():
    bs = _make_bot()
    now = time.time()
    samples = [(now - i * 0.5, 1.0 + (0.5 if i % 2 else -0.5)) for i in range(8)]
    v = bs._recent_vol_pct(samples, 5)
    assert v is not None and v > 30.0


# ---------- pool depth ----------
def test_pool_depth_pumpfun():
    bs = _make_bot()
    state = {"virtual_sol_reserves": 50_000_000_000}  # 50 SOL
    assert bs._pool_depth_sol(state, "pumpfun") == 50.0


def test_pool_depth_pumpswap():
    bs = _make_bot()
    state = {"quote_reserves": 8_000_000_000}  # 8 SOL WSOL
    assert bs._pool_depth_sol(state, "pumpswap") == 8.0


def test_pool_depth_missing_state():
    bs = _make_bot()
    assert bs._pool_depth_sol({}, "pumpfun") == 0.0
    assert bs._pool_depth_sol(None, "pumpswap") == 0.0


# ---------- breach persistence (the critical SL/TS gate) ----------
def test_persistence_blocks_initial_breach():
    bs = _make_bot()
    slot = {}
    # First breach tick → should NOT fire (timer just started)
    assert bs._check_breach_persistence(
        slot, kind="sl", breached=True, persistence_ms=1000, min_samples=2,
    ) is False
    assert slot["sl_breached_since"] is not None


def test_persistence_fires_after_sustained_breach():
    bs = _make_bot()
    slot = {}
    # Manually backdate the first breach 1.5s ago + 2 samples
    slot["sl_breached_since"] = time.time() - 1.5
    slot["sl_breached_samples"] = 2
    assert bs._check_breach_persistence(
        slot, kind="sl", breached=True, persistence_ms=1000, min_samples=2,
    ) is True


def test_persistence_resets_on_recovery():
    bs = _make_bot()
    slot = {"sl_breached_since": time.time() - 5.0, "sl_breached_samples": 10}
    assert bs._check_breach_persistence(
        slot, kind="sl", breached=False, persistence_ms=1000, min_samples=2,
    ) is False
    # State is cleared — next breach restarts the timer
    assert slot["sl_breached_since"] is None
    assert slot["sl_breached_samples"] == 0


def test_persistence_requires_min_samples_even_if_old():
    bs = _make_bot()
    slot = {"sl_breached_since": time.time() - 5.0, "sl_breached_samples": 1}
    # Old enough but only 1 sample → still don't fire (min_samples=3)
    fire = bs._check_breach_persistence(
        slot, kind="sl", breached=True, persistence_ms=1000, min_samples=3,
    )
    # After this tick: samples=2 → still under min_samples — should NOT fire
    assert fire is False
    assert slot["sl_breached_samples"] == 2


def test_persistence_ts_and_sl_are_independent():
    bs = _make_bot()
    slot = {}
    # SL breach in progress
    bs._check_breach_persistence(slot, kind="sl", breached=True, persistence_ms=1000, min_samples=2)
    # TS recovers — should not affect SL state
    bs._check_breach_persistence(slot, kind="ts", breached=False, persistence_ms=1500, min_samples=2)
    assert slot["sl_breached_since"] is not None
    assert slot.get("ts_breached_since") is None


def test_severity_override_fires_immediately_on_sharp_dump():
    """v2.1: when price has dropped FAR past the SL trigger (e.g. -10% SL but
    price is at -16%), persistence is meant to prevent millisecond blips;
    a sustained sharp dump should fire immediately, not wait another 1.2s.
    Without this on thin pump.fun pools, the 1.2s wait turns a -10% SL into
    a -40% actual exit."""
    bs = _make_bot()
    slot = {}
    # First tick of breach, severity 6% beyond SL (over the 5% threshold) → fire NOW
    fired = bs._check_breach_persistence(
        slot, kind="sl", breached=True,
        persistence_ms=1200, min_samples=3,
        severity_pct=6.0, severity_threshold_pct=5.0,
    )
    assert fired is True, "severity override should fire on first tick"


def test_severity_below_threshold_still_uses_persistence():
    """If severity is BELOW the threshold (e.g. only -1% beyond SL), the
    normal persistence rules still apply — single tick should NOT fire."""
    bs = _make_bot()
    slot = {}
    fired = bs._check_breach_persistence(
        slot, kind="sl", breached=True,
        persistence_ms=1200, min_samples=3,
        severity_pct=1.0, severity_threshold_pct=5.0,
    )
    assert fired is False
    assert slot["sl_breached_since"] is not None  # timer started
