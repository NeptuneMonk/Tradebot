"""Regression tests for the SOL-liquidity band gate.

Pre-fix bug: scanner's `score()` derived `real_sol_reserves` from
`last_vsr_lamports - 30 SOL`, but PumpSwap pools (graduated tokens) had their
`quote_reserves` stored directly as `last_vsr_lamports` — making the
subtraction produce 0 (clamped from negative). This silently rejected every
graduated token at the band gate, regardless of how strong its momentum was.

Post-fix: writers populate `last_real_sol_lamports` explicitly per protocol
(PumpSwap: `quote_reserves`; Pump.fun curve: `vsr - 30 SOL` or
`real_sol_reserves` from the API). `score()` prefers this field.
"""
import os

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

from collections import deque
from scanner import MomentumScanner


class _StubConfig:
    scanner_recent_inflow_window_s = 60
    scanner_holder_velocity_window_s = 30


class _StubState:
    def __init__(self):
        self.config = _StubConfig()
        self.tracking = {}


def _make_bucket(**overrides):
    base = {
        "start": 1000.0,
        "first_seen_price_sol": 1e-10,
        "last_price_sol": 1e-9,
        "buyers": set(),
        "buy_events": deque(maxlen=500),
    }
    base.update(overrides)
    return base


def test_graduated_token_reports_real_sol_from_pool_reserves():
    """A graduated token (PumpSwap pool) stores `quote_reserves` in
    `last_real_sol_lamports`. The scanner must report this as-is, with NO
    -30 SOL subtraction (PumpSwap pools have no virtual offset)."""
    scanner = MomentumScanner(_StubState())
    b = _make_bucket(last_real_sol_lamports=50_000_000_000)  # 50 SOL WSOL pool
    m = scanner.score(b, None, now=2000.0)
    assert abs(m["real_sol_reserves"] - 50.0) < 0.001


def test_pumpfun_curve_token_uses_real_sol_lamports_when_present():
    """A Pump.fun curve token after a buy event: `on_trade` stores
    `vsr - 30 SOL` directly in `last_real_sol_lamports`. Verify scanner
    reads this without any further subtraction."""
    scanner = MomentumScanner(_StubState())
    # 50 SOL real reserves (under the bonding-curve threshold ~85 SOL).
    b = _make_bucket(last_real_sol_lamports=50_000_000_000)
    m = scanner.score(b, None, now=2000.0)
    assert abs(m["real_sol_reserves"] - 50.0) < 0.001


def test_legacy_fallback_when_only_vsr_present():
    """Old buckets created before the fix may only have `last_vsr_lamports`.
    Verify the legacy fallback: real = max(0, vsr - 30 SOL)."""
    scanner = MomentumScanner(_StubState())
    # 60 SOL virtual (curve still active, well under graduation at ~115 SOL)
    b = _make_bucket(last_vsr_lamports=60_000_000_000)
    m = scanner.score(b, None, now=2000.0)
    assert abs(m["real_sol_reserves"] - 30.0) < 0.001  # 60 - 30


def test_pre_fix_bug_repro_pumpswap_token_reads_zero():
    """Reproduces the pre-fix bug for documentation: WITHOUT
    `last_real_sol_lamports`, a graduated token with 80 SOL in
    `last_vsr_lamports` (mis-stored as if it were vsr) reads as 50 SOL
    instead of 80. After the fix, callers MUST set
    `last_real_sol_lamports` explicitly to avoid this confusion.
    """
    scanner = MomentumScanner(_StubState())
    # Simulate the pre-fix discovery path: it stored quote_reserves into
    # last_vsr_lamports. Without last_real_sol_lamports set, fallback
    # still subtracts 30 (legacy behavior — present for backward compat).
    b = _make_bucket(last_vsr_lamports=80_000_000_000)  # 80 SOL pool
    m = scanner.score(b, None, now=2000.0)
    # Legacy path: 80 - 30 = 50 SOL (visibly wrong for PumpSwap, but kept
    # for back-compat with already-tracked buckets pre-restart). Writers
    # MUST set last_real_sol_lamports to get correct values.
    assert abs(m["real_sol_reserves"] - 50.0) < 0.001


def test_curve_state_overrides_bucket_cache():
    """When a real `curve_state` dict is passed, scanner must use its
    `real_sol_reserves` directly — never the bucket cache. This is the
    authoritative path used during entry-time verification."""
    scanner = MomentumScanner(_StubState())
    b = _make_bucket(last_real_sol_lamports=10_000_000_000)  # 10 SOL cache
    curve_state = {
        "real_sol_reserves": 75_000_000_000,
        "virtual_sol_reserves": 105_000_000_000,
        "virtual_token_reserves": 280_000_000_000_000,
        "complete": False,
    }
    m = scanner.score(b, curve_state, now=2000.0)
    assert abs(m["real_sol_reserves"] - 75.0) < 0.001
