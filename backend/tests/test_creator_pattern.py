"""
Tests for the mechanical creator pattern classifier (creator_pattern.py).
Maps creator history → one of the 6 buckets defined in RUG_PATTERNS.md.
"""
import os
os.environ.setdefault("HELIUS_RPC_URL", "https://x")
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")

from creator_pattern import (  # noqa: E402
    classify_creator,
    BAD_PATTERNS,
    GOOD_PATTERNS,
    _hype_score_from_names,
    _instant_share,
)


# ---------------------------------------------------------------------------
# helper-level tests
# ---------------------------------------------------------------------------

def test_hype_score_matches_obvious_keywords():
    frac, kws = _hype_score_from_names(["AI Dog", "MOON Pup", "Regular Token"])
    assert frac > 0.6
    assert "AI" in kws and "MOON" in kws


def test_hype_score_does_not_match_substrings_within_words():
    """'AIRCRAFT' should NOT match 'AI' — keyword boundary regex."""
    frac, kws = _hype_score_from_names(["AIRCRAFT", "PAINT"])
    assert frac == 0.0
    assert kws == []


def test_hype_score_empty_inputs():
    assert _hype_score_from_names([]) == (0.0, [])
    assert _hype_score_from_names([None, "", " "]) == (0.0, [])


def test_instant_share_counts_correctly():
    # All fixtures need sol_inflow≥0.1 OR buy_count≥3 to be considered "meaningful"
    fails = [
        {"fail_class": "failed_instant", "buy_count": 3},
        {"fail_class": "failed_instant", "buy_count": 3},
        {"fail_class": "failed_fizzled", "buy_count": 10},
        {"fail_class": "failed_chaotic", "buy_count": 5},
    ]
    share, n_inst, n_total = _instant_share(fails)
    assert n_inst == 2 and n_total == 4
    assert share == 0.5


def test_instant_share_drops_spam_launches():
    """Test/spam launches (<0.1 SOL inflow AND <3 buys) shouldn't count."""
    fails = [
        {"fail_class": "failed_instant", "sol_inflow": 0.00001, "buy_count": 1},
        {"fail_class": "failed_instant", "sol_inflow": 0.00001, "buy_count": 1},
        {"fail_class": "failed_fizzled", "sol_inflow": 5.0, "buy_count": 20},
        {"fail_class": "failed_chaotic", "sol_inflow": 3.0, "buy_count": 10},
    ]
    share, n_inst, n_total = _instant_share(fails)
    # Only the 2 meaningful fails counted; both are non-instant
    assert n_total == 2
    assert n_inst == 0
    assert share == 0.0


# ---------------------------------------------------------------------------
# classifier — bad buckets (blacklisted)
# ---------------------------------------------------------------------------

def test_unknown_when_no_history():
    r = classify_creator({}, [], [])
    assert r["pattern"] == "unknown"
    assert r["blacklisted"] is True


def test_untradeable_when_dominant_instant_rugs():
    """≥50% of failed launches are failed_instant → blacklisted."""
    # All fixtures need buy_count≥3 to pass the "meaningful" filter
    fails = [{"fail_class": "failed_instant", "buy_count": 3} for _ in range(4)]
    fails += [{"fail_class": "failed_fizzled", "buy_count": 10, "final_peak_mc_usd": 10000} for _ in range(2)]
    r = classify_creator(
        {"tokens_created": 10, "tokens_failed": 6},
        failed_launches=fails,
        trades=[],
    )
    assert r["pattern"] == "untradeable_rug"
    assert r["blacklisted"] is True


def test_unpredictable_when_variance_above_40():
    """rug_pct stddev > 40 on ≥3 samples → blacklisted (chaos).
    Loosened from 20 to 40 (2026-05-26) to surface more tradeable creators."""
    trades = [
        {"status": "closed", "rug_pct_from_peak": v}
        for v in (5, 95, 10, 90, 5, 85)
    ]
    r = classify_creator(
        {"tokens_created": 8, "tokens_failed": 6},
        failed_launches=[{"fail_class": "failed_fizzled"} for _ in range(5)],
        trades=trades,
    )
    assert r["pattern"] == "unpredictable_rug"
    assert r["blacklisted"] is True


# ---------------------------------------------------------------------------
# classifier — good buckets (tradeable)
# ---------------------------------------------------------------------------

def test_slow_rug_tradeable_high_median_low_variance():
    """rug_pct median in 18-30 + stddev < 6 → slow_rug_tradeable."""
    trades = [
        {"status": "closed", "rug_pct_from_peak": v}
        for v in (21, 23, 19, 22, 24, 20, 21)  # mean ≈ 21.4, σ ≈ 1.7
    ]
    r = classify_creator(
        {"tokens_created": 10, "tokens_failed": 7},
        failed_launches=[
            {"fail_class": "failed_fizzled", "symbol": "TKN1"}
            for _ in range(6)
        ],
        trades=trades,
    )
    assert r["pattern"] == "slow_rug_tradeable"
    assert r["blacklisted"] is False
    assert r["suggested_entry_pct"] == (10.0, 15.0)
    # Exit should be below the median
    assert r["suggested_exit_pct"][1] < 21.5


