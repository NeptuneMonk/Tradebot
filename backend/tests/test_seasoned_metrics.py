"""
Tests for the discovery refresh resilience fixes (2026-05-30):

Symptom: PumpSwap-graduated tokens showed `mc_velocity_5m_pct = 0.00%`,
`growth_pct_rolling = 0.0%`, and `buy_count = 0` in the scanner snapshot —
even though the tokens had healthy MC/liquidity and were trading actively
(last_trade_age_s within 1-2 minutes). The seasoned gate requires a
non-zero MC velocity, so the scanner NEVER fired entries on PumpSwap.

Root cause: Pump.fun's per-mint endpoint `/coins/{mint}` returns HTTP
200 with an EMPTY BODY for graduated tokens. The old refresh code did
`r.json()` → JSONDecodeError → caught + `continue` → entire bucket update
skipped. So mc_samples / price_samples never grew → velocity = 0 forever.

Fix: don't bail on empty body. Compute MC and price directly from
PumpSwap pool state (which IS reachable via Helius RPC) when the API
gives us nothing. Pre-seed the sample deques at seed time so the very
first refresh cycle produces a meaningful velocity reading.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

os.environ.setdefault("HELIUS_RPC_URL", "https://x")
os.environ.setdefault("HELIUS_WSS_URL", "wss://x")
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")
os.environ.setdefault("CORS_ORIGINS", "http://localhost")
os.environ.setdefault("PUMP_PROGRAM_ID", "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
os.environ.setdefault("PUMP_GLOBAL", "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf")
os.environ.setdefault("PUMP_FEE_RECIPIENT", "CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM")
os.environ.setdefault("PUMP_EVENT_AUTHORITY", "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1")

from scanner import _mc_velocity  # noqa: E402


def _mc_usd_from_pool(quote_lamports: float, base_raw: float, sol_usd: float) -> float:
    """Mirror of the inline MC formula in `discovery._refresh_once`.

    Pump.fun's standard MC formula is `price * 1B` (the full token supply).
    Computed directly from pool reserves so we don't depend on the empty
    per-mint Pump.fun API response for graduated tokens.

        MC_SOL  = (quote/base) * total_supply_raw
                = (quote/base) * 1e15 / 1e9     (lamports→SOL)
                = quote * 1e6 / base            (in SOL)
        MC_USD  = MC_SOL * sol_usd
    """
    if base_raw <= 0 or quote_lamports <= 0 or sol_usd <= 0:
        return 0.0
    mc_sol = quote_lamports * 1e6 / base_raw
    return mc_sol * sol_usd


def test_mc_usd_from_pool_realistic_pool():
    """A pool with 200 SOL (200e9 lamports) and 300M tokens (3e14 raw)
    at $200/SOL should give an MC around $133K — plausible for a
    seasoned PumpSwap token."""
    mc = _mc_usd_from_pool(
        quote_lamports=200e9, base_raw=3e14, sol_usd=200.0
    )
    # Expected: 200e9 * 1e6 / 3e14 = 666.67 SOL; * 200 = $133,333
    assert 130_000 < mc < 140_000, f"got ${mc:.0f}"


def test_mc_usd_from_pool_deep_liquidity():
    """A high-MC token (~$815K reported in user's screenshot) with
    ~440 SOL quote reserves at ~$200/SOL."""
    mc = _mc_usd_from_pool(
        quote_lamports=440e9, base_raw=1.2e14, sol_usd=200.0
    )
    # Expected: 440e9 * 1e6 / 1.2e14 = 3666.67 SOL; * 200 = $733,333
    assert 700_000 < mc < 800_000, f"got ${mc:.0f}"


def test_mc_usd_from_pool_zero_safe():
    """Defensive zero handling — never crash on degenerate input."""
    assert _mc_usd_from_pool(0, 1e14, 200) == 0
    assert _mc_usd_from_pool(1e9, 0, 200) == 0
    assert _mc_usd_from_pool(1e9, 1e14, 0) == 0


def test_mc_velocity_two_samples_produces_value():
    """With 2 mc_samples 5 min apart and a 10% increase, velocity = +10%."""
    now = 1_000_000.0
    samples = [(now - 300, 100_000), (now, 110_000)]
    v = _mc_velocity(samples, now, window_s=300)
    assert abs(v - 10.0) < 0.01


def test_mc_velocity_one_sample_returns_zero():
    """The fundamental warmup issue — with only 1 sample, velocity is 0.
    The seed-time pre-population (in discovery._seed_token) was added so
    that after just ONE refresh cycle (60s) we already have 2 samples
    and velocity becomes meaningful — instead of waiting 2 cycles.
    """
    now = 1_000_000.0
    samples = [(now - 60, 100_000)]
    assert _mc_velocity(samples, now, window_s=300) == 0.0


def test_mc_velocity_empty_returns_zero():
    """No samples at all → 0 (initial scanner pass before any refresh)."""
    assert _mc_velocity([], 1_000_000.0, window_s=300) == 0.0


def test_mc_velocity_handles_negative_change():
    """Velocity correctly reports negative % when MC drops."""
    now = 1_000_000.0
    samples = [(now - 300, 100_000), (now, 80_000)]
    v = _mc_velocity(samples, now, window_s=300)
    assert abs(v - (-20.0)) < 0.01


def test_mc_velocity_uses_oldest_in_window():
    """If multiple samples exist, velocity anchors to the earliest sample
    inside the window — not the first sample in the deque if it's older."""
    now = 1_000_000.0
    # 600s ago: 50K (OUTSIDE 5min window — should be ignored)
    # 250s ago: 100K (inside window — this is the baseline)
    # now:      120K (current)
    # Expected: (120-100)/100 = +20%
    samples = [(now - 600, 50_000), (now - 250, 100_000), (now, 120_000)]
    v = _mc_velocity(samples, now, window_s=300)
    assert abs(v - 20.0) < 0.01, f"got {v}"
