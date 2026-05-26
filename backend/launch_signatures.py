"""
Launch signature derivation per Bing greylist reference.

For each launch, derive a compact set of categorical signatures that capture
the launch's BEHAVIORAL profile — independent of outcome. These signatures
are then aggregated per creator and fed into the pattern classifier so a
creator with consistent signatures across N launches gets the
"acceleration pattern repeatability +15" Bing-formula bonus.

Three signatures per launch:

  accel_class: how fast did money + buyers arrive?
    - "fast"    : ≥10 SOL inflow OR ≥30 buys in the observed window
    - "moderate": 2-10 SOL OR 10-30 buys
    - "slow"    : 0.1-2 SOL OR 3-10 buys
    - "dead"    : less than that

  flow_class: distinguishes whale-led vs swarm-led vs broad participation
    (signals bot clusters per Bing's bot_cluster_score)
    - "whale_led" : sol_inflow/buy_count ≥ 0.2 SOL/buy (concentrated)
    - "broad"     : 0.02-0.2 SOL/buy (typical organic)
    - "swarm"     : < 0.02 SOL/buy AND ≥10 buys (bot-swarm signature)
    - "unknown"   : not enough data

  rug_speed_class: how fast did the rug happen?
    - "instant" : rugged in <60s
    - "fast"    : 60s-10min
    - "delayed" : 10min-6h
    - "fizzle"  : 6h+ (slow death)
    - None      : didn't rug / no outcome_at
"""
from __future__ import annotations
from datetime import datetime
from typing import Any


def _parse_iso(s: Any) -> datetime | None:
    if not s:
        return None
    if isinstance(s, datetime):
        return s
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def accel_class(launch: dict) -> str:
    """Acceleration class based on observed inflow + buyer count.
    Independent of timestamps so works on historical launches that
    only stored their final state."""
    sol_inflow = float(launch.get("sol_inflow") or 0)
    buys = int(launch.get("buy_count") or 0)
    if sol_inflow >= 10.0 or buys >= 30:
        return "fast"
    if sol_inflow >= 2.0 or buys >= 10:
        return "moderate"
    if sol_inflow >= 0.1 or buys >= 3:
        return "slow"
    return "dead"


def flow_class(launch: dict) -> str:
    """Flow class — distinguishes whale-led / broad / bot-swarm based on
    avg SOL per buy. Bot swarms show MANY tiny buys; whales show few big
    ones. Used as a coarse Bing-style bot_cluster_score proxy."""
    sol_inflow = float(launch.get("sol_inflow") or 0)
    buys = int(launch.get("buy_count") or 0)
    if buys < 3 or sol_inflow < 0.05:
        return "unknown"
    avg_per_buy = sol_inflow / buys
    if avg_per_buy >= 0.2:
        return "whale_led"
    if avg_per_buy < 0.02 and buys >= 10:
        return "swarm"
    return "broad"


def rug_speed_class(launch: dict) -> str | None:
    """Rug speed class — only set when both `detected_at` and `outcome_at`
    are populated. Independent signal from accel/flow."""
    if launch.get("outcome") != "failed":
        return None
    started = _parse_iso(launch.get("detected_at"))
    rugged = _parse_iso(launch.get("outcome_at"))
    if not started or not rugged:
        return None
    seconds = (rugged - started).total_seconds()
    if seconds < 60:
        return "instant"
    if seconds < 600:
        return "fast"
    if seconds < 21600:  # 6h
        return "delayed"
    return "fizzle"


