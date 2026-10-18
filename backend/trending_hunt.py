"""Trending-name hunt — when a ticker is trending, look for its TOP DOG outside the band time gates (last
`trend_name_search_window_h`, default 4 h) and enter only that one.

Wide search = every launch with that ticker in Mongo `launches` (+ tracked buckets) → DexScreener batch stats (MC, buys,
volume, 1 h change) → smart-wallet HOLDER counts for the top copies by MC (DAS) → rank: smart wallets (manual ×2) → MC →
1 h buys → holders. The wide leader overrides the in-band cluster leader, so the normal trending-name gate refuses every
other copy, and the leader is entered through `_enter` (Creator Verdict, cost gate, audits all still apply)."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone

import httpx

import smart_holders
import trending_names as tn

logger = logging.getLogger("trending_hunt")
DEX_URL = "https://api.dexscreener.com/tokens/v1/solana/{mints}"
DEX_BATCH = 30
MAX_COPIES = 90


def _symbol_regex(key: str) -> dict:
    # "fly" matches "$FLY", "Fly ", "f.l.y" — any non-alphanumerics around / between the letters
    return {"$regex": "^[\\W_]*" + "[\\W_]*".join(re.escape(ch) for ch in key) + "[\\W_]*$", "$options": "i"}


async def wide_candidates(state, key: str, now: float) -> dict[str, dict]:
    """{mint: {mint, symbol, name, creator, launched_ts}} for every copy of `key` inside the search window."""
    cfg = state.config
    hours = float(getattr(cfg, "trend_name_search_window_h", 4.0) or 4.0)
    since = datetime.fromtimestamp(now - hours * 3600.0, tz=timezone.utc)
    out: dict[str, dict] = {}
    try:
        q = {"detected_at": {"$gte": since.isoformat()}, "$or": [{"symbol": _symbol_regex(key)}, {"name": _symbol_regex(key)}]}
        rows = await state.db.launches.find(q, {"_id": 0, "mint": 1, "symbol": 1, "name": 1, "creator": 1, "detected_at": 1}).limit(MAX_COPIES).to_list(MAX_COPIES)
    except Exception as e:
        logger.debug(f"wide search {key}: {e}")
        rows = []
    for r in rows:
        if tn.norm_key(r.get("symbol"), r.get("name")) != key:
            continue
        ts = None
        try:
            ts = datetime.fromisoformat(str(r.get("detected_at")).replace("Z", "+00:00")).timestamp()
        except Exception:
            pass
        out[r["mint"]] = {"mint": r["mint"], "symbol": r.get("symbol"), "name": r.get("name"), "creator": r.get("creator") or "", "launched_ts": ts}
    for mint, b in state.tracking.items():
        if tn.norm_key(b.get("symbol"), b.get("name")) == key and now - float(b.get("start") or now) <= hours * 3600.0:
            out.setdefault(mint, {"mint": mint, "symbol": b.get("symbol"), "name": b.get("name"), "creator": b.get("creator") or "", "launched_ts": b.get("start")})
    return out


async def dex_stats(mints: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    async with httpx.AsyncClient(timeout=10.0) as client:
        for i in range(0, len(mints), DEX_BATCH):
            chunk = mints[i:i + DEX_BATCH]
            try:
                r = await client.get(DEX_URL.format(mints=",".join(chunk)), headers={"accept": "application/json"})
                r.raise_for_status()
                pairs = r.json() or []
            except Exception as e:
                logger.debug(f"dex batch failed: {e}")
                continue
            for p in pairs if isinstance(pairs, list) else []:
                m = (p.get("baseToken") or {}).get("address")
                if m not in chunk:
                    continue
                tx = (p.get("txns") or {}).get("h1") or {}
                cur = out.setdefault(m, {"mc_usd": 0.0, "buys_h1": 0, "vol_h1": 0.0, "chg_h1": None, "liq_usd": 0.0, "dex": (p.get("dexId") or "")})
                cur["mc_usd"] = max(cur["mc_usd"], float(p.get("marketCap") or p.get("fdv") or 0.0))
                cur["buys_h1"] += int(tx.get("buys") or 0)
                cur["vol_h1"] += float((p.get("volume") or {}).get("h1") or 0.0)
                cur["liq_usd"] += float((p.get("liquidity") or {}).get("usd") or 0.0)
                ch = (p.get("priceChange") or {}).get("h1")
                if ch is not None:
                    cur["chg_h1"] = float(ch) if cur["chg_h1"] is None else max(cur["chg_h1"], float(ch))
            if i + DEX_BATCH < len(mints):
                await asyncio.sleep(0.2)
    return out


async def rank_copies(state, copies: dict[str, dict], now: float) -> list[dict]:
    cfg = state.config
    sw = state.smart_wallets
    stats = await dex_stats(list(copies)) if copies else {}
    rows = []
    for mint, c in copies.items():
        s = stats.get(mint) or {}
        b = state.tracking.get(mint) or {}
        hits = b.get("smart_hits") or {}
        rows.append({**c, "mc_usd": float(s.get("mc_usd") or b.get("usd_market_cap") or 0.0), "buys_h1": int(s.get("buys_h1") or 0),
                     "vol_h1": float(s.get("vol_h1") or 0.0), "chg_h1": s.get("chg_h1"), "liq_usd": float(s.get("liq_usd") or 0.0),
                     "holders": int(b.get("holder_count") or 0), "smart_total": len(hits),
                     "smart_manual": sum(1 for h in hits.values() if h.get("kind") == "manual"), "tracked": mint in state.tracking,
                     "held": mint in state.active_trades, "age_min": round((now - float(c.get("launched_ts") or now)) / 60.0)})
    rows.sort(key=lambda r: (r["mc_usd"], r["buys_h1"]), reverse=True)
    reads = max(0, int(getattr(cfg, "trend_name_holder_reads", 8) or 0))
    for r in rows[:reads]:
        try:
            owners = await smart_holders.holder_owners(r["mint"])
        except Exception as e:
            logger.debug(f"holders {r['mint'][:8]}: {e}")
            continue
        r["holders"] = max(r["holders"], len(owners))
        matches = [(w, sw.kind_of(w)) for w in owners if w != r.get("creator") and sw.kind_of(w) is not None]
        r["smart_total"] = max(r["smart_total"], len(matches))
        r["smart_manual"] = max(r["smart_manual"], sum(1 for _, k in matches if k == "manual"))
        b = state.tracking.get(r["mint"])
        if b is not None:
            hits = b.setdefault("smart_hits", {})
            for w, k in matches:
                hits.setdefault(w, {"ts": now, "kind": k, "sol": None, "source": "holder"})
            b["holder_count"] = max(int(b.get("holder_count") or 0), len(owners))
    for r in rows:
        r["smart_weighted"] = r["smart_total"] + r["smart_manual"]
    rows.sort(key=lambda r: (r["smart_weighted"], r["mc_usd"], r["buys_h1"], r["holders"]), reverse=True)
    return rows


async def hunt_cluster(state, key: str, now: float) -> dict | None:
    """Wide-search one trending name, store the wide view on the state, enter the top dog when allowed."""
    cfg = state.config
    copies = await wide_candidates(state, key, now)
    if len(copies) < 2:
        return None
    rows = await rank_copies(state, copies, now)
    leader = rows[0]
    wide = {"key": key, "ts": now, "copies": len(rows), "leader": leader["mint"], "leader_row": leader, "members": rows,
            "held": [r["mint"] for r in rows if r["held"]], "window_h": float(getattr(cfg, "trend_name_search_window_h", 4.0) or 4.0)}
    state._trend_wide[key] = wide
    min_smart = max(0, int(getattr(cfg, "trend_name_leader_min_smart", 1) or 0))
    max_pos = max(1, int(getattr(cfg, "trend_name_max_positions", 2) or 2))
    if leader["smart_total"] < min_smart:
        wide["verdict"] = f"top dog {leader['mint'][:4]}… has {leader['smart_total']} smart holders (< {min_smart}) — waiting"
        return wide
    if leader["held"]:
        wide["verdict"] = "already holding the top dog"
        return wide
    if len(wide["held"]) >= max_pos:
        wide["verdict"] = f"holding {len(wide['held'])} copies (cap {max_pos})"
        return wide
    if state.kill_switch_tripped or not state.config.enabled:
        wide["verdict"] = "bot off / kill switch"
        return wide
    last = state._trend_entered.get(leader["mint"], 0.0)
    if now - last < 120:
        wide["verdict"] = "entry attempted recently"
        return wide
    state._trend_entered[leader["mint"]] = now
    b = state.tracking.get(leader["mint"])
    if b is None:
        b = await state.seed_untracked(leader["mint"], "trending top dog")
        if isinstance(b, str):
            wide["verdict"] = f"could not seed top dog: {b}"
            return wide
    b["trend_leader_ts"] = now
    from models import Launch
    launch = Launch(mint=leader["mint"], creator=b.get("creator") or leader.get("creator") or "", bonding_curve="", name=b.get("name"), symbol=b.get("symbol"))
    launch.id = b.get("launch_id") or launch.id
    launch.classifier_action = "trending_leader"
    logger.info(f"trending top dog [{key} ×{len(rows)} in {wide['window_h']:g}h]: {leader.get('symbol')} {leader['mint'][:8]}… "
                f"{leader['smart_total']} smart ({leader['smart_manual']} manual) · ${leader['mc_usd']:,.0f} MC · {leader['buys_h1']} buys/1h · {leader['age_min']} min old — entering")
    try:
        await state._enter(launch, 50, "trending_leader")
    except Exception as e:
        logger.exception(f"trending leader entry failed for {leader['mint']}: {e}")
    opened = leader["mint"] in state.active_trades
    ref = (state.tracking.get(leader["mint"]) or {}).get("entry_refusal") or {}
    wide["verdict"] = "ENTERED top dog" if opened else f"entry refused: {ref.get('reason')} — {ref.get('detail')}" if ref else "entry did not open"
    return wide


async def run(state, now: float | None = None) -> None:
    """One pass over the in-band trending clusters; each name is wide-searched every `trend_name_hunt_every_s`."""
    cfg = state.config
    if not bool(getattr(cfg, "trend_name_gate_enabled", True)) or not bool(getattr(cfg, "trend_name_hunt_enabled", True)):
        return
    now = now or time.time()
    every = float(getattr(cfg, "trend_name_hunt_every_s", 20) or 20)
    clusters = tn.build_clusters(state.tracking, cfg, now, set(state.active_trades.keys()))
    for key in list(clusters):
        if now - state._trend_hunt_last.get(key, 0.0) < every:
            continue
        state._trend_hunt_last[key] = now
        try:
            await hunt_cluster(state, key, now)
        except Exception as e:
            logger.warning(f"trending hunt {key}: {e}")
    # forget wide views whose name stopped trending a while ago
    for key in list(state._trend_wide):
        if key not in clusters and now - float(state._trend_wide[key].get("ts") or 0) > 600:
            state._trend_wide.pop(key, None)


def overlay(clusters: dict[str, dict], wide_views: dict[str, dict], now: float) -> dict[str, dict]:
    """The wide (4 h) leader replaces the in-band leader so the gate / strip / chips all point at the top dog."""
    for key, w in wide_views.items():
        if now - float(w.get("ts") or 0) > 900:
            continue
        c = clusters.get(key)
        L = w["leader_row"]
        lrow = {"mint": L["mint"], "symbol": L.get("symbol"), "name": L.get("name"), "age_s": L["age_min"] * 60, "held": L["held"],
                "smart_total": L["smart_total"], "smart_manual": L["smart_manual"], "smart_fresh": 0, "smart_weighted": L["smart_weighted"],
                "mc_usd": L["mc_usd"], "buyers": L["holders"], "inflow_sol_2m": 0.0}
        if c is None:
            clusters[key] = {"key": key, "copies": w["copies"], "leader": L["mint"], "leader_row": lrow, "members": [lrow], "held": w["held"],
                             "smart_total": sum(r["smart_total"] for r in w["members"]), "ts": now, "wide": True, "window_h": w["window_h"], "verdict": w.get("verdict")}
            continue
        c["leader"], c["leader_row"] = L["mint"], lrow
        c["copies"] = max(c["copies"], w["copies"])
        c["held"] = sorted(set(c["held"]) | set(w["held"]))
        c["smart_total"] = max(c["smart_total"], sum(r["smart_total"] for r in w["members"]))
        c["wide"], c["window_h"], c["verdict"] = True, w["window_h"], w.get("verdict")
    return clusters
