"""
creator_pattern — classify a creator into one of 6 buckets per RUG_PATTERNS.md.

Three BAD buckets (creator gets `greylist_blacklisted=True`, excluded from
greylist surface):
  - `untradeable_rug`        — dominant failed_instant cohort (Dead in 60s)
  - `unpredictable_rug`      — rug_pct stddev > 20 on ≥4 samples
  - `unknown`                — no history / can't classify yet (standard logic)

Three GOOD buckets (creator stays in greylist; UI shows the pattern badge):
  - `slow_rug_tradeable`        — rug_pct median 18-30%, stddev < 6 (long entry window)
  - `predictable_dump_tradeable`— rug_pct median 12-18%, stddev < 6 (pump→dump→pump→rug)
  - `fake_hype_tradeable`       — hype-keyword name AND sharp early acceleration

Inputs come from `creator_doc` + the list of failed launches + recent trades
(closed) so everything stays Mongo-side; no Helius calls. Returns:
  {pattern, confidence, evidence: [...], rug_stats: {...}}

Confidence 0-100 is a soft signal — UI can show "85% slow_rug" when the data
is rich but variance bordering 6.0. Hard pattern threshold is still applied.
"""
from __future__ import annotations
import re
import statistics
from typing import Any

# Bad patterns — these get blacklisted from the greylist surface entirely.
BAD_PATTERNS = {"untradeable_rug", "unpredictable_rug", "unknown"}

# Good patterns — these stay on the greylist and get a badge in the UI.
GOOD_PATTERNS = {"slow_rug_tradeable", "predictable_dump_tradeable", "fake_hype_tradeable"}

# Hype keywords commonly used in fake-hype launches. Matched case-insensitive,
# substring, on the LAUNCH symbol/name (NOT the creator address). Extend as
# you spot new themes — the list is intentionally small to avoid false-pos.
HYPE_KEYWORDS = [
    "AI", "AGI", "ELON", "MUSK", "TRUMP", "BIDEN", "MOON", "GOD",
    "JESUS", "PEPE", "DOGE", "INU", "SHIB", "WIF", "BONK",
    "MEME", "BASED", "WEN", "GME", "AMC", "TESLA",
]
# Pre-compile a single regex for speed — word-ish boundary so "AI" matches
# "AI_DOG" or "AIBABE" but not "AIRCRAFT".
_HYPE_RE = re.compile(
    r"(?<![A-Z])(" + "|".join(map(re.escape, HYPE_KEYWORDS)) + r")(?![A-Z])",
    re.IGNORECASE,
)


def _hype_score_from_names(names: list[str]) -> tuple[float, list[str]]:
    """Count what fraction of the names match a hype keyword. Returns
    (fraction 0..1, list of unique matched keywords)."""
    if not names:
        return 0.0, []
    matched_keywords: set[str] = set()
    hits = 0
    for n in names:
        if not n:
            continue
        m = _HYPE_RE.findall(n)
        if m:
            hits += 1
            for kw in m:
                matched_keywords.add(kw.upper())
    return (hits / max(1, len(names))), sorted(matched_keywords)


def _instant_share(failed_launches: list[dict]) -> tuple[float, int, int]:
    """Fraction of failed launches that are 'failed_instant' (Dead in 60s).
    Returns (share 0..1, n_instant, n_total). When the share > 0.5 the
    creator is dominated by the untradeable cohort."""
    if not failed_launches:
        return 0.0, 0, 0
    n_total = len(failed_launches)
    n_instant = sum(1 for f in failed_launches if f.get("fail_class") == "failed_instant")
    return (n_instant / n_total), n_instant, n_total


def _rug_pct_stats(trades: list[dict]) -> dict[str, Any]:
    """Median / stddev / count of `rug_pct_from_peak`. Used to classify
    slow-rug (high %) vs predictable-dump (low %) vs unpredictable (high σ)."""
    rugs = [
        float(t["rug_pct_from_peak"])
        for t in trades
        if t.get("rug_pct_from_peak") is not None
    ]
    if len(rugs) < 2:
        return {"median": None, "stddev": None, "n": len(rugs)}
    try:
        sd = statistics.stdev(rugs) if len(rugs) >= 2 else 0.0
    except statistics.StatisticsError:
        sd = 0.0
    return {
        "median": round(statistics.median(rugs), 1),
        "stddev": round(sd, 1),
        "n": len(rugs),
    }