def derive_curve_fill_pct(launch: dict) -> float | None:
    """Reconstruct `curve_fill_pct` for historical launches that were
    persisted before the live tracker started capturing it (~92% of
    pre-2026-05 failed launches sit at curve_fill_pct=0).

    Two derivation paths, in priority order:

    1. **From `final_peak_mc_usd`** (preferred). Pump.fun graduation MC is
       ~$69k → curve_fill_pct = 100. The relationship is linear in
       real_sol_reserves so:
            `curve_fill_pct ≈ peak_mc_usd / 69_000 * 100`
       This is the cleanest derivation because peak_mc represents WHERE
       THE CURVE GOT TO, which is exactly what `expected_rug_curve_pct`
       needs.

    2. **From `sol_inflow`** (fallback). When peak_mc isn't set:
            `curve_fill_pct ≈ sol_inflow / 85 * 100`
       Caveat: `sol_inflow` is the SUM of buy amounts, not real_sol
       reserves (which is buys − sells). For launches that pumped then
       dumped, sol_inflow > peak_real_sol. So this is an UPPER BOUND.
       For dead-instant launches it's essentially equal.

    Returns the derived value clamped to [0, 100], or None if neither
    `final_peak_mc_usd` nor `sol_inflow` are present.
    """
    peak_mc = launch.get("final_peak_mc_usd")
    if peak_mc is not None and peak_mc > 0:
        return min(100.0, max(0.0, float(peak_mc) / 69_000.0 * 100.0))
    inflow = launch.get("sol_inflow")
    if inflow is not None and inflow > 0:
        return min(100.0, max(0.0, float(inflow) / 85.0 * 100.0))
    return None


def derive_signatures(launch: dict) -> dict:
    """Combine all 3 signatures for batch persistence. Returns the dict
    that should be `$set` on the launch doc."""
    out: dict = {
        "accel_class": accel_class(launch),
        "flow_class": flow_class(launch),
    }
    rug_speed = rug_speed_class(launch)
    if rug_speed:
        out["rug_speed_class"] = rug_speed
        # Also compute rug_seconds for finer-grained per-creator variance
        started = _parse_iso(launch.get("detected_at"))
        rugged = _parse_iso(launch.get("outcome_at"))
        if started and rugged:
            out["rug_seconds_from_launch"] = (rugged - started).total_seconds()
    # Profit window — peak → rug delta. Tells the strategy how long you
    # have to ride a pump before the typical dump for this creator.
    # Requires `peak_mc_usd_at` to have been stamped during the launch's
    # tracked lifetime (see `_persist_metrics` in bot.py).
    pw = profit_window_seconds(launch)
    if pw is not None:
        out["profit_window_seconds"] = pw
    return out


def profit_window_seconds(launch: dict) -> float | None:
    """Time between peak MC and rug. Captures the trading window per
    creator. Returns None unless BOTH `peak_mc_usd_at` and `outcome_at` are
    present (which requires the launch to have lived through the tracked
    lifetime AND failed/graduated)."""
    peak = _parse_iso(launch.get("peak_mc_usd_at"))
    rugged = _parse_iso(launch.get("outcome_at"))
    if not peak or not rugged:
        return None
    delta = (rugged - peak).total_seconds()
    return delta if delta >= 0 else None


# === Delta-based acceleration signature (Bing reference §3.C) ============
# Distinguishes parabolic / bot_swarm / whale_led using per-event SOL
# deltas rather than total inflow/buy counts. Captures HOW money arrived,
# not just how much. Persisted as `accel_signature_v2` on launches that
# had live buy_events tracked (failure_sweep can't compute this for
# historical launches because raw event series wasn't persisted).

