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
    """Fraction of *meaningful* failed launches that are 'failed_instant'.
    
    "Meaningful" excludes spam/test launches (sol_inflow < 0.1 SOL with
    only 1-2 buys). A creator who routinely deploys test mints at 0.00001
    SOL would otherwise show 95% 'instant' just from noise launches that
    have nothing to do with their actual rug pattern.
    
    Returns (share 0..1, n_instant, n_meaningful_total). When the share
    > 0.5 the creator is dominated by the untradeable cohort."""
    if not failed_launches:
        return 0.0, 0, 0
    # Filter out spam (no real money attempted)
    meaningful = [
        f for f in failed_launches
        if (float(f.get("sol_inflow") or 0) >= 0.1
            or int(f.get("buy_count") or 0) >= 3)
    ]
    if not meaningful:
        return 0.0, 0, 0
    n_total = len(meaningful)
    n_instant = sum(1 for f in meaningful if f.get("fail_class") == "failed_instant")
    return (n_instant / n_total), n_instant, n_total


def _peak_mc_stats(failed_launches: list[dict]) -> dict[str, Any]:
    """Peak MC distribution across the creator's FAILED launches.
    This is the user's primary signal: 'whats most important is their
    median mc across all fails — so we know when its too late'.

    Excludes `failed_instant` (Dead-in-60s) since their peak MCs are tiny
    and would drag the median artificially low. A creator who consistently
    rugs at $50k MC is the signal we want, not one whose 'fails' were 80%
    rugged within 60s with no real volume."""
    peaks = [
        float(f["final_peak_mc_usd"])
        for f in failed_launches
        if (f.get("final_peak_mc_usd") and f.get("fail_class") != "failed_instant")
    ]
    if len(peaks) < 2:
        return {"median": None, "mean": None, "cv": None, "n": len(peaks)}
    mean = statistics.mean(peaks)
    try:
        sd = statistics.stdev(peaks) if len(peaks) >= 2 else 0.0
    except statistics.StatisticsError:
        sd = 0.0
    cv = sd / mean if mean else 0.0
    return {
        "median": round(statistics.median(peaks), 0),
        "mean": round(mean, 0),
        "stddev": round(sd, 0),
        "cv": round(cv, 3),
        "n": len(peaks),
    }


def _fail_class_breakdown(failed_launches: list[dict]) -> dict[str, float]:
    """Share of each fail_class. Used to distinguish:
      - slow_rug         → mostly `failed_fizzled` (slow death over hours/days)
      - heavier_dump     → mostly `failed_chaotic` (volume + abrupt rug)
      - untradeable      → mostly `failed_instant` (Dead in 60s)
    """
    if not failed_launches:
        return {}
    n = len(failed_launches)
    shares: dict[str, float] = {}
    for f in failed_launches:
        k = f.get("fail_class") or "failed"
        shares[k] = shares.get(k, 0.0) + 1.0 / n
    return shares


