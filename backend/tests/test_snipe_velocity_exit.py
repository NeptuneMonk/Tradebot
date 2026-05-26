"""
Tests for the post-entry SOL-velocity and new-holder velocity decay exits
on greylist snipes. These run on ACTIVE positions only — they're NOT entry
gates. The entry velocity gate is a separate (unchanged) filter.

The signals we exercise (computed by `BotState._snipe_velocity_signals`):

  - `recent_sol_per_s`     : SOL inflow / sec over `velocity_window_s`
  - `baseline_sol_per_s`   : SOL inflow / sec over the PRIOR `velocity_baseline_s`
  - `recent_holders_per_s` : unique NEW buyers / sec in the recent window
                             (buyers that didn't appear in the baseline)
  - `baseline_holders_per_s`: unique buyers / sec in the baseline window

The exit ladder triggers when:
  - SOL: `recent / baseline <= 1 - drop_pct/100` (rate collapsed)
  - Holders: same comparison on unique new buyers
  - GUARD: `baseline_buys >= velocity_min_buys` so cold-start tracking
    buckets don't fire instantly when the deque is half empty.
"""
from __future__ import annotations
import os
import sys
import time
from collections import deque

os.environ.setdefault("HELIUS_RPC_URL", "https://x")
os.environ.setdefault("HELIUS_WSS_URL", "wss://x")
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")
os.environ.setdefault("CORS_ORIGINS", "http://localhost")

# Load backend/.env so bot.py module-load env reads succeed (PUMP_PROGRAM_ID etc.)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


LAMPORTS_PER_SOL = 1_000_000_000


def _make_stub(**cfg_overrides):
    """Minimal stub mimicking the bits of BotState used by the velocity
    helper + the snipe exit ladder."""
    from bot import BotState

    class _Cfg:
        pass

    stub = type("S", (), {})()
    stub.config = _Cfg()
    # Defaults that match models.BotConfig
    stub.config.greylist_snipe_pattern_exits = True
    stub.config.greylist_snipe_velocity_exits_enabled = True
    stub.config.greylist_snipe_velocity_window_s = 15
    stub.config.greylist_snipe_velocity_baseline_s = 60
    stub.config.greylist_snipe_velocity_min_buys = 8
    stub.config.greylist_snipe_sol_vel_drop_pct = 70.0
    stub.config.greylist_snipe_holder_vel_drop_pct = 70.0
    stub.config.greylist_snipe_profit_ripcord_pct = 100.0
    stub.config.greylist_snipe_peak_mc_proximity_pct = 85.0
    stub.config.greylist_snipe_curve_buffer_pct = 5.0
    stub.config.greylist_snipe_ripcord_drawdown_pct = 60.0
    stub.config.greylist_snipe_ripcord_grace_seconds = 8
    # P0 fields added in 2026-02-08
    stub.config.greylist_snipe_stale_seconds = 0  # disabled in unit tests
    stub.config.greylist_snipe_stale_min_profit_pct = 25.0
    stub.config.greylist_snipe_require_classified_pattern = False
    for k, v in cfg_overrides.items():
        setattr(stub.config, k, v)
    stub.tracking = {}
    # Bind helpers
    stub._snipe_velocity_signals = BotState._snipe_velocity_signals.__get__(stub, type(stub))
    stub._check_snipe_pattern_exit = BotState._check_snipe_pattern_exit.__get__(stub, type(stub))
    return stub


def _events(now, *specs):
    """Build a deque of (ts, sol_lamports, user) trade events.
    specs: list of (seconds_ago, sol_amount, user_addr)."""
    out = deque()
    for sec_ago, sol_amt, user in specs:
        out.append((now - sec_ago, int(sol_amt * LAMPORTS_PER_SOL), user))
    return out


# ===== _snipe_velocity_signals direct =====================================

def test_signals_returns_none_when_no_buy_events():
    stub = _make_stub()
    assert stub._snipe_velocity_signals({}) is None
    assert stub._snipe_velocity_signals({"buy_events": deque()}) is None


