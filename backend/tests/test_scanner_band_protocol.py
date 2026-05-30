"""
Tests for the protocol-aware band classification in MomentumScanner.

NEW band     → protocol == "pumpfun"  AND age-since-launch     ∈ [new_min, new_max]
SEASONED band → protocol == "pumpswap" AND age-since-graduation ∈ [seasoned_min, seasoned_max]
Anything else → None (excluded from the scanner entirely).
"""
from __future__ import annotations

import os
import sys
import time

# Make the backend modules importable from the tests folder
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

from models import BotConfig  # noqa: E402
from scanner import MomentumScanner  # noqa: E402


def _cfg(**overrides) -> BotConfig:
    c = BotConfig()
    for k, v in overrides.items():
        setattr(c, k, v)
    return c


def test_new_band_pumpfun_in_window():
    """A pumpfun token aged 5 minutes (within default 0-15min) classifies as new."""
    cfg = _cfg(band_new_min_age_min=0.0, band_new_max_age_min=15.0)
    now = 1_000_000.0
    bucket = {"protocol": "pumpfun", "start": now - 5 * 60, "graduated_at": None}
    assert MomentumScanner.classify_band(bucket, cfg, now) == "new"


def test_new_band_pumpfun_above_window_excluded():
    """A pumpfun token aged 20 min (above 15min cap) is excluded — NOT seasoned."""
    cfg = _cfg(band_new_min_age_min=0.0, band_new_max_age_min=15.0)
    now = 1_000_000.0
    bucket = {"protocol": "pumpfun", "start": now - 20 * 60, "graduated_at": None}
    # Critical: not yet graduated, so it CANNOT be seasoned even if old.
    assert MomentumScanner.classify_band(bucket, cfg, now) is None


def test_seasoned_band_pumpswap_in_window():
    """A pumpswap token graduated 10 min ago (within default 0-60min) is seasoned."""
    cfg = _cfg(band_seasoned_min_age_min=0.0, band_seasoned_max_age_min=60.0)
    now = 1_000_000.0
    bucket = {
        "protocol": "pumpswap",
        "start": now - 4 * 3600,
        "graduated_at": now - 10 * 60,
    }
    assert MomentumScanner.classify_band(bucket, cfg, now) == "seasoned"


def test_seasoned_band_pumpswap_above_window_excluded():
    """A pumpswap token graduated 90 min ago (>60min) is excluded."""
    cfg = _cfg(band_seasoned_min_age_min=0.0, band_seasoned_max_age_min=60.0)
    now = 1_000_000.0
    bucket = {
        "protocol": "pumpswap",
        "start": now - 10 * 3600,
        "graduated_at": now - 90 * 60,
    }
    assert MomentumScanner.classify_band(bucket, cfg, now) is None


def test_seasoned_below_min_excluded():
    """A pumpswap token graduated 30s ago is excluded if seasoned_min_age_min=5."""
    cfg = _cfg(band_seasoned_min_age_min=5.0, band_seasoned_max_age_min=60.0)
    now = 1_000_000.0
    bucket = {
        "protocol": "pumpswap",
        "start": now - 3600,
        "graduated_at": now - 30,
    }
    assert MomentumScanner.classify_band(bucket, cfg, now) is None


def test_new_below_min_excluded():
    """A pumpfun token aged 30s is excluded if new_min_age_min=2."""
    cfg = _cfg(band_new_min_age_min=2.0, band_new_max_age_min=15.0)
    now = 1_000_000.0
    bucket = {"protocol": "pumpfun", "start": now - 30, "graduated_at": None}
    assert MomentumScanner.classify_band(bucket, cfg, now) is None


def test_protocol_pumpfun_never_seasoned():
    """A pumpfun token, regardless of age, never classifies as seasoned."""
    cfg = _cfg(band_seasoned_max_age_min=99999.0)
    now = 1_000_000.0
    # Aged 24h, still pumpfun (didn't graduate)
    bucket = {"protocol": "pumpfun", "start": now - 24 * 3600, "graduated_at": None}
    band = MomentumScanner.classify_band(bucket, cfg, now)
    assert band != "seasoned"  # Either "new" if window allows, or None — but never seasoned


def test_protocol_pumpswap_never_new():
    """A pumpswap token never classifies as new, even if just graduated."""
    cfg = _cfg(band_new_max_age_min=99999.0, band_seasoned_max_age_min=60.0)
    now = 1_000_000.0
    bucket = {
        "protocol": "pumpswap",
        "start": now - 60,
        "graduated_at": now - 10,
    }
    band = MomentumScanner.classify_band(bucket, cfg, now)
    assert band != "new"
    assert band == "seasoned"


def test_pumpswap_missing_graduated_at_falls_back_to_start():
    """Legacy discovered pumpswap tokens without `graduated_at` fall back
    to `start` (first-seen) so they don't get permanently excluded."""
    cfg = _cfg(band_seasoned_min_age_min=0.0, band_seasoned_max_age_min=60.0)
    now = 1_000_000.0
    bucket = {
        "protocol": "pumpswap",
        "start": now - 30 * 60,   # first-seen 30min ago
        "graduated_at": None,
    }
    # 30min < 60min cap → seasoned
    assert MomentumScanner.classify_band(bucket, cfg, now) == "seasoned"