def _rug_pct_stats(trades: list[dict], failed_launches: list[dict] | None = None) -> dict[str, Any]:
    """Median / stddev / count of where this creator's launches typically
    rug. Used to classify slow-rug (high %) vs predictable-dump (low %)
    vs unpredictable (high σ).

    Two data sources, in priority order:
      1. **Failed launch `curve_fill_pct`** — the % the bonding curve had
         filled when the launch died. This is the PRIMARY signal because
         we have ~thousands of failed launches with this data, but only
         ~tens of closed trades. Without this fallback, the classifier
         can't produce a pattern for 99% of creators.
      2. **Trade `rug_pct_from_peak`** — historical legacy: the drop from
         our trade's peak price to exit price. Useful when we've actually
         traded the creator, mostly empty.

    We use launch curve_fill_pct as the proxy for "where the curve rugged"
    because pump.fun bonding curves are monotonic with cumulative net
    buys — when the curve fills 40% then dies, it died near the 40% mark.
    """
    # Primary: failed-launch curve_fill_pct. Only include curves that
    # actually started filling (>= 1%) so dead-instant launches don't
    # bias the median toward 0.
    rugs: list[float] = []
    if failed_launches:
        rugs.extend([
            float(fl["curve_fill_pct"])
            for fl in failed_launches
            if fl.get("curve_fill_pct") is not None
            and float(fl["curve_fill_pct"] or 0) >= 1.0
        ])
    # Secondary: trade rug_pct (legacy / sparse data)
    if not rugs and trades:
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
    tp_buffer: float = 2.0,
    all_launches: list[dict] | None = None,
) -> dict:
    """Mechanically classify a creator into one of the 6 buckets.

    `all_launches` (optional) supplies per-launch behavioral signatures
    (accel_class / flow_class / rug_speed_class) for the repeatability
    bonus per Bing reference. When provided, the classifier adds an
    `signatures` summary to the result with dominant accel/flow + a
    repeatability % that the scorer uses for the +15 Bing bonus.

    `tp_buffer` is the % subtracted from the observed median rug to compute
    the LOWER bound of `suggested_exit_pct` (which Phase 2.7 uses as the TP
    override). e.g. median rug 20% + buffer 2% → exit lo=18%. Smaller buffer
    = more upside captured but tighter to the actual rug edge.

    Decision order (first match wins):
      1. No data            → "unknown"
      2. Variance > 20      → "unpredictable_rug"
      3. ≥50% instant rugs  → "untradeable_rug"
      4. Hype-name + ≥40% failed_instant + fast      → "fake_hype_tradeable"
      5. Median 18-30 + σ<6 + n≥4                    → "slow_rug_tradeable"
      6. Median 12-18 + σ<6 + n≥4                    → "predictable_dump_tradeable"
      7. Fallback                                     → "unknown"
    """
    # === Original classify_creator return value ===
    # We don't inject signatures here — call sites that need them use the
    # wrapper at the bottom of this module (`classify_with_signatures`).
    creator_doc = creator_doc or {}
    failed_launches = failed_launches or []
    trades = trades or []

    # Compute behavioral signature aggregate (Bing reference: acceleration
    # pattern repeatability gives +15 to creators whose launches share a
    # dominant accel + flow signature). Uses `all_launches` when provided
    # (includes both failed AND graduated mints — the signature is about
    # creator behavior across ALL launches), falling back to failed_launches.
    from launch_signatures import aggregate_signatures
    sig_source = all_launches if all_launches is not None else failed_launches
    signatures = aggregate_signatures(sig_source) if sig_source else {}
    _ = signatures  # consumed by post-processor below; kept here for context

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

    rug_stats = _rug_pct_stats(trades, failed_launches)
    mc_stats = _peak_mc_stats(failed_launches)
    fail_classes = _fail_class_breakdown(failed_launches)
    instant_share, n_instant, n_total_failed = _instant_share(failed_launches)
    names = [(fl.get("symbol") or fl.get("name") or "") for fl in failed_launches]
    hype_share, hype_kw = _hype_score_from_names(names)

    evidence: list[str] = []

    # --- bucket 2: unpredictable (variance too high) ---
    # Two paths: rug_pct stddev (when we have trades) OR peak_mc CV (when
    # we only have launch data, which is the common case since we won't
    # be trading these creators in standard mode).
    if rug_stats["n"] >= 4 and rug_stats["stddev"] is not None and rug_stats["stddev"] > 20.0:
        evidence.append(f"rug_pct stddev {rug_stats['stddev']:.1f}% > 20% on {rug_stats['n']} samples")
        return {
            "pattern": "unpredictable_rug",
            "confidence": min(100.0, 50.0 + rug_stats["stddev"]),
            "evidence": evidence,
            "rug_stats": rug_stats,
            "mc_stats": mc_stats,
            "blacklisted": True,
        }
    if mc_stats["n"] >= 5 and mc_stats["cv"] is not None and mc_stats["cv"] > 0.55:
        evidence.append(
            f"peak MC CV {mc_stats['cv']:.2f} > 0.55 on {mc_stats['n']} fails "
            f"(median ${int(mc_stats['median']):,}, mean ${int(mc_stats['mean']):,}, "
            f"σ ${int(mc_stats['stddev']):,}) — no consistent peak"
        )
        return {
            "pattern": "unpredictable_rug",
            "confidence": min(100.0, 50.0 + mc_stats["cv"] * 50),
            "evidence": evidence,
            "rug_stats": rug_stats,
            "mc_stats": mc_stats,
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
            "mc_stats": mc_stats,
            "blacklisted": True,
        }

    # === PRIMARY PATH (launch-data classification) =========================
    # Looser thresholds than the pre-Bing-reference version so creators with
    # PARTIAL signal (3-4 fails, moderate CV) still surface at low confidence
    # instead of getting silently blacklisted as "unknown". Matches the
    # Bing classifier's incremental-scoring philosophy: score everything we
    # see, blacklist only the clearly-bad patterns.
    if mc_stats["n"] >= 3 and mc_stats["cv"] is not None and mc_stats["cv"] <= 0.60:
        median_mc = float(mc_stats["median"])
        fizzled_share = fail_classes.get("failed_fizzled", 0.0)
        chaotic_share = fail_classes.get("failed_chaotic", 0.0)
        # Consistency points: tight CV → high score (range 0..100). Linear
        # interpolation: CV 0 = 100pts, CV 0.60 = 0pts.
        consistency_pts = max(0.0, (0.60 - mc_stats["cv"]) / 0.60 * 100)
        mc_evidence = (
            f"median peak MC ${int(median_mc):,} "
            f"(σ ${int(mc_stats['stddev']):,}, CV {mc_stats['cv']:.2f}) "
            f"across {mc_stats['n']} fails"
        )

        # Pattern A: fake_hype — hype name + slow death (fizzled-dominant)
        if hype_share >= 0.4 and fizzled_share >= 0.3:
            evidence.append(mc_evidence)
            evidence.append(
                f"{hype_share*100:.0f}% of mint names match hype keywords {hype_kw[:4]}"
            )
            evidence.append(
                f"{fizzled_share*100:.0f}% slow-fizzle pattern (trendy-name signature)"
            )
            return {
                "pattern": "fake_hype_tradeable",
                "confidence": min(95.0, 30.0 + 0.6 * consistency_pts),
                "evidence": evidence,
                "rug_stats": rug_stats,
                "mc_stats": mc_stats,
                "blacklisted": False,
                "hype_keywords": hype_kw,
                "median_peak_mc_usd": median_mc,
                "stop_mc_usd": median_mc * 0.7,
            }

        # Pattern B: predictable_dump — chaotic-significant share
        if chaotic_share >= 0.25 and median_mc >= 5_000:
            evidence.append(mc_evidence)
            evidence.append(
                f"{chaotic_share*100:.0f}% `failed_chaotic` — heavier dump signature"
            )
            return {
                "pattern": "predictable_dump_tradeable",
                "confidence": min(95.0, 25.0 + 0.7 * consistency_pts),
                "evidence": evidence,
                "rug_stats": rug_stats,
                "mc_stats": mc_stats,
                "blacklisted": False,
                "suggested_entry_pct": (8.0, 10.0),
                "median_peak_mc_usd": median_mc,
                "stop_mc_usd": median_mc * 0.7,
            }

        # Pattern C: slow_rug — fizzled-dominant + consistent peak
        if fizzled_share >= 0.4 and median_mc >= 8_000:
            evidence.append(mc_evidence)
            evidence.append(
                f"{fizzled_share*100:.0f}% `failed_fizzled` — slow-rug signature "
                f"(creator + seeded wallets pull at consistent curve %)"
            )
            return {
                "pattern": "slow_rug_tradeable",
                "confidence": min(95.0, 30.0 + 0.65 * consistency_pts),
                "evidence": evidence,
                "rug_stats": rug_stats,
                "mc_stats": mc_stats,
                "blacklisted": False,
                "suggested_entry_pct": (10.0, 15.0),
                "median_peak_mc_usd": median_mc,
                "stop_mc_usd": median_mc * 0.7,
            }

        # Tight-MC creator that didn't match any clean bucket signature →
        # surface as `unknown` BUT NOT BLACKLISTED. Per the Bing reference:
        # score everything we see, even partial signal, so the user can
        # observe these candidates in the panel. The composite score will
        # be modest (driven by component math in compute_score) so they
        # naturally sit below the strong patterns.
        evidence.append(mc_evidence)
        evidence.append(
            f"fail_class mix: fizzled {fizzled_share*100:.0f}% / "
            f"chaotic {chaotic_share*100:.0f}% / instant {instant_share*100:.0f}% — "
            f"no clean pattern signature yet (still watchable)"
        )
        return {
            "pattern": "unknown",
            "confidence": min(70.0, 30.0 + 0.4 * consistency_pts),
            "evidence": evidence,
            "rug_stats": rug_stats,
            "mc_stats": mc_stats,
            "blacklisted": False,   # Watchable, not blacklisted
            "median_peak_mc_usd": median_mc,
            "stop_mc_usd": median_mc * 0.7,
        }

    # --- legacy bucket 4 retained for back-compat with prior trade-based
    # fake_hype detection. Only fires if launch path above didn't match.
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
            lo = max(0.0, med - tp_buffer)
            hi = max(0.0, med - min(1.0, tp_buffer / 2))
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
                "suggested_exit_pct": (lo, hi),
            }
        if 12.0 <= med < 18.0:
            lo = max(0.0, med - tp_buffer)
            hi = max(0.0, med - min(1.0, tp_buffer / 2))
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
                "suggested_exit_pct": (lo, hi),
            }

    # --- fallback: not enough signal yet ---
    msg_parts = []
    if mc_stats["n"] < 3:
        msg_parts.append(f"only {mc_stats['n']} meaningful fails with peak data (need ≥3)")
    if rug_stats["n"] < 4:
        msg_parts.append(f"only {rug_stats['n']} rug_pct samples (need ≥4)")
    if rug_stats["n"] >= 4 and rug_stats["stddev"] is not None and rug_stats["stddev"] >= 6.0:
        msg_parts.append(f"stddev {rug_stats['stddev']:.1f}% too wide (need <6%)")
    evidence.append("; ".join(msg_parts) if msg_parts else "no decisive pattern yet")
    # Watchable when we have ANY lifetime fail history — score will be
    # modest but the creator stays visible in the panel so the user can
    # observe additional launches accumulate.
    has_fail_history = (
        n_total_failed >= 3
        or tokens_failed >= 5
        or mc_stats["n"] >= 1
    )
    return {
        "pattern": "unknown",
        "confidence": 25.0 if has_fail_history else 60.0,
        "evidence": evidence,
        "rug_stats": rug_stats,
        "mc_stats": mc_stats,
        "blacklisted": not has_fail_history,
    }