def test_signals_partitions_recent_vs_baseline_windows():
    """recent = last 15s; baseline = prior 60s (i.e. -75s..-15s)."""
    stub = _make_stub(greylist_snipe_velocity_window_s=15,
                      greylist_snipe_velocity_baseline_s=60)
    now = time.time()
    # 3 recent (within 15s) + 4 baseline (15s..75s ago)
    bucket = {"buy_events": _events(
        now,
        (2, 0.5, "A"), (5, 0.5, "B"), (10, 0.5, "C"),
        (20, 1.0, "D"), (30, 1.0, "E"), (45, 1.0, "F"), (70, 1.0, "G"),
    )}
    sig = stub._snipe_velocity_signals(bucket)
    assert sig["recent_buys"] == 3
    assert sig["baseline_buys"] == 4
    # SOL rates
    assert abs(sig["recent_sol_per_s"] - 1.5 / 15) < 1e-9   # 1.5 SOL / 15s
    assert abs(sig["baseline_sol_per_s"] - 4.0 / 60) < 1e-9  # 4 SOL / 60s


def test_signals_unique_new_holders_only():
    """A buyer who appeared in the baseline does NOT count as a new
    holder in the recent window."""
    stub = _make_stub(greylist_snipe_velocity_window_s=15,
                      greylist_snipe_velocity_baseline_s=60)
    now = time.time()
    bucket = {"buy_events": _events(
        now,
        # Recent: 2 unique users, both ALSO in baseline → 0 NEW
        (3, 0.1, "X"), (10, 0.1, "Y"),
        # Baseline: same X, Y plus a Z
        (40, 0.1, "X"), (45, 0.1, "Y"), (50, 0.1, "Z"),
    )}
    sig = stub._snipe_velocity_signals(bucket)
    assert sig["recent_holders_per_s"] == 0.0  # no NEW holders
    assert sig["baseline_holders_per_s"] == 3.0 / 60


def test_signals_drops_events_older_than_baseline():
    """Events outside the baseline window (>75s ago) are ignored."""
    stub = _make_stub(greylist_snipe_velocity_window_s=15,
                      greylist_snipe_velocity_baseline_s=60)
    now = time.time()
    bucket = {"buy_events": _events(
        now,
        (10, 0.5, "A"),    # recent
        (40, 0.5, "B"),    # baseline
        (90, 5.0, "C"),    # outside (older than 75s) — ignored
    )}
    sig = stub._snipe_velocity_signals(bucket)
    assert sig["recent_buys"] == 1
    assert sig["baseline_buys"] == 1


# ===== _check_snipe_pattern_exit — velocity gates =========================

def _make_slot(*, mint="m1", entry_price=1e-7, peak_price=None):
    slot = {
        "trade": {
            "mint": mint, "classifier_action": "greylist_snipe",
            "entry_price_sol": entry_price,
            "greylist_pattern_suggested_tp_pct": None,
        },
        "snipe_pattern_ctx": {},  # required so the ladder doesn't early-return
        "peak_price_sol": peak_price if peak_price is not None else entry_price,
    }
    return slot


def test_sol_velocity_drop_triggers_exit():
    """Recent SOL inflow drops 80% vs baseline → exit fires."""
    stub = _make_stub(greylist_snipe_sol_vel_drop_pct=70.0,
                      greylist_snipe_velocity_min_buys=4)
    now = time.time()
    # Baseline: 6 buys × 1 SOL over 60s = 0.1 SOL/s baseline rate.
    # Recent: 2 buys × 0.05 SOL over 15s = 0.0067 SOL/s recent rate.
    # Ratio = 0.0067 / 0.1 = 6.7% → 93% drop, clear trigger.
    mint = "m_solvel"
    stub.tracking[mint] = {
        "buy_events": _events(
            now,
            (3, 0.05, "r1"), (10, 0.05, "r2"),
            (20, 1.0, "b1"), (25, 1.0, "b2"), (35, 1.0, "b3"),
            (45, 1.0, "b4"), (55, 1.0, "b5"), (65, 1.0, "b6"),
        ),
        "curve_fill_pct": 10.0,
    }
    slot = _make_slot(mint=mint, entry_price=1e-7)
    should, reason = stub._check_snipe_pattern_exit(slot, 1.01e-7)
    assert should is True
    assert "SOL-velocity decay" in reason