def test_unknown_protocol_excluded():
    """A bucket with an unknown protocol returns None."""
    cfg = _cfg()
    now = 1_000_000.0
    bucket = {"protocol": "raydium", "start": now - 60, "graduated_at": None}
    assert MomentumScanner.classify_band(bucket, cfg, now) is None


def test_band_boundary_inclusive_at_max():
    """A token exactly AT the band max is included (inclusive boundary)."""
    cfg = _cfg(band_new_min_age_min=0.0, band_new_max_age_min=15.0)
    now = 1_000_000.0
    bucket = {"protocol": "pumpfun", "start": now - 15 * 60, "graduated_at": None}
    assert MomentumScanner.classify_band(bucket, cfg, now) == "new"


def test_band_boundary_inclusive_at_min():
    """A token exactly AT the band min is included (inclusive boundary)."""
    cfg = _cfg(band_new_min_age_min=2.0, band_new_max_age_min=15.0)
    now = 1_000_000.0
    bucket = {"protocol": "pumpfun", "start": now - 120, "graduated_at": None}
    assert MomentumScanner.classify_band(bucket, cfg, now) == "new"


def test_default_protocol_pumpfun_when_missing():
    """A bucket without an explicit protocol defaults to pumpfun
    (legacy buckets created before this feature)."""
    cfg = _cfg(band_new_min_age_min=0.0, band_new_max_age_min=15.0)
    now = 1_000_000.0
    bucket = {"start": now - 5 * 60}  # No `protocol` key
    assert MomentumScanner.classify_band(bucket, cfg, now) == "new"


def test_independent_new_and_seasoned_windows():
    """User can set asymmetric windows per band (the whole point of the
    refactor): new=[0,5], seasoned=[10,30] — they don't have to be
    contiguous or symmetric."""
    cfg = _cfg(
        band_new_min_age_min=0.0,
        band_new_max_age_min=5.0,
        band_seasoned_min_age_min=10.0,
        band_seasoned_max_age_min=30.0,
    )
    now = 1_000_000.0
    # 3min pumpfun → new
    b_new = {"protocol": "pumpfun", "start": now - 3 * 60, "graduated_at": None}
    assert MomentumScanner.classify_band(b_new, cfg, now) == "new"
    # 6min pumpfun → above new cap, NOT seasoned (different protocol gate)
    b_no = {"protocol": "pumpfun", "start": now - 6 * 60, "graduated_at": None}
    assert MomentumScanner.classify_band(b_no, cfg, now) is None
    # 20min-post-grad pumpswap → seasoned
    b_seas = {
        "protocol": "pumpswap",
        "start": now - 4 * 3600,
        "graduated_at": now - 20 * 60,
    }
    assert MomentumScanner.classify_band(b_seas, cfg, now) == "seasoned"
    # 8min-post-grad pumpswap → below seasoned min, excluded
    b_too_fresh = {
        "protocol": "pumpswap",
        "start": now - 3600,
        "graduated_at": now - 8 * 60,
    }
    assert MomentumScanner.classify_band(b_too_fresh, cfg, now) is None


def test_candidates_snapshot_excludes_out_of_band(monkeypatch):
    """End-to-end: candidates_snapshot returns only tokens that classify
    into a band — out-of-band tokens (e.g., 6h-old non-graduated pumpfun)
    are dropped from the snapshot."""
    from collections import deque

    class _MockState:
        def __init__(self):
            self.config = _cfg(
                band_new_min_age_min=0.0,
                band_new_max_age_min=15.0,
                band_seasoned_min_age_min=0.0,
                band_seasoned_max_age_min=60.0,
            )
            self.tracking = {}
            self.entered_mints = set()
            self.active_trades = {}

    state = _MockState()
    now = time.time()
    # Token A: pumpfun, 5min — new band → IN
    state.tracking["mintA"] = {
        "protocol": "pumpfun",
        "start": now - 5 * 60,
        "graduated_at": None,
        "buy_events": deque([(now - 30, 100_000_000, "u1")]),
        "buyers": {"u1"},
        "first_seen_price_sol": 1e-7,
        "last_price_sol": 2e-7,
        "price_samples": deque(),
    }
    # Token B: pumpfun, 6h — OUT (above new cap, can't be seasoned)
    state.tracking["mintB"] = {
        "protocol": "pumpfun",
        "start": now - 6 * 3600,
        "graduated_at": None,
        "buy_events": deque(),
        "buyers": set(),
        "first_seen_price_sol": 1e-7,
        "last_price_sol": 2e-7,
        "price_samples": deque(),
    }
    # Token C: pumpswap graduated 10min ago — seasoned band → IN
    state.tracking["mintC"] = {
        "protocol": "pumpswap",
        "start": now - 3 * 3600,
        "graduated_at": now - 10 * 60,
        "buy_events": deque(),
        "buyers": set(),
        "first_seen_price_sol": 1e-7,
        "last_price_sol": 3e-7,
        "price_samples": deque(),
    }
    scanner = MomentumScanner(state)
    snap = scanner.candidates_snapshot()
    mints_in_snap = {row["mint"] for row in snap}
    assert "mintA" in mints_in_snap
    assert "mintC" in mints_in_snap
    assert "mintB" not in mints_in_snap, (
        "Out-of-band pumpfun token leaked into the snapshot — band guard broken"
    )
    a_row = next(r for r in snap if r["mint"] == "mintA")
    c_row = next(r for r in snap if r["mint"] == "mintC")
    assert a_row["band"] == "new"
    assert c_row["band"] == "seasoned"