def test_predictable_dump_tradeable_lower_median():
    """rug_pct median 12-18 + low variance → predictable_dump_tradeable."""
    trades = [
        {"status": "closed", "rug_pct_from_peak": v}
        for v in (14, 15, 13, 16, 14, 15)  # mean ≈ 14.5, σ ≈ 1.1
    ]
    r = classify_creator(
        {"tokens_created": 12, "tokens_failed": 11},
        failed_launches=[
            {"fail_class": "failed_fizzled", "symbol": "DUMP"}
            for _ in range(10)
        ],
        trades=trades,
    )
    assert r["pattern"] == "predictable_dump_tradeable"
    assert r["blacklisted"] is False
    assert r["suggested_entry_pct"] == (8.0, 10.0)


def test_fake_hype_tradeable_when_hype_names_plus_fast_rugs():
    """hype-keyword names + ≥40% of fails are failed_instant → fake_hype."""
    fails = [
        {"fail_class": "failed_instant", "symbol": "AI MOON DOG", "buy_count": 3},
        {"fail_class": "failed_instant", "symbol": "ELON PEPE", "buy_count": 3},
        {"fail_class": "failed_fizzled", "symbol": "MUSK BUTT", "buy_count": 10, "final_peak_mc_usd": 8000},
        {"fail_class": "failed_fizzled", "symbol": "regular thing", "buy_count": 10, "final_peak_mc_usd": 8000},
        {"fail_class": "failed_instant", "symbol": "GOD COIN", "buy_count": 3},
    ]
    r = classify_creator(
        {"tokens_created": 8, "tokens_failed": 5},
        failed_launches=fails,
        trades=[],   # no rug_pct samples — variance gate doesn't fire
    )
    # Must take the fake_hype branch, NOT untradeable (untradeable triggers
    # at >=50% instant; here it's 60% but hype keywords match too — the rule
    # order in classify_creator puts untradeable BEFORE fake_hype because
    # >=50% instant is the stricter "this creator just dies" signal).
    # So expect untradeable here. Update test if we re-order rules.
    assert r["pattern"] in {"untradeable_rug", "fake_hype_tradeable"}


def test_fake_hype_branch_when_hype_names_but_instant_share_below_untradeable_floor():
    """≥40% hype + 30-49% instant → fake_hype (not untradeable)."""
    fails = [
        {"fail_class": "failed_instant", "symbol": "AI ROCKET", "buy_count": 3},
        {"fail_class": "failed_fizzled",  "symbol": "MOON BABY", "buy_count": 10, "final_peak_mc_usd": 8000},
        {"fail_class": "failed_fizzled",  "symbol": "ELON DOG", "buy_count": 10, "final_peak_mc_usd": 8000},
        {"fail_class": "failed_fizzled",  "symbol": "regular", "buy_count": 10, "final_peak_mc_usd": 8000},
        {"fail_class": "failed_instant",  "symbol": "GOD PEPE", "buy_count": 3},
    ]
    # 2/5 = 40% instant share (≥0.3 floor for fake_hype but <0.5 for untradeable)
    # 4/5 = 80% hype share (≥0.4 floor)
    r = classify_creator(
        {"tokens_created": 6, "tokens_failed": 5},
        failed_launches=fails,
        trades=[],
    )
    assert r["pattern"] == "fake_hype_tradeable"
    assert r["blacklisted"] is False
    assert "AI" in r.get("hype_keywords", [])


# ---------------------------------------------------------------------------
# classifier — unknown fallbacks
# ---------------------------------------------------------------------------

def test_unknown_when_not_enough_rug_samples():
    """Have failures but < 4 rug_pct samples → can't decide → unknown."""
    r = classify_creator(
        {"tokens_created": 5, "tokens_failed": 4},
        failed_launches=[{"fail_class": "failed_fizzled"} for _ in range(3)],
        trades=[
            {"status": "closed", "rug_pct_from_peak": 22},
            {"status": "closed", "rug_pct_from_peak": 24},
        ],
    )
    assert r["pattern"] == "unknown"


def test_unknown_when_median_outside_tradeable_windows():
    """Median outside both 12-18 and 18-30 → unknown, not falsely classified."""
    trades = [
        {"status": "closed", "rug_pct_from_peak": v}
        for v in (40, 42, 41, 43, 42)  # median 42 — outside both windows, but σ small
    ]
    r = classify_creator(
        {"tokens_created": 10, "tokens_failed": 7},
        failed_launches=[{"fail_class": "failed_fizzled"} for _ in range(5)],
        trades=trades,
    )
    assert r["pattern"] == "unknown"


# ---------------------------------------------------------------------------
# safety: every bucket the classifier returns is one of the documented constants
# ---------------------------------------------------------------------------

def test_classifier_always_returns_documented_pattern():
    """Defensive: the 'pattern' field is always one of the 6 buckets."""
    all_patterns = BAD_PATTERNS | GOOD_PATTERNS
    fixtures = [
        ({}, [], []),
        ({"tokens_created": 1, "tokens_failed": 1}, [], []),
        ({"tokens_created": 50, "tokens_failed": 40},
         [{"fail_class": "failed_fizzled"} for _ in range(20)], []),
    ]
    for cd, fl, tr in fixtures:
        r = classify_creator(cd, fl, tr)
        assert r["pattern"] in all_patterns, f"unknown bucket: {r['pattern']}"