def classify_with_signatures(
    creator_doc: dict | None,
    failed_launches: list[dict] | None = None,
    trades: list[dict] | None = None,
    tp_buffer: float = 2.0,
    all_launches: list[dict] | None = None,
) -> dict:
    """Wrapper around `classify_creator` that ALSO returns the per-creator
    behavioral signatures (Bing reference inputs) — accel/flow/rug_speed
    distributions + repeatability %. Used by `update_creator_score` so the
    UI can surface "creator launches consistently arrive with high SOL
    inflow and broad participation — repeatability 87%".

    The repeatability % feeds the Bing-formula +15 bonus (proportional to
    consistency) on top of the base pattern score.
    """
    from launch_signatures import aggregate_signatures
    sig_source = all_launches if all_launches is not None else (failed_launches or [])
    signatures = aggregate_signatures(sig_source) if sig_source else {}

    result = classify_creator(creator_doc, failed_launches, trades,
                              tp_buffer=tp_buffer, all_launches=all_launches)
    result["signatures"] = signatures

    # Add the dominant signature to the evidence trail when we have enough
    # data — surfaces the "behavioral fingerprint" alongside the rug pattern.
    if signatures.get("dominant_accel") and signatures.get("dominant_flow"):
        rep = signatures.get("signature_repeatability", 0.0)
        if rep >= 60:
            evidence_msg = (
                f"behavior: {signatures['dominant_accel']} accel / "
                f"{signatures['dominant_flow']} flow "
                f"(repeatability {rep:.0f}%)"
            )
            result.setdefault("evidence", []).append(evidence_msg)
        # Boost confidence on already-classified patterns when signatures
        # are repeatable — directly maps to Bing's +15 acceleration bonus
        # (scaled down to 0-15 by repeatability %).
        if result.get("pattern") in GOOD_PATTERNS and rep >= 60:
            sig_bonus = (rep - 60) / 40 * 15  # 60%=0, 100%=15
            result["confidence"] = min(100.0, (result.get("confidence", 0) + sig_bonus))
            result["signature_bonus"] = round(sig_bonus, 1)
    return result
