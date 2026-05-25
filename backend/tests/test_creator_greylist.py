"""
Tests for creator_greylist scoring + decay.
"""
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("HELIUS_RPC_URL", "https://x")
os.environ.setdefault("HELIUS_WSS_URL", "wss://x")
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")

from creator_greylist import (  # noqa: E402
    apply_decay,
    compute_score,
    recommended_strategy,
    strategy_overrides,
    _peak_mc_component,
    _profitability_component,
    _predictability_component,
)


def _iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def test_decay_zero_hours_returns_input():
    assert apply_decay(80.0, datetime.now(timezone.utc).isoformat()) > 79.0


def test_decay_one_day_drops_about_22_percent():
    # 0.99^24 ≈ 0.786
    decayed = apply_decay(100.0, _iso(24))
    assert 75 < decayed < 82


def test_decay_handles_missing_iso():
    assert apply_decay(100.0, None) == 100.0


def test_decay_handles_zero_score():
    assert apply_decay(0, _iso(24)) == 0.0


def test_recommended_strategy_thresholds():
    assert recommended_strategy(80) == "aggressive"
    assert recommended_strategy(55) == "hybrid"
    assert recommended_strategy(10) == "standard"


def test_peak_mc_component_high_consistency_high_score():
    failed = [
        {"outcome": "failed", "final_peak_mc_usd": v}
        for v in (52_000, 48_000, 55_000, 51_000, 49_000)  # tight cluster around 50k
    ]
    score, stats = _peak_mc_component(failed)
    assert score >= 70  # 80pts magnitude × ~0.95 consistency
    assert stats["mean_peak_mc_usd"] == 51_000
    assert stats["cv"] < 0.2


def test_peak_mc_component_inconsistent_lower_score():
    failed = [
        {"outcome": "failed", "final_peak_mc_usd": v}
        for v in (50_000, 2_000, 100_000, 1_000, 80_000)  # all over the place
    ]
    score, _ = _peak_mc_component(failed)
    # mean = 46_600 → magnitude 60, but cv > 1.0 → consistency multiplier 0.5
    assert 25 <= score <= 35


def test_peak_mc_component_too_few_samples():
    failed = [{"outcome": "failed", "final_peak_mc_usd": 50_000}]
    score, stats = _peak_mc_component(failed)
    assert score == 0.0
    assert stats["n_failed_with_peak"] == 1


def test_peak_mc_component_ignores_non_failed_outcomes():
    mixed = [
        {"outcome": "graduated", "final_peak_mc_usd": 200_000},
        {"outcome": "failed", "final_peak_mc_usd": 30_000},
        {"outcome": "failed", "final_peak_mc_usd": 32_000},
    ]
    score, stats = _peak_mc_component(mixed)
    assert stats["n_failed_with_peak"] == 2
    assert stats["mean_peak_mc_usd"] == 31_000


def test_predictability_component_tight_rugs_high_score():
    trades = [
        {"status": "closed", "rug_pct_from_peak": v}
        for v in (20, 22, 21, 23, 19, 22)  # all in 19-23% window
    ]
    assert _predictability_component(trades) >= 80


def test_predictability_component_random_rugs_low_score():
    trades = [
        {"status": "closed", "rug_pct_from_peak": v}
        for v in (5, 80, 30, 50, 15, 70)  # huge spread
    ]
    assert _predictability_component(trades) < 30


def test_compute_score_combines_all_components():
    creator_doc = {
        "tokens_created": 8,
        "tokens_graduated": 1,
        "tokens_failed": 7,
        "first_seen": _iso(72),
        "last_seen": _iso(2),
    }
    trades = [
        {"status": "closed", "pnl_pct": p, "rug_pct_from_peak": r}
        for p, r in [(15, 22), (8, 21), (-5, 23), (12, 20), (3, 22)]
    ]
    failed = [
        {"outcome": "failed", "final_peak_mc_usd": v}
        for v in (40_000, 42_000, 38_000, 41_000, 39_000)  # very consistent
    ]
    result = compute_score(creator_doc, trades, failed)
    assert "score" in result
    assert "expected_peak_mc_usd" in result
    assert result["expected_peak_mc_usd"]["n_failed_with_peak"] == 5
    assert result["components"]["peak_mc"] >= 50  # consistent ~40k peaks
    assert result["n_failed_launches"] == 5


def test_compute_score_no_failed_launches_doesnt_crash():
    creator_doc = {
        "tokens_created": 3,
        "first_seen": _iso(48),
        "last_seen": _iso(1),
    }
    trades = []
    result = compute_score(creator_doc, trades, None)
    assert result["components"]["peak_mc"] == 0.0
    assert result["n_failed_launches"] == 0


# ============================================================================
# Phase 2: strategy_overrides — TP/SL/trail/size per tier
# ============================================================================

def test_strategy_overrides_standard_returns_empty():
    """Standard tier means 'use BotConfig defaults' — no overrides."""
    assert strategy_overrides("standard") == {}


def test_strategy_overrides_unknown_strategy_returns_empty():
    """Defensive: an unknown strategy string can't crash the entry path."""
    assert strategy_overrides("unknown_tier") == {}
    assert strategy_overrides(None) == {}


def test_strategy_overrides_aggressive_full_shape():
    ov = strategy_overrides("aggressive")
    # Must have ALL the keys exit logic reads via _exit_param + the size_mult
    for k in ("size_mult", "tp_pct", "sl_pct", "trail_pct", "trail_arm_pct"):
        assert k in ov, f"missing key {k} in aggressive overrides"
    # Aggressive should be the largest size + tightest exits
    assert ov["size_mult"] >= 1.3
    assert ov["sl_pct"] <= 13.0  # tighter than default 15
    assert ov["trail_pct"] <= 7.0  # tighter than default 8


def test_strategy_overrides_hybrid_is_between_standard_and_aggressive():
    h = strategy_overrides("hybrid")
    a = strategy_overrides("aggressive")
    # Hybrid TP between default (20) and aggressive (35)
    assert 20 <= h["tp_pct"] <= a["tp_pct"]
    # Hybrid size_mult between 1.0 and aggressive
    assert 1.0 <= h["size_mult"] <= a["size_mult"]


def test_strategy_overrides_returns_copy_not_shared_dict():
    """Mutating the returned dict must NOT poison the module-level template."""
    a1 = strategy_overrides("aggressive")
    a1["tp_pct"] = 999.0
    a2 = strategy_overrides("aggressive")
    assert a2["tp_pct"] != 999.0