def test_holder_velocity_drop_triggers_exit():
    """No new holders in recent window vs strong baseline → exit."""
    stub = _make_stub(greylist_snipe_holder_vel_drop_pct=70.0,
                      greylist_snipe_sol_vel_drop_pct=99.9,  # don't let SOL trigger first
                      greylist_snipe_velocity_min_buys=4)
    now = time.time()
    mint = "m_holvel"
    stub.tracking[mint] = {
        "buy_events": _events(
            now,
            # Recent: only OLD buyers come back → 0 new holders
            (3, 5.0, "old1"), (10, 5.0, "old2"),
            # Baseline: 6 unique buyers (high holder velocity)
            (20, 1.0, "old1"), (25, 1.0, "old2"), (35, 1.0, "n3"),
            (45, 1.0, "n4"), (55, 1.0, "n5"), (65, 1.0, "n6"),
        ),
        "curve_fill_pct": 10.0,
    }
    slot = _make_slot(mint=mint, entry_price=1e-7)
    should, reason = stub._check_snipe_pattern_exit(slot, 1.01e-7)
    assert should is True
    assert "new-holder velocity decay" in reason


def test_velocity_decay_skipped_when_baseline_too_thin():
    """`velocity_min_buys` guard prevents cold-start false positives."""
    stub = _make_stub(greylist_snipe_velocity_min_buys=10)
    now = time.time()
    mint = "m_thin"
    # Only 2 baseline buys — below the min_buys floor → no decay check.
    stub.tracking[mint] = {
        "buy_events": _events(
            now,
            (3, 0.01, "r1"),
            (40, 1.0, "b1"), (50, 1.0, "b2"),
        ),
        "curve_fill_pct": 10.0,
    }
    slot = _make_slot(mint=mint, entry_price=1e-7)
    should, _ = stub._check_snipe_pattern_exit(slot, 1.01e-7)
    assert should is False


def test_velocity_decay_skipped_when_pace_healthy():
    """Recent SOL/holder pace matches baseline → no exit."""
    stub = _make_stub(greylist_snipe_velocity_min_buys=4)
    now = time.time()
    mint = "m_ok"
    # Both windows have the same per-second rate.
    stub.tracking[mint] = {
        "buy_events": _events(
            now,
            (2, 1.0, "rA"), (5, 1.0, "rB"), (8, 1.0, "rC"), (12, 1.0, "rD"),
            (20, 1.0, "bA"), (30, 1.0, "bB"), (40, 1.0, "bC"),
            (50, 1.0, "bD"), (60, 1.0, "bE"), (70, 1.0, "bF"),
        ),
        "curve_fill_pct": 10.0,
    }
    slot = _make_slot(mint=mint, entry_price=1e-7)
    should, _ = stub._check_snipe_pattern_exit(slot, 1.01e-7)
    assert should is False


def test_velocity_decay_disabled_by_flag():
    """`greylist_snipe_velocity_exits_enabled=False` short-circuits both gates."""
    stub = _make_stub(greylist_snipe_velocity_exits_enabled=False,
                      greylist_snipe_velocity_min_buys=4)
    now = time.time()
    mint = "m_off"
    stub.tracking[mint] = {
        "buy_events": _events(
            now,
            (3, 0.01, "r1"),
            (20, 1.0, "b1"), (25, 1.0, "b2"), (35, 1.0, "b3"),
            (45, 1.0, "b4"), (55, 1.0, "b5"), (65, 1.0, "b6"),
        ),
        "curve_fill_pct": 10.0,
    }
    slot = _make_slot(mint=mint, entry_price=1e-7)
    should, _ = stub._check_snipe_pattern_exit(slot, 1.01e-7)
    assert should is False


# ===== Profit ripcord (+100%) ============================================

def test_profit_ripcord_fires_at_double():
    stub = _make_stub(greylist_snipe_profit_ripcord_pct=100.0)
    slot = _make_slot(entry_price=1e-7)
    # +101% → triggers
    should, reason = stub._check_snipe_pattern_exit(slot, 2.01e-7)
    assert should is True
    assert "profit-ripcord" in reason


def test_profit_ripcord_does_not_fire_below_threshold():
    stub = _make_stub(greylist_snipe_profit_ripcord_pct=100.0)
    slot = _make_slot(entry_price=1e-7)
    # +50% — below ripcord threshold
    should, _ = stub._check_snipe_pattern_exit(slot, 1.5e-7)
    assert should is False