def accel_signature_v2(buy_events: list) -> str | None:
    """`buy_events` is a list of `(timestamp, sol_lamports, user)` tuples
    captured by the listener during the launch's tracked lifetime.
    Returns one of: 'parabolic' | 'bot_swarm' | 'whale_led' | 'moderate'
    | 'dead' | None (insufficient data).

      parabolic   : monotonic +acceleration in cumulative inflow
                    (>70% of consecutive deltas are positive AND each delta
                    grows). Classic pump-and-dump shape.
      bot_swarm   : many tiny buys with low timestamp variance
                    (>70% of buys are < 0.005 SOL each AND >20 buys)
      whale_led   : single largest buy ≥ 40% of total inflow
      moderate    : mixed/organic shape that doesn't trip the others
      dead        : <3 events total
    """
    if not buy_events or len(buy_events) < 3:
        return "dead"
    sols = [float(e[1]) / 1_000_000_000.0 for e in buy_events]  # lamports → SOL
    total = sum(sols)
    if total < 0.05:
        return "dead"
    n = len(sols)

    # whale-led: single biggest buy dominates
    if max(sols) >= 0.4 * total:
        return "whale_led"

    # bot-swarm: lots of tiny buys
    tiny = sum(1 for s in sols if s < 0.005)
    if n >= 20 and tiny >= 0.7 * n:
        return "bot_swarm"

    # parabolic: cumulative-inflow growth shows acceleration. Use
    # cumulative-sum deltas (windowed) — a parabola has growing slopes.
    if n >= 6:
        # Bucket events into 5 equal slices, look at slope acceleration.
        bucket_size = max(1, n // 5)
        buckets = [sum(sols[i:i + bucket_size])
                   for i in range(0, n, bucket_size)][:5]
        # Need at least 3 buckets for acceleration check
        if len(buckets) >= 3:
            deltas = [buckets[i] - buckets[i - 1] for i in range(1, len(buckets))]
            growing = sum(1 for i in range(1, len(deltas))
                          if deltas[i] > deltas[i - 1])
            if growing >= len(deltas) - 1 and deltas[-1] > deltas[0]:
                return "parabolic"

    return "moderate"


# === Creator-level aggregation ============================================

def aggregate_signatures(launches: list[dict]) -> dict:
    """Aggregate per-launch signatures into per-creator stats.

    Returns:
      {
        accel_distribution: {fast: N, moderate: N, slow: N, dead: N},
        flow_distribution: {whale_led: N, broad: N, swarm: N, unknown: N},
        rug_speed_distribution: {instant: N, fast: N, delayed: N, fizzle: N},
        signature_repeatability: 0..100,  # % launches matching dominant signature
        dominant_accel: str,
        dominant_flow: str,
        dominant_rug_speed: str | None,
        rug_seconds_stats: {median, stddev, n} | None,
      }
    """
    if not launches:
        return {}
    n = len(launches)
    accel_dist: dict[str, int] = {}
    flow_dist: dict[str, int] = {}
    rug_speed_dist: dict[str, int] = {}
    rug_seconds: list[float] = []
    for launch in launches:
        a = launch.get("accel_class") or accel_class(launch)
        f = launch.get("flow_class") or flow_class(launch)
        accel_dist[a] = accel_dist.get(a, 0) + 1
        flow_dist[f] = flow_dist.get(f, 0) + 1
        r = launch.get("rug_speed_class") or rug_speed_class(launch)
        if r:
            rug_speed_dist[r] = rug_speed_dist.get(r, 0) + 1
        rs = launch.get("rug_seconds_from_launch")
        if rs is not None:
            try:
                rug_seconds.append(float(rs))
            except (TypeError, ValueError):
                pass

    dom_accel = max(accel_dist, key=accel_dist.get) if accel_dist else None
    dom_flow = max(flow_dist, key=flow_dist.get) if flow_dist else None
    dom_rug = max(rug_speed_dist, key=rug_speed_dist.get) if rug_speed_dist else None

    # Repeatability = average dominance % across the two non-outcome
    # signatures (accel + flow). Bing's formula gives +15 when signatures
    # are repeatable (same accel + flow on most launches). We expose this
    # as a 0-100 score the scorer can use directly.
    repeatability_components = []
    if accel_dist and dom_accel:
        repeatability_components.append(accel_dist[dom_accel] / n * 100)
    if flow_dist and dom_flow:
        repeatability_components.append(flow_dist[dom_flow] / n * 100)
    repeatability = (
        sum(repeatability_components) / len(repeatability_components)
        if repeatability_components else 0.0
    )

    # rug_seconds stats — measure of rug-timing consistency
    rug_seconds_stats: dict | None = None
    if len(rug_seconds) >= 3:
        import statistics
        rug_seconds.sort()
        median = statistics.median(rug_seconds)
        try:
            sd = statistics.stdev(rug_seconds)
        except statistics.StatisticsError:
            sd = 0.0
        rug_seconds_stats = {
            "median": round(median, 1),
            "stddev": round(sd, 1),
            "cv": round(sd / median, 3) if median else 0.0,
            "n": len(rug_seconds),
        }

    return {
        "accel_distribution": accel_dist,
        "flow_distribution": flow_dist,
        "rug_speed_distribution": rug_speed_dist,
        "signature_repeatability": round(repeatability, 1),
        "dominant_accel": dom_accel,
        "dominant_flow": dom_flow,
        "dominant_rug_speed": dom_rug,
        "rug_seconds_stats": rug_seconds_stats,
    }
