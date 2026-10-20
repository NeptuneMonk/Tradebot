"""A filled curve stops printing on the pump.fun tape — the scanner must report 'curve-complete' (graduation pending),
not 'no-buy-events' (1099 tallies / 10 min in prod, 2026-10-19)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
from scanner import curve_is_complete  # noqa: E402

L = 1_000_000_000


def test_tape_flag_and_fill_mark_complete():
    assert curve_is_complete({"protocol": "pumpfun", "_tape_complete": True})
    assert curve_is_complete({"protocol": "pumpfun", "curve_fill_pct": 100.0})
    assert curve_is_complete({"protocol": "pumpfun", "complete": True})


def test_api_seeded_bucket_at_completion_reserves_is_complete():
    # seeded from the Pump.fun pull after the curve filled: no tape prints, vsr parked at 30 + 85 SOL
    assert curve_is_complete({"protocol": "pumpfun", "last_vsr_lamports": 115 * L, "buy_events": []})
    assert not curve_is_complete({"protocol": "pumpfun", "last_vsr_lamports": 90 * L, "buy_events": []})


def test_live_curve_and_pumpswap_are_not_complete():
    assert not curve_is_complete({"protocol": "pumpfun", "curve_fill_pct": 72.0, "last_vsr_lamports": 91 * L})
    assert not curve_is_complete({"protocol": "pumpswap", "curve_fill_pct": 100.0})     # already migrated: seasoned band
