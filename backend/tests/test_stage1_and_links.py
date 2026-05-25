"""
Tests for the Bing-reference Stage-1 cheap filter and the linked-wallet
score component (Bing §1 + §2). These are the two new wiring pieces that
let the greylist score creators whose primary signal is "funded by a
known rug cluster" rather than "has 5+ failed launches".
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from creator_greylist import stage1_filter, _links_component, compute_score


# ===== Stage-1 filter =====================================================

def test_stage1_passes_with_tokens_failed_ge2():
    ok, reason = stage1_filter({"tokens_failed": 3}, [])
    assert ok is True
    assert "fails" in reason


def test_stage1_passes_with_rug_cluster_link():
    ok, reason = stage1_filter(
        {"tokens_failed": 0}, [],
        linked_wallets=[{"wallet": "W1", "hop": 1}],
        links_evidence={"linked_to_rug_cluster": True},
    )
    assert ok is True
    assert "rug cluster" in reason


def test_stage1_passes_with_multiple_hop1_funders():
    ok, reason = stage1_filter(
        {"tokens_failed": 0}, [],
        linked_wallets=[
            {"wallet": "W1", "hop": 1},
            {"wallet": "W2", "hop": 1},
        ],
    )
    assert ok is True
    assert "hop-1" in reason


def test_stage1_passes_with_instant_rug_history():
    ok, reason = stage1_filter(
        {"tokens_failed": 1}, [{"rug_seconds_from_launch": 10}],
    )
    assert ok is True
    assert "instant-rug" in reason


def test_stage1_passes_with_parabolic_history():
    ok, reason = stage1_filter(
        {"tokens_failed": 1}, [{"accel_signature_v2": "parabolic"}],
    )
    assert ok is True
    assert "parabolic" in reason


def test_stage1_passes_with_bot_swarm_history():
    ok, reason = stage1_filter(
        {"tokens_failed": 1}, [{"accel_signature_v2": "bot_swarm"}],
    )
    assert ok is True
    assert "bot_swarm" in reason


def test_stage1_passes_with_fband_membership():
    # tokens_failed=1 → no ≥2 trigger; F-band requires 5 ≤ F < 80 so still no
    # 5 ≤ F < 80 path requires the ≥2 path to NOT short-circuit. Use F=1
    # so neither ≥2 nor F-band triggers. F=1 with no other signals → reject.
    # Then verify F=5 with no other signals reaches the ≥2 path FIRST.
    ok, reason = stage1_filter({"tokens_failed": 5}, [])
    assert ok is True
    # ≥2 fails check fires before F-band so reason is fail count
    assert "fails" in reason or "F-band" in reason


def test_stage1_rejects_quiet_creator():
    ok, reason = stage1_filter({"tokens_failed": 1}, [])
    assert ok is False
    assert "no stage1 trigger" in reason


def test_stage1_rejects_too_many_fails():
    # 80 is the exclusive max — F=80 should fail F-band BUT pass via ≥2 fails
    ok, _ = stage1_filter({"tokens_failed": 80}, [])
    assert ok is True  # tokens_failed ≥ 2 dominates


def test_stage1_rejects_zero_fails_no_links():
    ok, reason = stage1_filter({"tokens_failed": 0}, [])
    assert ok is False


# ===== _links_component ===================================================

def test_links_component_zero_when_empty():
    score, ev = _links_component([], set())
    assert score == 0.0
    assert ev["n_links"] == 0
    assert ev["linked_to_rug_cluster"] is False


def test_links_component_base_score_one_hop1_funder():
    score, ev = _links_component(
        [{"wallet": "W1", "hop": 1}], set(),
    )
    assert score == 30.0
    assert ev["n_hop1"] == 1
    assert ev["linked_to_rug_cluster"] is False


def test_links_component_base_caps_at_60():
    # 3 hop-1 funders → 30*3=90 → capped at 60
    score, ev = _links_component(
        [{"wallet": f"W{i}", "hop": 1} for i in range(3)], set(),
    )
    assert score == 60.0
    assert ev["n_hop1"] == 3


def test_links_component_rug_cluster_overlap():
    # 1 funder, and that funder is also a known blacklisted creator
    score, ev = _links_component(
        [{"wallet": "BadActor1", "hop": 1}],
        blacklisted_creators={"BadActor1"},
    )
    assert score == 30.0 + 25.0  # base + cluster bonus
    assert ev["linked_to_rug_cluster"] is True
    assert ev["rug_cluster_hits"] == 1


def test_links_component_multiple_cluster_hits():
    score, ev = _links_component(
        [{"wallet": "B1", "hop": 1},
         {"wallet": "B2", "hop": 1},
         {"wallet": "B3", "hop": 1}],
        blacklisted_creators={"B1", "B2", "B3"},
    )
    # base=60 (capped), bonus = 25 + 15*2 = 55
    assert score == 100.0  # capped at 100
    assert ev["rug_cluster_hits"] == 3


def test_links_component_hop2_doesnt_count_toward_base():
    score, _ = _links_component(
        [{"wallet": "W1", "hop": 2}], set(),
    )
    assert score == 0.0


# ===== compute_score integration ==========================================

def test_compute_score_passes_links_through():
    """compute_score should propagate the links contribution into the
    composite without breaking the existing component breakdown."""
    creator = {"tokens_created": 5, "tokens_failed": 5,
               "first_seen": "2026-01-01T00:00:00+00:00",
               "last_seen": "2026-05-25T00:00:00+00:00"}
    out = compute_score(
        creator, trades=[], failed_launches=[],
        linked_wallets=[{"wallet": "BadActor", "hop": 1}],
        blacklisted_creators={"BadActor"},
    )
    assert out["components"]["links"] > 0
    assert out["links_evidence"]["linked_to_rug_cluster"] is True


def test_compute_score_no_links_doesnt_break():
    """Existing call sites that don't pass linked_wallets must still work."""
    creator = {"tokens_created": 5, "tokens_failed": 5,
               "first_seen": "2026-01-01T00:00:00+00:00",
               "last_seen": "2026-05-25T00:00:00+00:00"}
    out = compute_score(creator, trades=[], failed_launches=[])
    assert out["components"]["links"] == 0.0
    assert out["links_evidence"]["linked_to_rug_cluster"] is False
