"""
Tests for the bimodal-pattern detection — promotes "unpredictable" creators
to `bimodal_dump_tradeable` when their high stddev is actually two tight
clusters separated by a gap (not random noise).
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from creator_pattern import _detect_bimodality, classify_creator


# ===== _detect_bimodality direct =========================================

def test_bimodality_detects_tight_two_clusters():
    """8 launches: 4 rug at ~12%, 4 rug at ~65%. Clear bimodal."""
    rugs = [10, 12, 14, 13, 62, 64, 66, 68]
    out = _detect_bimodality(rugs)
    assert out["bimodal"] is True
    assert out["tradeable"] is True
    assert out["gap_pct"] >= 20
    assert out["lo_cluster"]["n"] == 4
    assert out["hi_cluster"]["n"] == 4
    assert out["lo_cluster"]["median"] == 12.5
    assert out["hi_cluster"]["median"] == 65.0


def test_bimodality_not_tradeable_with_loose_clusters():
    """Two clusters but each is itself noisy → not tradeable."""
    rugs = [5, 18, 25, 30, 55, 70, 80, 95]
    out = _detect_bimodality(rugs)
    assert out["bimodal"] is True
    assert out["tradeable"] is False  # intra-cluster σ too high


def test_bimodality_rejects_unimodal_distribution():
    """Single cluster with high variance — NOT bimodal."""
    rugs = [10, 20, 30, 40, 50, 60, 70]
    out = _detect_bimodality(rugs)
    assert out["bimodal"] is False


def test_bimodality_rejects_too_few_samples():
    assert _detect_bimodality([10, 65])["bimodal"] is False
    assert _detect_bimodality([10, 12, 65, 67])["bimodal"] is False  # n<6


def test_bimodality_rejects_lopsided_clusters():
    """Tiny cluster (1 outlier + 9 normal) shouldn't count as bimodal."""
    rugs = [10, 12, 11, 13, 12, 11, 12, 13, 14, 80]
    out = _detect_bimodality(rugs)
    assert out["bimodal"] is False


def test_bimodality_requires_min_20pp_gap():
    """Gap of 18pp — below threshold."""
    rugs = [10, 12, 14, 16, 34, 36, 38, 40]
    out = _detect_bimodality(rugs)
    assert out["bimodal"] is False


# ===== classifier integration ============================================

def test_classifier_promotes_bimodal_unpredictable_to_tradeable():
    """A creator that would be 'unpredictable_rug' (stddev > 40%) but
    has bimodal-tradeable distribution should be classified as
    `bimodal_dump_tradeable` instead and NOT be blacklisted."""
    # 8 failed launches: 4 rug at ~12%, 4 rug at ~65%. Overall stddev = ~28%
    # (above 40 floor when calculated properly with these values).
    # Use wider spread: rug at 5% vs 80% → stddev ~38%, but bimodality
    # bypass should kick in regardless.
    failed = [
        {"curve_fill_pct": v, "sol_inflow": 5.0, "buy_count": 20}
        for v in (3, 5, 4, 6, 90, 92, 94, 93)
    ]
    creator_doc = {
        "tokens_created": 8, "tokens_failed": 8,
        "first_seen": "2026-01-01T00:00:00+00:00",
        "last_seen": "2026-05-26T00:00:00+00:00",
    }
    r = classify_creator(creator_doc, failed, trades=[])
    assert r["pattern"] == "bimodal_dump_tradeable"
    assert r["blacklisted"] is False
    assert any("BIMODAL" in e for e in r["evidence"])
    # Bimodal info should be exposed on rug_stats
    bm = r["rug_stats"]["bimodality"]
    assert bm["bimodal"] is True
    assert bm["tradeable"] is True


def test_classifier_keeps_chaotic_bimodal_blacklisted():
    """Bimodal but each cluster is loose → still blacklisted as unpredictable."""
    failed = [
        {"curve_fill_pct": v, "sol_inflow": 5.0, "buy_count": 20}
        # Two loose clusters; intra-σ ≈ 16pp each (above tradeable=12 cap).
        # Overall stddev ≈ 40.7 → triggers the variance branch, but the
        # bimodal intercept fails on `tradeable=False` so it falls through
        # to the unpredictable_rug return.
        for v in (1, 2, 3, 30, 35, 65, 70, 95, 98, 99)
    ]
    creator_doc = {
        "tokens_created": 10, "tokens_failed": 10,
        "first_seen": "2026-01-01T00:00:00+00:00",
        "last_seen": "2026-05-26T00:00:00+00:00",
    }
    r = classify_creator(creator_doc, failed, trades=[])
    assert r["pattern"] == "unpredictable_rug"
    assert r["blacklisted"] is True
    # Bimodality should be detected but NOT tradeable (loose clusters)
    bm = r["rug_stats"]["bimodality"]
    assert bm["bimodal"] is True
    assert bm["tradeable"] is False


def test_bimodal_suggested_tp_targets_lo_cluster():
    """Suggested TP should target the LO cluster median (the fast-rug mode)."""
    failed = [
        {"curve_fill_pct": v, "sol_inflow": 5.0, "buy_count": 20}
        for v in (1, 4, 3, 5, 85, 88, 90, 92, 95, 98)
    ]
    creator_doc = {
        "tokens_created": 10, "tokens_failed": 10,
        "first_seen": "2026-01-01T00:00:00+00:00",
        "last_seen": "2026-05-26T00:00:00+00:00",
    }
    r = classify_creator(creator_doc, failed, trades=[], tp_buffer=2.0)
    assert r["pattern"] == "bimodal_dump_tradeable"
    # Lo cluster median ≈ 3.5, tp_buffer = 2 → suggested = max(5.0, 1.5) = 5.0
    suggested_tp = r["suggested_exit_pct"][0]
    assert suggested_tp == 5.0