def test_profit_ripcord_disabled_when_zero():
    stub = _make_stub(greylist_snipe_profit_ripcord_pct=0.0)
    slot = _make_slot(entry_price=1e-7)
    # +500% but ripcord disabled — no exit from this gate
    # (peak/curve/drawdown also won't fire since no ctx data)
    should, _ = stub._check_snipe_pattern_exit(slot, 6e-7)
    assert should is False


def test_profit_ripcord_priority_over_pattern_tp():
    """Ripcord (100%) ALWAYS wins, even when pattern TP is also satisfied
    (and would have fired at a lower %)."""
    stub = _make_stub(greylist_snipe_profit_ripcord_pct=100.0)
    slot = _make_slot(entry_price=1e-7)
    slot["trade"]["greylist_pattern_suggested_tp_pct"] = 25.0
    # +120% — both fire but ripcord runs first in the ladder.
    should, reason = stub._check_snipe_pattern_exit(slot, 2.2e-7)
    assert should is True
    assert "profit-ripcord" in reason


# ===== Stale-snipe time fail-safe =========================================
# Snipes held longer than `stale_seconds` without reaching `stale_min_profit_pct`
# get auto-exited. Prevents 10-30min drift losses observed in paper data.

def test_stale_exit_fires_when_held_past_threshold():
    """Held > 90s and stuck at +0% profit → exit fires."""
    stub = _make_stub(greylist_snipe_stale_seconds=90,
                      greylist_snipe_stale_min_profit_pct=25.0,
                      greylist_snipe_profit_ripcord_pct=0,  # disable to isolate
                      greylist_snipe_velocity_exits_enabled=False)
    slot = _make_slot(entry_price=1e-7)
    slot["_entry_ts_mono"] = time.time() - 120  # 120s ago
    should, reason = stub._check_snipe_pattern_exit(slot, 1.0e-7)  # +0%
    assert should is True
    assert "stale-exit" in reason


def test_stale_exit_does_not_fire_when_profitable_enough():
    """Held > 90s but +30% profit → keeps running."""
    stub = _make_stub(greylist_snipe_stale_seconds=90,
                      greylist_snipe_stale_min_profit_pct=25.0,
                      greylist_snipe_profit_ripcord_pct=0,
                      greylist_snipe_velocity_exits_enabled=False)
    slot = _make_slot(entry_price=1e-7)
    slot["_entry_ts_mono"] = time.time() - 120
    should, _ = stub._check_snipe_pattern_exit(slot, 1.3e-7)  # +30%
    assert should is False


def test_stale_exit_does_not_fire_before_threshold():
    """Only 30s old, no profit → still in window."""
    stub = _make_stub(greylist_snipe_stale_seconds=90,
                      greylist_snipe_stale_min_profit_pct=25.0,
                      greylist_snipe_profit_ripcord_pct=0,
                      greylist_snipe_velocity_exits_enabled=False)
    slot = _make_slot(entry_price=1e-7)
    slot["_entry_ts_mono"] = time.time() - 30
    should, _ = stub._check_snipe_pattern_exit(slot, 1.0e-7)
    assert should is False


def test_stale_exit_disabled_when_seconds_zero():
    """stale_seconds=0 → never fires."""
    stub = _make_stub(greylist_snipe_stale_seconds=0,
                      greylist_snipe_profit_ripcord_pct=0,
                      greylist_snipe_velocity_exits_enabled=False)
    slot = _make_slot(entry_price=1e-7)
    slot["_entry_ts_mono"] = time.time() - 9999
    should, _ = stub._check_snipe_pattern_exit(slot, 1.0e-7)
    assert should is False


def test_stale_exit_lazy_timestamp_no_immediate_fire():
    """If _entry_ts_mono is missing, the helper lazily stamps `now()` so
    the very next call doesn't immediately fire (age=0)."""
    stub = _make_stub(greylist_snipe_stale_seconds=90,
                      greylist_snipe_profit_ripcord_pct=0,
                      greylist_snipe_velocity_exits_enabled=False)
    slot = _make_slot(entry_price=1e-7)
    # No _entry_ts_mono on slot
    should, _ = stub._check_snipe_pattern_exit(slot, 1.0e-7)
    assert should is False
    # On second call, age is still ~0 → no fire
    should2, _ = stub._check_snipe_pattern_exit(slot, 1.0e-7)
    assert should2 is False
    # Stamp was set lazily
    assert "_entry_ts_mono" in slot
