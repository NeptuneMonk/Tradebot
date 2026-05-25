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
    return out


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
