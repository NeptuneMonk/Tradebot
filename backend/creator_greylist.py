"""
creator_greylist — score creators by their *predictable* rug patterns.

NOT a blacklist. The greylist captures creators whose tokens consistently
rug at a predictable %, making them high-value MICRO-SNIPE targets. A
creator with 5 rugs that all rug at ~22% of peak is MORE valuable than
a creator with 1 graduation but no behavioral history.

Scoring breakdown (composite 0-100):
  - Profitability  (40%) — mean pnl_pct of our past trades on their mints
  - Predictability (30%) — 100 - (stddev of rug_pct_from_peak); high stddev = unpredictable
  - Activity       (20%) — launches/week × recency boost
  - Volume         (10%) — tokens_created irrespective of failures (user's
                            "rugs OK if creator produces tradeable volume" rule)

Decay-on-read: stored score × 0.99^hours_since_update. We don't run a
background decay sweep — instead every read applies the decay formula
based on `greylist_score_updated_at`. Cheap and always-current.

`expected_rug_window_pct` = (median - 1σ, median + 1σ) of rug_pct_from_peak.
Used by the classifier (Phase 2) to set TP just BELOW the expected rug.

PHASE 1 = TELEMETRY ONLY. This module computes + persists scores. The bot
logs what it WOULD do differently for greylisted creators but executes
normal logic. After 24-48h you can see whether predictions correlate
with profitable snipes before flipping `creator_greylist_mode=live`.
"""
from __future__ import annotations
import logging
import statistics
from datetime import datetime, timezone

logger = logging.getLogger("creator_greylist")

# Composite weights (must sum to 1.0)
W_PROFITABILITY = 0.28
W_PREDICTABILITY = 0.20
W_PEAK_MC = 0.25           # avg peak MC of past FAILED mints
W_ACTIVITY = 0.13
W_VOLUME = 0.09
W_LINKS = 0.05             # NEW: linked-wallet evidence (Bing §2)

# Decay: ~1%/hr → score halves in ~70h. Reset on new launch zeroes elapsed.
DECAY_PER_HOUR = 0.99


