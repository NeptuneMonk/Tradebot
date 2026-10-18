"""Trending-name gate — when a ticker is being cloned (≥ N copies inside the band window) buy ONLY the copy smart wallets
are piling into. Non-leader copies are refused on every automatic entry path and shown as "trending" in the candidates.

Leader = most smart-wallet buys (manual wallets weigh 2×) → market cap → unique buyers / holders → recent inflow.
A trending name is only tradeable at all once its leader has ≥ `trend_name_leader_min_smart` smart buys."""
from __future__ import annotations

import re
import time

_NORM = re.compile(r"[^a-z0-9]+")


def norm_key(symbol: str | None, name: str | None) -> str | None:
    for raw in (symbol, name):
        k = _NORM.sub("", str(raw or "").lower().lstrip("$"))
        if len(k) >= 2:
            return k
    return None


def _stats(b: dict, now: float, hit_ttl_s: float) -> dict:
    hits = b.get("smart_hits") or {}
    manual = sum(1 for h in hits.values() if h.get("kind") == "manual")
    fresh = sum(1 for h in hits.values() if now - float(h.get("ts") or 0) <= hit_ttl_s)
    buyers = max(len(b.get("buyers") or ()), int(b.get("holder_count") or 0))
    inflow = sum(l for ts, l, _u in (b.get("buy_events") or ()) if ts >= now - 120) / 1e9
    return {"smart_total": len(hits), "smart_manual": manual, "smart_fresh": fresh, "smart_weighted": len(hits) + manual,
            "mc_usd": float(b.get("usd_market_cap") or 0.0), "buyers": buyers, "inflow_sol_2m": round(inflow, 3)}


def build_clusters(tracking: dict, cfg, now: float | None = None, active_mints: set | None = None) -> dict[str, dict]:
    """{name_key: cluster} for every name with ≥ trend_name_min_copies copies launched inside the band window."""
    if not bool(getattr(cfg, "trend_name_gate_enabled", True)):
        return {}
    now = now or time.time()
    active_mints = active_mints or set()
    window_s = 60.0 * max(float(getattr(cfg, "band_new_max_age_min", 15.0) or 15.0), float(getattr(cfg, "band_seasoned_max_age_min", 60.0) or 60.0))
    min_copies = max(2, int(getattr(cfg, "trend_name_min_copies", 3) or 3))
    hit_ttl = float(getattr(cfg, "smart_wallet_hit_ttl_s", 180) or 180)
    groups: dict[str, list[tuple[str, dict]]] = {}
    for mint, b in tracking.items():
        start = float(b.get("start") or 0)
        if start <= 0 or now - start > window_s:
            continue
        k = norm_key(b.get("symbol"), b.get("name"))
        if k:
            groups.setdefault(k, []).append((mint, b))
    out: dict[str, dict] = {}
    for k, members in groups.items():
        if len(members) < min_copies:
            continue
        rows = []
        for mint, b in members:
            s = _stats(b, now, hit_ttl)
            rows.append({"mint": mint, "symbol": b.get("symbol"), "name": b.get("name"), "age_s": round(now - float(b.get("start") or now)),
                         "held": mint in active_mints, **s})
        rows.sort(key=lambda r: (r["smart_weighted"], r["smart_fresh"], r["mc_usd"], r["buyers"], r["inflow_sol_2m"]), reverse=True)
        leader = rows[0]
        out[k] = {"key": k, "copies": len(rows), "leader": leader["mint"], "leader_row": leader, "members": rows,
                  "held": [r["mint"] for r in rows if r["held"]], "smart_total": sum(r["smart_total"] for r in rows), "ts": now}
    return out


def cluster_for(clusters: dict[str, dict], b: dict | None) -> dict | None:
    k = norm_key((b or {}).get("symbol"), (b or {}).get("name"))
    return clusters.get(k) if k else None


def leader_label(c: dict) -> str:
    L = c["leader_row"]
    mc = f"${L['mc_usd'] / 1000:.0f}k" if L["mc_usd"] >= 1000 else f"${L['mc_usd']:.0f}"
    return f"{L.get('symbol') or '?'} {L['mint'][:4]}…{L['mint'][-4:]} · {L['smart_total']} smart ({L['smart_manual']} manual) · {mc} MC · {L['buyers']} buyers"


def gate(clusters: dict[str, dict], mint: str, b: dict | None, cfg) -> tuple[bool, str | None, dict | None]:
    """(allowed, refusal_detail, cluster). Allowed when the name is not trending, or this copy is the leader with
    enough smart buys and the per-name position cap is not yet used up."""
    c = cluster_for(clusters, b)
    if not c:
        return True, None, None
    min_smart = max(0, int(getattr(cfg, "trend_name_leader_min_smart", 1) or 0))
    max_pos = max(1, int(getattr(cfg, "trend_name_max_positions", 2) or 2))
    tag = f"trending name ×{c['copies']}"
    if mint != c["leader"]:
        return False, f"{tag}: not the leader — leader is {leader_label(c)}", c
    if c["leader_row"]["smart_total"] < min_smart:
        return False, f"{tag}: no smart wallet has bought any copy yet (leader needs ≥ {min_smart}) — {leader_label(c)}", c
    others = [m for m in c["held"] if m != mint]
    if len(others) >= max_pos:
        return False, f"{tag}: already holding {len(others)} cop{'y' if len(others) == 1 else 'ies'} of this name (cap {max_pos})", c
    return True, None, c


def annotate(clusters: dict[str, dict], m: dict, b: dict) -> None:
    """Stamp candidate-snapshot fields: trend_copies / trend_leader / trend_is_leader / trend_leader_label."""
    c = cluster_for(clusters, b)
    if not c:
        return
    m["trend_copies"] = c["copies"]
    m["trend_leader"] = c["leader"]
    m["trend_is_leader"] = m.get("mint") == c["leader"]
    m["trend_leader_label"] = leader_label(c)
    m["trend_leader_smart"] = c["leader_row"]["smart_total"]
    m["trend_held"] = len(c["held"])


def strip(clusters: dict[str, dict]) -> list[dict]:
    """Compact list for the UI strip, busiest names first."""
    out = []
    for c in clusters.values():
        L = c["leader_row"]
        out.append({"key": c["key"], "copies": c["copies"], "smart_total": c["smart_total"], "held": c["held"],
                    "wide": bool(c.get("wide")), "window_h": c.get("window_h"), "verdict": c.get("verdict"),
                    "leader": {k: L[k] for k in ("mint", "symbol", "name", "smart_total", "smart_manual", "mc_usd", "buyers", "age_s")},
                    "members": [{k: r[k] for k in ("mint", "symbol", "smart_total", "mc_usd", "buyers", "held")} for r in c["members"][:8]]})
    out.sort(key=lambda x: (x["smart_total"], x["copies"]), reverse=True)
    return out