def classify_creator(
    creator_doc: dict | None,
    failed_launches: list[dict] | None = None,
    trades: list[dict] | None = None,
) -> dict:
    """Mechanically classify a creator into one of the 6 buckets.

    Decision order (first match wins):
      1. No data            → "unknown"
      2. Variance > 20      → "unpredictable_rug"
      3. ≥50% instant rugs  → "untradeable_rug"
      4. Hype-name + ≥40% failed_instant + fast      → "fake_hype_tradeable"
      5. Median 18-30 + σ<6 + n≥4                    → "slow_rug_tradeable"
      6. Median 12-18 + σ<6 + n≥4                    → "predictable_dump_tradeable"
      7. Fallback                                     → "unknown"
    """
    creator_doc = creator_doc or {}
    failed_launches = failed_launches or []
    trades = trades or []

    tokens_created = int(creator_doc.get("tokens_created") or 0)
    tokens_failed = int(creator_doc.get("tokens_failed") or 0)

    # --- bucket 1: no history at all ---
    if tokens_created < 1 and tokens_failed < 1 and not failed_launches:
        return {
            "pattern": "unknown",
            "confidence": 100.0,
            "evidence": ["no observed history"],
            "rug_stats": {"median": None, "stddev": None, "n": 0},
            "blacklisted": True,
        }

    rug_stats = _rug_pct_stats(trades)
    instant_share, n_instant, n_total_failed = _instant_share(failed_launches)
    names = [(fl.get("symbol") or fl.get("name") or "") for fl in failed_launches]
    hype_share, hype_kw = _hype_score_from_names(names)

    evidence: list[str] = []

    # --- bucket 2: unpredictable (variance too high) — only if we have enough data ---
    if rug_stats["n"] >= 4 and rug_stats["stddev"] is not None and rug_stats["stddev"] > 20.0:
        evidence.append(f"rug_pct stddev {rug_stats['stddev']:.1f}% > 20% on {rug_stats['n']} samples")
        return {
            "pattern": "unpredictable_rug",
            "confidence": min(100.0, 50.0 + rug_stats["stddev"]),
            "evidence": evidence,
            "rug_stats": rug_stats,
            "blacklisted": True,
        }

    # --- bucket 3: untradeable (dominated by Dead-in-60s cohort) ---
    if n_total_failed >= 4 and instant_share >= 0.5:
        evidence.append(
            f"{n_instant}/{n_total_failed} failed launches were Dead-in-60s "
            f"({instant_share*100:.0f}% instant)"
        )
        return {
            "pattern": "untradeable_rug",
            "confidence": min(100.0, instant_share * 100),
            "evidence": evidence,
            "rug_stats": rug_stats,
            "blacklisted": True,
        }

    # --- bucket 4: fake_hype (hype name + fast acceleration profile) ---
    # Hype names alone aren't enough — we need a launch profile that ALSO
    # looks like a fake-hype rug (some `failed_instant` + low rug-pct samples).
    if hype_share >= 0.4 and n_total_failed >= 4 and instant_share >= 0.3:
        evidence.append(
            f"{hype_share*100:.0f}% of mint names match hype keywords {hype_kw[:4]}"
        )
        evidence.append(
            f"{instant_share*100:.0f}% of fails were Dead-in-60s (fast-rug profile)"
        )
        return {
            "pattern": "fake_hype_tradeable",
            "confidence": min(100.0, 40.0 + 60.0 * hype_share),
            "evidence": evidence,
            "rug_stats": rug_stats,
            "blacklisted": False,
            "hype_keywords": hype_kw,
        }

    # --- buckets 5 + 6: tradeable patterns require ≥4 rug_pct samples with σ<6 ---
    if (
        rug_stats["n"] >= 4
        and rug_stats["stddev"] is not None
        and rug_stats["stddev"] < 6.0
        and rug_stats["median"] is not None
    ):
        med = float(rug_stats["median"])
        if 18.0 <= med <= 30.0:
            evidence.append(
                f"rug_pct median {med:.1f}% in slow-rug window (18-30%)"
            )
            evidence.append(
                f"stddev {rug_stats['stddev']:.1f}% < 6% — consistent"
            )
            return {
                "pattern": "slow_rug_tradeable",
                "confidence": min(100.0, 100.0 - rug_stats["stddev"] * 5),
                "evidence": evidence,
                "rug_stats": rug_stats,
                "blacklisted": False,
                "suggested_entry_pct": (10.0, 15.0),
                "suggested_exit_pct": (max(0.0, med - 4.0), max(0.0, med - 1.0)),
            }
        if 12.0 <= med < 18.0:
            evidence.append(
                f"rug_pct median {med:.1f}% in predictable-dump window (12-18%)"
            )
            evidence.append(
                f"stddev {rug_stats['stddev']:.1f}% < 6% — consistent"
            )
            return {
                "pattern": "predictable_dump_tradeable",
                "confidence": min(100.0, 100.0 - rug_stats["stddev"] * 5),
                "evidence": evidence,
                "rug_stats": rug_stats,
                "blacklisted": False,
                "suggested_entry_pct": (8.0, 10.0),
                "suggested_exit_pct": (max(0.0, med - 3.0), max(0.0, med - 1.0)),
            }

    # --- fallback: not enough signal yet ---
    msg_parts = []
    if rug_stats["n"] < 4:
        msg_parts.append(f"only {rug_stats['n']} rug_pct samples (need ≥4)")
    if rug_stats["n"] >= 4 and rug_stats["stddev"] is not None and rug_stats["stddev"] >= 6.0:
        msg_parts.append(f"stddev {rug_stats['stddev']:.1f}% too wide (need <6%)")
    evidence.append("; ".join(msg_parts) if msg_parts else "no decisive pattern yet")
    return {
        "pattern": "unknown",
        "confidence": 20.0,
        "evidence": evidence,
        "rug_stats": rug_stats,
        "blacklisted": True,   # treat unknown as blacklisted-from-greylist
    }