def _safe_iso_to_ts(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


# Module-level cache for the blacklisted-creators set. The Bing §2 link
# overlap check needs this for every score update; rebuilding it from
# scratch each time would re-scan the creators collection ~1000×/sweep.
_BLACKLIST_CACHE: dict = {"set": set(), "fetched_at": 0.0}


async def _get_blacklisted_creators(db, ttl_s: int = 300) -> set:
    """Return the set of creator IDs flagged `greylist_blacklisted=True`.
    Cached for `ttl_s` seconds in-process. Cheap projection."""
    import time as _time
    now = _time.time()
    if (_BLACKLIST_CACHE["set"]
            and now - _BLACKLIST_CACHE["fetched_at"] < ttl_s):
        return _BLACKLIST_CACHE["set"]
    cur = db.creators.find(
        {"greylist_blacklisted": True}, {"_id": 1}
    ).limit(20000)
    s: set = set()
    async for d in cur:
        s.add(d["_id"])
    _BLACKLIST_CACHE["set"] = s
    _BLACKLIST_CACHE["fetched_at"] = now
    return s


def apply_decay(stored_score: float, updated_at_iso: str | None) -> float:
    """Apply exponential decay based on time since last score update."""
    if not stored_score:
        return 0.0
    ts = _safe_iso_to_ts(updated_at_iso)
    if not ts:
        return float(stored_score)
    hours = max(0.0, (datetime.now(timezone.utc).timestamp() - ts) / 3600.0)
    return float(stored_score) * (DECAY_PER_HOUR ** hours)


def _profitability_component(trades: list[dict]) -> float:
    """Mean pnl_pct of CLOSED trades on this creator's mints, mapped to 0-100.
    -100% → 0pts; 0% → 50pts; +100% → 100pts (clamped).
    If <3 trades: returns 50 (neutral). User said any % is a win, but the
    SCORE still rewards bigger wins to bias toward consistently profitable
    creators."""
    closed = [t for t in trades if t.get("status") == "closed"]
    if len(closed) < 3:
        return 50.0
    pnls = [float(t.get("pnl_pct") or 0) for t in closed]
    avg = sum(pnls) / len(pnls)
    return max(0.0, min(100.0, 50.0 + avg / 2.0))


def _predictability_component(trades: list[dict]) -> float:
    """100 - stddev_of_rug_pct, normalized to 0-100. Low variance = highly
    predictable rug = HIGH score. Requires at least 4 trades with
    `rug_pct_from_peak` populated."""
    rugs = [
        float(t["rug_pct_from_peak"])
        for t in trades
        if t.get("rug_pct_from_peak") is not None
    ]
    if len(rugs) < 4:
        return 50.0  # not enough data
    try:
        sd = statistics.stdev(rugs)
    except statistics.StatisticsError:
        return 50.0
    # stddev=0 → 100pts; stddev≥40 → 0pts (40pp spread = effectively random)
    return max(0.0, min(100.0, 100.0 * (1.0 - sd / 40.0)))


def _activity_component(creator_doc: dict) -> float:
    """Launches/week × recency boost. Active creators score higher than
    one-shot accounts that vanished. Last-seen within 24h gets full boost."""
    created = int(creator_doc.get("tokens_created") or 0)
    if created < 1:
        return 0.0
    first_seen_ts = _safe_iso_to_ts(creator_doc.get("first_seen"))
    last_seen_ts = _safe_iso_to_ts(creator_doc.get("last_seen"))
    now = datetime.now(timezone.utc).timestamp()
    weeks_active = max(1.0, (now - first_seen_ts) / (7 * 86400)) if first_seen_ts else 1.0
    launches_per_week = created / weeks_active
    base = min(100.0, launches_per_week * 25)  # 4 launches/wk = max
    # Recency boost: <24h = 1.0, 24h-7d = 0.6, >7d = 0.3
    recency = 1.0
    if last_seen_ts:
        hours_idle = (now - last_seen_ts) / 3600.0
        if hours_idle > 168:
            recency = 0.3
        elif hours_idle > 24:
            recency = 0.6
    return base * recency


def _volume_component(creator_doc: dict) -> float:
    """Pure tokens_created count — user's 'rugs OK if creator brings volume'
    rule. Caps at 20 created = 100pts."""
    created = int(creator_doc.get("tokens_created") or 0)
    return min(100.0, created * 5.0)


def _peak_mc_component(failed_launches: list[dict]) -> tuple[float, dict]:
    """Score creators whose FAILED-but-FIZZLED mints reach a meaningful
    peak MC before dying. Per RUG_PATTERNS.md, the "Dead in 60s" cohort
    (`fail_class == 'failed_instant'`) is the USELESS pattern — we
    explicitly exclude those so they can't drag the score down for a
    creator whose OTHER launches had real volume.

    Scoring:
      - mean peak MC ≥ $50k AND cv < 0.3   → ~100pts
      - mean peak MC ≥ $20k                → scaled
      - mean peak MC < $5k OR < 2 samples  → 0pts
    Returns (score, stats_dict).
    """
    peaks = [
        float(fl["final_peak_mc_usd"])
        for fl in failed_launches
        if (fl.get("outcome") == "failed"
            and fl.get("fail_class") != "failed_instant"
            and fl.get("final_peak_mc_usd") is not None
            and float(fl["final_peak_mc_usd"]) > 0)
    ]
    if len(peaks) < 2:
        return 0.0, {"n_failed_with_peak": len(peaks)}
    mean_peak = sum(peaks) / len(peaks)
    try:
        sd = statistics.stdev(peaks) if len(peaks) >= 2 else 0.0
    except statistics.StatisticsError:
        sd = 0.0
    med = statistics.median(peaks)
    cv = sd / mean_peak if mean_peak > 0 else 1.0
    if mean_peak >= 100_000:
        magnitude = 100.0
    elif mean_peak >= 50_000:
        magnitude = 80.0
    elif mean_peak >= 20_000:
        magnitude = 60.0
    elif mean_peak >= 10_000:
        magnitude = 40.0
    elif mean_peak >= 5_000:
        magnitude = 20.0
    else:
        magnitude = 0.0
    consistency = max(0.5, min(1.0, 1.0 - cv / 2.0))
    score = magnitude * consistency
    stats = {
        "n_failed_with_peak": len(peaks),
        "mean_peak_mc_usd": round(mean_peak, 0),
        "median_peak_mc_usd": round(med, 0),
        "stddev_peak_mc_usd": round(sd, 0),
        "cv": round(cv, 2),
        "lo": round(max(0, mean_peak - sd), 0),
        "hi": round(mean_peak + sd, 0),
    }
    return round(score, 1), stats


def _links_component(linked_wallets: list[dict] | None,
                     blacklisted_creators: set | None) -> tuple[float, dict]:
    """Bing §2 — linked-wallet score (0..100 scaled, then weighted W_LINKS).

    Inputs come from the `wallet_graph` hunter:
      - `linked_wallets`: list of {wallet, hop, via, ...}
      - `blacklisted_creators`: set of creator IDs already flagged as
         bad-pattern (untradeable_rug etc.). Used to detect rug-cluster
         overlap when the same wallet funded multiple known-bad creators.

    Heuristic:
      - +30 base per discovered hop-1 funder (cap 60)
      - +25 if ANY funder is also a blacklisted creator (rug-cluster hit)
      - +15 per additional rug-cluster hit (cap +50)

    Returns (score 0..100, evidence dict).
    """
    if not linked_wallets:
        return 0.0, {"n_links": 0, "rug_cluster_hits": 0,
                     "linked_to_rug_cluster": False}
    n_hop1 = sum(1 for L in linked_wallets if (L.get("hop") or 1) == 1)
    base = min(60.0, n_hop1 * 30.0)
    rug_hits = 0
    if blacklisted_creators:
        # A funding-wallet that itself appears in `blacklisted_creators` is
        # the strongest signal: this creator was funded by a known bad
        # actor (or one of their wallets), making them likely an alias.
        rug_hits = sum(
            1 for L in linked_wallets
            if L.get("wallet") in blacklisted_creators
        )
    bonus = 0.0
    if rug_hits >= 1:
        bonus = 25.0 + min(50.0, (rug_hits - 1) * 15.0)
    raw = min(100.0, base + bonus)
    return raw, {
        "n_links": len(linked_wallets),
        "n_hop1": n_hop1,
        "rug_cluster_hits": rug_hits,
        "linked_to_rug_cluster": rug_hits >= 1,
    }


def compute_score(creator_doc: dict, trades: list[dict],
                   failed_launches: list[dict] | None = None,
                   linked_wallets: list[dict] | None = None,
                   blacklisted_creators: set | None = None) -> dict:
    """Composite greylist score (0-100) + breakdown + rug-window estimate
    + expected peak MC estimate.

    `trades` should be CLOSED trade docs on this creator's mints.
    `failed_launches` should be `launches.find({creator: X, outcome: "failed"})`
    — drives the peak-MC component (user's primary asked-for signal).
    If None, the peak_mc component contributes 0.
    """
    p = _profitability_component(trades)
    pred = _predictability_component(trades)
    a = _activity_component(creator_doc)
    v = _volume_component(creator_doc)
    peak_mc_score, peak_mc_stats = _peak_mc_component(failed_launches or [])
    links_score, links_evidence = _links_component(linked_wallets,
                                                   blacklisted_creators)
    composite = (
        W_PROFITABILITY * p
        + W_PREDICTABILITY * pred
        + W_PEAK_MC * peak_mc_score
        + W_ACTIVITY * a
        + W_VOLUME * v
        + W_LINKS * links_score
    )
    # Failed-launch curve fill % distribution — what % of the bonding curve
    # typically fills before THIS creator's launches die. Drives the snipe
    # exit's `expected_rug_curve_pct` gate (see `_check_snipe_pattern_exit`).
    # We use FAILED LAUNCHES (broad data, ~thousands per creator) not trades
    # (~zero per creator pre-sniper). Curves that died below 1% fill are
    # excluded — dead-instant launches don't tell us where the creator's
    # OWN curve typically rugs.
    rug_curve_pcts = [
        float(fl["curve_fill_pct"])
        for fl in (failed_launches or [])
        if fl.get("curve_fill_pct") is not None
        and float(fl["curve_fill_pct"] or 0) >= 1.0
    ]
    if len(rug_curve_pcts) >= 3:
        med = statistics.median(rug_curve_pcts)
        try:
            sd = statistics.stdev(rug_curve_pcts)
        except statistics.StatisticsError:
            sd = 0.0
        rug_window = {
            "median_rug_pct": round(med, 1),
            "stddev_rug_pct": round(sd, 1),
            "lo": round(max(0.0, med - sd), 1),
            "hi": round(med + sd, 1),
            "samples": len(rug_curve_pcts),
        }
    else:
        rug_window = {"samples": len(rug_curve_pcts)}
    return {
        "score": round(composite, 1),
        "components": {
            "profitability": round(p, 1),
            "predictability": round(pred, 1),
            "peak_mc": peak_mc_score,
            "activity": round(a, 1),
            "volume": round(v, 1),
            "links": round(links_score, 1),
        },
        "links_evidence": links_evidence,
        "expected_rug_window_pct": rug_window,
        "expected_peak_mc_usd": peak_mc_stats,
        "n_trades": len(trades),
        "n_failed_launches": len([fl for fl in (failed_launches or [])
                                  if fl.get("outcome") == "failed"]),
    }


def recommended_strategy(effective_score: float) -> str:
    """0-100 score → 3-way classifier. PHASE 1 = LOGGING ONLY."""
    if effective_score >= 70:
        return "aggressive"
    if effective_score >= 45:
        return "hybrid"
    return "standard"


# Per-strategy entry/exit override profile. Applied at trade-entry time when
# `creator_greylist_mode == "live"` AND the creator scores ≥ "hybrid".
#
#   size_mult   : multiplier on the risk-bucketed position size (capped by
#                 BotConfig.max_trade_usd so a hot greylist can't blow risk).
#   tp_pct      : take-profit % from entry. Aggressive creators dump faster
#                 from peak so we lock smaller wins more often.
#   sl_pct      : stop-loss % from entry. Tighter on aggressive because
#                 patterned creators rug predictably and we'd rather eat
#                 a small SL than ride to -25%.
#   trail_pct   : trailing-stop %. Tighter trail = exit faster on the way
#                 down (typical for high-predictability creators).
#   trail_arm   : % gain required before trailing arms. Lower = lock gains
#                 sooner on predictable creators.
#
# These were tuned against the user's RUG_PATTERNS.md heuristic: creators
# with consistent peak MC + low rug-window variance show "tradeable volume
# even though they rug" — the goal is to ride the predictable pump and exit
# just BEFORE the historical rug threshold.
_STRATEGY_OVERRIDES = {
    "aggressive": {
        "size_mult": 1.5,
        "tp_pct": 35.0,
        "sl_pct": 12.0,
        "trail_pct": 6.0,
        "trail_arm_pct": 12.0,
    },
    "hybrid": {
        "size_mult": 1.2,
        "tp_pct": 25.0,
        "sl_pct": 15.0,
        "trail_pct": 7.0,
        "trail_arm_pct": 14.0,
    },
    "standard": {},  # no overrides
}


def strategy_overrides(strategy: str) -> dict:
    """Return the entry/exit override dict for a strategy tier. Empty dict
    for 'standard' (means: use BotConfig defaults). Phase 2 — wires into
    bot.py only when `creator_greylist_mode == 'live'`."""
    return dict(_STRATEGY_OVERRIDES.get(strategy) or {})


def stage1_filter(creator_doc: dict | None,
                  failed_launches: list[dict],
                  linked_wallets: list[dict] | None = None,
                  links_evidence: dict | None = None) -> tuple[bool, str]:
    """Bing §1 — cheap, fast, broad. Returns (pass, reason).

    A creator passes Stage 1 (i.e. deserves the expensive classifier run)
    if ANY of these conditions hold. All inputs are already-fetched from
    Mongo — no Helius calls here.

      A. tokens_failed ≥ 2                                  (≥2 launches)
      B. linked_to_rug_cluster                             (Bing C)
      C. ANY launch rug_seconds_from_launch < 20            (Bing D)
      D. ANY launch accel_signature_v2 in {parabolic, bot_swarm}  (Bing E)
      E. F-band membership (5 ≤ tokens_failed < 80)         (custom: our
         existing primary admission gate — kept here so calling code that
         doesn't otherwise filter by F-band still gets sane behavior)

    A creator who fails ALL conditions returns (False, reason) — they're
    either too quiet, too new, or showing no behavioral edges yet. The
    caller can skip the full classifier run and persist a placeholder.
    """
    creator_doc = creator_doc or {}
    failed_launches = failed_launches or []
    tokens_failed = int(creator_doc.get("tokens_failed") or 0)
    if tokens_failed >= 2:
        return True, f"≥2 fails ({tokens_failed})"
    if links_evidence and links_evidence.get("linked_to_rug_cluster"):
        return True, "linked to rug cluster"
    if linked_wallets and len([L for L in linked_wallets
                               if (L.get("hop") or 1) == 1]) >= 2:
        return True, ">=2 hop-1 funders"
    for L in failed_launches:
        rs = L.get("rug_seconds_from_launch")
        if rs is not None and rs < 20:
            return True, "instant-rug history"
    for L in failed_launches:
        accv2 = L.get("accel_signature_v2")
        if accv2 in ("parabolic", "bot_swarm"):
            return True, f"{accv2} signature"
    if 5 <= tokens_failed < 80:
        return True, "in F-band"
    return False, f"no stage1 trigger (F={tokens_failed}, links={len(linked_wallets or [])})"


async def update_creator_score(db, creator: str,
                                min_fails: int = 5, max_fails: int = 80,
                                tp_buffer: float = 2.0) -> dict | None:
    """Recompute + persist greylist score for one creator. Called on every
    trade close AND every launch outcome stamp. Cheap — Mongo-only.

    `min_fails` / `max_fails` are the F-BAND gate (defaults match the
    user's preferred 5-80 window). Outside the band the COMPONENT stats are
    still computed + persisted (so the moment a creator crosses into the
    band their score "wakes up"), but the composite is forced to 0 and an
    `out_of_band` flag is set so the UI can explain the suppression.

    `tp_buffer` is the % subtracted from the median observed rug to derive
    `pattern_suggested_exit_pct[0]` (the TP override Phase 2.7 uses).

    Hot-path optimization: we fetch the cheap inputs (creator_doc, failed
    launches, wallet_graph) FIRST and run `stage1_filter()`. If the creator
    fails Stage-1 we persist a minimal `stage1_rejected=True` placeholder
    and SKIP the expensive `trades` (≤500 docs) + `all_launches` (≤300
    docs) fetches + classifier run. Cuts Mongo load roughly 80% for fresh
    or quiet creators where the composite would have been 0 anyway."""
    if not creator:
        return None
    creator_doc = await db.creators.find_one({"_id": creator})
    if not creator_doc:
        return None
    # === CHEAP INPUTS (needed for Stage-1) =================================
    # Failed launches with their peak MC — drives the "average peak MC of
    # past failures" component (user's primary asked-for signal). The
    # peak_mc scorer explicitly skips fail_class='failed_instant' so the
    # "Dead in 60s" cohort can't drag down a creator whose other launches
    # had real volume. Also: stage1 checks rug_seconds + accel_signature_v2
    # on this set so the projection includes those fields.
    failed = await db.launches.find(
        {"creator": creator, "outcome": "failed"},
        {"_id": 0, "mint": 1, "symbol": 1, "outcome": 1, "fail_class": 1,
         "outcome_at": 1, "final_peak_mc_usd": 1, "buy_count": 1,
         "unique_buyers": 1, "sol_inflow": 1, "curve_fill_pct": 1,
         "rug_seconds_from_launch": 1, "accel_signature_v2": 1},
    ).to_list(200)
    failed_meaningful = [
        f for f in failed
        if (float(f.get("sol_inflow") or 0) >= 0.1
            or int(f.get("buy_count") or 0) >= 3)
    ]
    # Linked-wallets (Bing §2) from the background hunter. Cheap Mongo
    # lookup; the hunter does the Helius calls separately and persists.
    wg_doc = await db.wallet_graph.find_one({"_id": creator}, {"_id": 0, "linked_wallets": 1})
    linked_wallets = (wg_doc or {}).get("linked_wallets") or []
    # Blacklisted-creators set — rebuilt lazily ONCE per process tick via
    # module-level cache (refreshed every 5min). The rug-cluster overlap
    # check needs this set; without it linked_wallets contributes only the
    # base "you have N linked funders" bonus.
    blacklisted = await _get_blacklisted_creators(db)
    # Pre-compute links evidence so stage1 can see linked_to_rug_cluster.
    _, links_evidence_preview = _links_component(linked_wallets, blacklisted)
    # === STAGE-1 GATE =====================================================
    s1_pass, s1_reason = stage1_filter(
        creator_doc, failed_meaningful,
        linked_wallets=linked_wallets,
        links_evidence=links_evidence_preview,
    )
    tokens_failed = int(creator_doc.get("tokens_failed") or 0)
    out_of_band = tokens_failed < min_fails or tokens_failed >= max_fails
    now_iso = datetime.now(timezone.utc).isoformat()
    if not s1_pass:
        # SHORT-CIRCUIT: skip the expensive trades + all_launches fetches
        # + the classifier run. Composite would have been 0 anyway (no
        # signal to score). Persist minimal placeholder so the next call
        # re-evaluates from scratch as soon as new data arrives.
        await db.creators.update_one(
            {"_id": creator},
            {"$set": {
                "greylist_score": 0.0,
                "greylist_score_raw": 0.0,
                "greylist_tokens_failed": tokens_failed,
                "greylist_out_of_band": out_of_band,
                "greylist_blacklisted": False,
                "greylist_pattern": "unknown",
                "greylist_pattern_confidence": 0,
                "greylist_pattern_evidence": [f"stage1: {s1_reason}"],
                "greylist_stage1_rejected": True,
                "greylist_stage1_reason": s1_reason,
                "greylist_links_evidence": links_evidence_preview,
                "greylist_band_min": min_fails,
                "greylist_band_max": max_fails,
                "greylist_score_updated_at": now_iso,
            }},
        )
        return {"score": 0.0, "raw_score": 0.0,
                "stage1_rejected": True, "stage1_reason": s1_reason,
                "out_of_band": out_of_band, "tokens_failed": tokens_failed,
                "pattern": "unknown", "pattern_confidence": 0,
                "pattern_blacklisted": False}
    # === EXPENSIVE PIPELINE (Stage-1 passed) ==============================
    trades = await db.trades.find(
        {"creator": creator, "status": "closed"}, {"_id": 0},
    ).to_list(500)
    score = compute_score(creator_doc, trades, failed_meaningful,
                          linked_wallets=linked_wallets,
                          blacklisted_creators=blacklisted)
    # Mechanical pattern classifier (see creator_pattern.py + RUG_PATTERNS.md).
    # Bad patterns (untradeable_rug / unpredictable_rug / unknown) flip
    # `blacklisted=True` → composite forced to 0 → creator hidden from UI.
    # Good patterns (slow_rug / predictable_dump / fake_hype) stay scored
    # and get a pattern badge in the UI.
    # Pull ALL launches (graduated + failed + dormant) for behavioral
    # signature aggregation — the Bing-style accel/flow signatures aren't
    # outcome-dependent; they describe how a creator's launches BEHAVE in
    # their first minutes regardless of how they ended.
    all_launches = await db.launches.find(
        {"creator": creator},
        {"_id": 0, "sol_inflow": 1, "buy_count": 1, "unique_buyers": 1,
         "outcome": 1, "outcome_at": 1, "detected_at": 1,
         "accel_class": 1, "flow_class": 1, "rug_speed_class": 1,
         "rug_seconds_from_launch": 1, "accel_signature_v2": 1,
         "profit_window_seconds": 1, "peak_mc_usd_at": 1},
    ).to_list(300)
    from creator_pattern import classify_with_signatures
    pattern = classify_with_signatures(
        creator_doc, failed_meaningful, trades,
        tp_buffer=tp_buffer, all_launches=all_launches,
    )
    # Trust the classifier's per-result `blacklisted` flag — it knows whether
    # an "unknown" pattern is a hard reject vs a watchable candidate. Don't
    # re-blacklist based on the pattern name alone.
    pattern_blacklisted = bool(pattern.get("blacklisted"))
    # F-band gate. We use LIFETIME `tokens_failed` (the "F" badge the user
    # sees in Recent Launches) — not `n_failed_launches` which only counts
    # sweep-classified ones with peak MC. The lifetime counter is the right
    # signal for "does this creator have a pattern worth watching yet".
    suppressed = out_of_band or pattern_blacklisted
    composite = 0.0 if suppressed else score["score"]
    # Top 10 recent meaningful failed mints (for the profile/UI card —
    # spam launches like 0.00001-SOL tests are dropped).
    recent_failed = sorted(
        failed_meaningful,
        key=lambda fl: fl.get("outcome_at") or "",
        reverse=True,
    )[:10]
    await db.creators.update_one(
        {"_id": creator},
        {"$set": {
            "greylist_score": composite,
            "greylist_score_raw": score["score"],  # pre-band score (for diagnostics)
            "greylist_components": score["components"],
            "expected_rug_window_pct": score["expected_rug_window_pct"],
            "expected_peak_mc_usd": score["expected_peak_mc_usd"],
            "greylist_n_trades": score["n_trades"],
            "greylist_n_failed": score["n_failed_launches"],
            "greylist_tokens_failed": tokens_failed,
            "greylist_out_of_band": out_of_band,
            "greylist_blacklisted": pattern_blacklisted,
            "greylist_pattern": pattern["pattern"],
            "greylist_pattern_confidence": pattern.get("confidence", 0),
            "greylist_pattern_evidence": pattern.get("evidence", []),
            "greylist_pattern_suggested_entry": pattern.get("suggested_entry_pct"),
            "greylist_pattern_suggested_exit": pattern.get("suggested_exit_pct"),
            "greylist_signatures": pattern.get("signatures") or {},
            "greylist_signature_bonus": pattern.get("signature_bonus", 0),
            "greylist_links_evidence": score.get("links_evidence") or {},
            "greylist_stage1_rejected": False,
            "greylist_stage1_reason": s1_reason,
            "greylist_band_min": min_fails,
            "greylist_band_max": max_fails,
            "greylist_recent_failed_mints": recent_failed,
            "greylist_score_updated_at": now_iso,
        }},
    )
    # Return both the original score AND the band-gated composite so callers
    # can log the suppression decision.
    return {**score, "score": composite, "raw_score": score["score"],
            "stage1_rejected": False, "stage1_reason": s1_reason,
            "out_of_band": out_of_band, "tokens_failed": tokens_failed,
            "pattern": pattern["pattern"],
            "pattern_confidence": pattern.get("confidence", 0),
            "pattern_blacklisted": pattern_blacklisted}


async def top_greylisted(db, limit: int = 20, min_score: float = 30.0) -> list[dict]:
    """Return the top N creators by EFFECTIVE (decayed) greylist score.
    Creators with `greylist_out_of_band=True` OR `greylist_blacklisted=True`
    are explicitly excluded — their composite was forced to 0 anyway, but
    this skips them at the index level so the query is cheap even on a
    large `creators` collection."""
    cur = db.creators.find(
        {"greylist_score": {"$gte": min_score},
         "$or": [
             {"greylist_out_of_band": {"$exists": False}},
             {"greylist_out_of_band": False},
         ],
         "$and": [{
             "$or": [
                 {"greylist_blacklisted": {"$exists": False}},
                 {"greylist_blacklisted": False},
             ]
         }]},
        {"_id": 1, "greylist_score": 1, "greylist_components": 1,
         "greylist_n_trades": 1, "greylist_n_failed": 1,
         "greylist_tokens_failed": 1, "greylist_out_of_band": 1,
         "greylist_blacklisted": 1, "greylist_pattern": 1,
         "greylist_pattern_confidence": 1, "greylist_pattern_evidence": 1,
         "greylist_pattern_suggested_entry": 1,
         "greylist_pattern_suggested_exit": 1,
         "greylist_signatures": 1, "greylist_signature_bonus": 1,
         "greylist_links_evidence": 1,
         "expected_rug_window_pct": 1, "expected_peak_mc_usd": 1,
         "greylist_score_updated_at": 1, "tokens_created": 1,
         "tokens_graduated": 1, "tokens_failed": 1, "last_seen": 1},
    ).sort("greylist_score", -1).limit(min(200, max(1, limit) * 3))
    rows = []
    async for d in cur:
        eff = apply_decay(d.get("greylist_score") or 0,
                          d.get("greylist_score_updated_at"))
        if eff < min_score:
            continue
        rows.append({
            "creator": d.get("_id"),
            "effective_score": round(eff, 1),
            "stored_score": d.get("greylist_score"),
            "components": d.get("greylist_components") or {},
            "expected_rug_window_pct": d.get("expected_rug_window_pct") or {},
            "expected_peak_mc_usd": d.get("expected_peak_mc_usd") or {},
            "n_trades": d.get("greylist_n_trades") or 0,
            "n_failed": d.get("greylist_n_failed") or 0,
            "tokens_created": d.get("tokens_created") or 0,
            "tokens_graduated": d.get("tokens_graduated") or 0,
            "tokens_failed": d.get("tokens_failed") or 0,
            "pattern": d.get("greylist_pattern") or "unknown",
            "pattern_confidence": d.get("greylist_pattern_confidence") or 0,
            "pattern_evidence": d.get("greylist_pattern_evidence") or [],
            "pattern_suggested_entry": d.get("greylist_pattern_suggested_entry"),
            "pattern_suggested_exit": d.get("greylist_pattern_suggested_exit"),
            "signatures": d.get("greylist_signatures") or {},
            "signature_bonus": d.get("greylist_signature_bonus", 0),
            "links_evidence": d.get("greylist_links_evidence") or {},
            "recommended_strategy": recommended_strategy(eff),
            "last_seen": d.get("last_seen"),
        })
    rows.sort(key=lambda r: r["effective_score"], reverse=True)
    return rows[:limit]


async def top_blacklisted(db, limit: int = 25) -> list[dict]:
    """Top blacklisted creators (untradeable / unpredictable / unknown).
    Surfaced as a SEPARATE UI panel so the user can see WHO got eliminated
    and WHY without polluting the greylist. Sorted by tokens_failed desc
    so the "loudest" offenders come first."""
    cur = db.creators.find(
        {"greylist_blacklisted": True},
        {"_id": 1, "greylist_pattern": 1, "greylist_pattern_confidence": 1,
         "greylist_pattern_evidence": 1, "tokens_created": 1,
         "tokens_failed": 1, "tokens_graduated": 1, "last_seen": 1,
         "expected_peak_mc_usd": 1, "greylist_n_failed": 1,
         "greylist_score_updated_at": 1},
    ).sort("tokens_failed", -1).limit(min(200, max(1, limit)))
    out: list[dict] = []
    async for d in cur:
        out.append({
            "creator": d.get("_id"),
            "pattern": d.get("greylist_pattern") or "unknown",
            "confidence": d.get("greylist_pattern_confidence") or 0,
            "evidence": d.get("greylist_pattern_evidence") or [],
            "tokens_created": d.get("tokens_created") or 0,
            "tokens_failed": d.get("tokens_failed") or 0,
            "tokens_graduated": d.get("tokens_graduated") or 0,
            "n_failed_with_peak": (d.get("expected_peak_mc_usd") or {}).get("n_failed_with_peak") or 0,
            "last_seen": d.get("last_seen"),
            "updated_at": d.get("greylist_score_updated_at"),
        })
    return out


async def pattern_analytics(db, days: int = 30, mode: str | None = None) -> dict:
    """Phase 2.6 — group CLOSED trades by `greylist_pattern_at_entry` and
    compute per-pattern PnL stats. Used to validate whether the pattern
    classifier's predictions actually correlate with profitable exits.

    `days` — lookback window over `exit_time`.
    `mode` — when set ('live' or 'paper'), restricts to that trading mode.
             Defaults to None = both. Live counts are the real signal; paper
             counts are useful while you're still in telemetry phase.

    Returns: {patterns: [...], totals: {...}, days, mode}
    Each pattern row carries:
      pattern, n_trades, n_wins, win_rate_pct,
      mean_pnl_pct, median_pnl_pct, total_pnl_usd, best_pnl_pct, worst_pnl_pct
    Sorted by total_pnl_usd desc so the moneymaker bucket is on top.
    """
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat()
    q: dict = {"status": "closed", "exit_time": {"$gte": cutoff}}
    if mode in ("live", "paper"):
        q["mode"] = mode
    cur = db.trades.find(
        q,
        {"_id": 0, "greylist_pattern_at_entry": 1, "greylist_strategy_at_entry": 1,
         "greylist_pattern_suggested_tp_pct": 1,
         "pnl_pct": 1, "pnl_usd": 1, "mode": 1, "exit_reason": 1},
    )
    buckets: dict[str, list[dict]] = {}
    async for t in cur:
        key = t.get("greylist_pattern_at_entry") or "unclassified"
        buckets.setdefault(key, []).append(t)

    rows: list[dict] = []
    for pattern, trades in buckets.items():
        pnl_pcts = [float(t.get("pnl_pct") or 0) for t in trades]
        pnl_usds = [float(t.get("pnl_usd") or 0) for t in trades]
        n = len(trades)
        wins = sum(1 for p in pnl_pcts if p > 0)
        # SL exits are the canonical "lost the rug race" signal — surfacing
        # them per-pattern lets the user see e.g. "fake_hype trips SL 40% of
        # the time, slow_rug only 8%". Keep cheap — string match on reason.
        sl_exits = sum(
            1 for t in trades
            if t.get("exit_reason") and "stop-loss" in str(t["exit_reason"]).lower()
        )
        rows.append({
            "pattern": pattern,
            "n_trades": n,
            "n_wins": wins,
            "n_sl_exits": sl_exits,
            "win_rate_pct": round(wins / n * 100, 1) if n else 0.0,
            "sl_rate_pct": round(sl_exits / n * 100, 1) if n else 0.0,
            "mean_pnl_pct": round(sum(pnl_pcts) / n, 2) if n else 0.0,
            "median_pnl_pct": round(statistics.median(pnl_pcts), 2) if n else 0.0,
            "total_pnl_usd": round(sum(pnl_usds), 2),
            "best_pnl_pct": round(max(pnl_pcts), 2) if pnl_pcts else 0.0,
            "worst_pnl_pct": round(min(pnl_pcts), 2) if pnl_pcts else 0.0,
        })
    rows.sort(key=lambda r: r["total_pnl_usd"], reverse=True)
    totals = {
        "n_trades": sum(r["n_trades"] for r in rows),
        "n_wins": sum(r["n_wins"] for r in rows),
        "total_pnl_usd": round(sum(r["total_pnl_usd"] for r in rows), 2),
    }
    return {"patterns": rows, "totals": totals, "days": days, "mode": mode or "all"}




async def get_creator_profile(db, creator: str) -> dict | None:
    """Full profile for one creator: score, components, rug window, peak MC,
    recent failed mints with their peak MC, recent trades, linked wallets."""
    d = await db.creators.find_one({"_id": creator})
    if not d:
        return None
    eff = apply_decay(d.get("greylist_score") or 0,
                      d.get("greylist_score_updated_at"))
    recent = await db.trades.find(
        {"creator": creator}, {"_id": 0, "mint": 1, "symbol": 1,
         "status": 1, "pnl_pct": 1, "exit_time": 1, "exit_reason": 1,
         "rug_pct_from_peak": 1, "peak_pct_pre_rug": 1},
    ).sort("entry_time", -1).limit(25).to_list(25)
    linked_doc = await db.wallet_graph.find_one({"_id": creator}, {"_id": 0})
    return {
        "creator": d.get("_id"),
        "effective_score": round(eff, 1),
        "stored_score": d.get("greylist_score"),
        "components": d.get("greylist_components") or {},
        "expected_rug_window_pct": d.get("expected_rug_window_pct") or {},
        "expected_peak_mc_usd": d.get("expected_peak_mc_usd") or {},
        "n_trades": d.get("greylist_n_trades") or 0,
        "n_failed": d.get("greylist_n_failed") or 0,
        "tokens_created": d.get("tokens_created") or 0,
        "tokens_graduated": d.get("tokens_graduated") or 0,
        "tokens_failed": d.get("tokens_failed") or 0,
        "first_seen": d.get("first_seen"),
        "last_seen": d.get("last_seen"),
        "recent_failed_mints": d.get("greylist_recent_failed_mints") or [],
        "recent_trades": recent,
        "linked_wallets": (linked_doc or {}).get("linked_wallets") or [],
        "recommended_strategy": recommended_strategy(eff),
        "pattern": d.get("greylist_pattern") or "unknown",
        "pattern_confidence": d.get("greylist_pattern_confidence") or 0,
        "pattern_evidence": d.get("greylist_pattern_evidence") or [],
        "pattern_suggested_entry": d.get("greylist_pattern_suggested_entry"),
        "pattern_suggested_exit": d.get("greylist_pattern_suggested_exit"),
        "signatures": d.get("greylist_signatures") or {},
        "signature_bonus": d.get("greylist_signature_bonus", 0),
        "blacklisted": bool(d.get("greylist_blacklisted")),
        "out_of_band": bool(d.get("greylist_out_of_band")),
    }
