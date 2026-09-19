"""
Creator Wallet Audit — optional MASTER gate (`creator_audit_enabled`, default off).

Runs LAST in the entry path (after every momentum / liquidity / cost gate), cached for CACHE_TTL_S per creator.
Solana data: Solscan Pro API when SOLSCAN_API_KEY is set (account detail / funded-by / metadata labels / transactions /
defi activities / token meta), Helius enhanced history as the fallback per check. A launch may only be bought when its creator wallet looks like a person who set out to
launch one token, not a farm:

  funded_before      wallet held funds before the deploy (not zero → then funded → then deployed in one motion)
  prior_dex          interacted with Pump.fun / PumpSwap / Raydium / Jupiter / Orca (SOL) — or has sent txs before (RH, nonce proxy)
  funding_lead       the real funding transfer landed ≥ creator_audit_min_funding_lead_h before the deploy
  wallet_age         first activity ≥ creator_audit_min_wallet_age_h before the deploy
  deploys_per_hour   ≤ creator_audit_max_deploys_per_hour launches in the hour around this one
  post_activity      (optional, off by default) non-sell activity after the deploy
  tags               explorer labels/tags (Solscan account metadata) + our greylist: no scam/rug/bot/spam/cluster label, not blacklisted
  metadata           name + symbol present and printable; SOL: DAS metadata URI present and reachable
  verified           SOL: n/a (Pump.fun mints share one audited program); RH: unavailable (explorer blocked)

Each check ends pass / fail / unavailable / n/a. `creator_audit_unavailable` decides what an unavailable check means:
"pass" (judge on the rest) or "skip" (fail-closed).
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

import httpx

logger = logging.getLogger("creator_audit")

CACHE_TTL_S = 3600.0
HISTORY_PAGES = 3               # × 100 txs; deeper history than this counts as "old, active wallet"
_cache: dict[str, tuple[float, dict]] = {}
stats: dict = {"audits": 0, "passed": 0, "skipped": 0, "fails_by_check": {}, "unavailable_by_check": {}}

HELIUS_RPC = os.environ.get("HELIUS_RPC_URL", "")
_API_KEY = HELIUS_RPC.split("api-key=", 1)[1].split("&")[0] if "api-key=" in HELIUS_RPC else ""
HELIUS_ENHANCED = "https://api.helius.xyz/v0/addresses/{addr}/transactions"

PUMP_PROGRAM = os.environ.get("PUMP_PROGRAM_ID", "6EF8rKz1ykRAMDcw5cT99whBmpL8GHcEHUbZUQzLCJY4")
DEX_PROGRAMS = {
    PUMP_PROGRAM: "pump.fun",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "pumpswap",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "raydium",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "raydium-clmm",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "jupiter",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "orca",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "meteora",
}
DEX_SOURCES = {"PUMP_FUN", "PUMP_AMM", "RAYDIUM", "JUPITER", "ORCA", "METEORA"}
BAD_PATTERNS = ("spam", "rug", "bot", "cluster", "farm", "serial")
BAD_LABELS = ("scam", "rug", "phish", "drain", "hack", "exploit", "sybil", "spam", "bot", "cluster", "malicious", "fraud", "blacklist", "sanction")

CHECKS = [
    ("funded_before", "Funded before deploy"),
    ("prior_dex", "Prior DEX / launchpad use"),
    ("funding_lead", "Funding lead time"),
    ("wallet_age", "Wallet age"),
    ("deploys_per_hour", "Deploys this hour"),
    ("post_activity", "Post-deploy activity"),
    ("tags", "No malicious / bot / rug / cluster tags"),
    ("metadata", "Clean metadata"),
    ("verified", "Verified contract"),
]


def _c(key: str, status: str, detail: str = "", value=None) -> dict:
    return {"key": key, "label": dict(CHECKS)[key], "status": status, "detail": detail, "value": value}


def _hms(seconds: float) -> str:
    s = max(0.0, float(seconds))
    if s < 90:
        return f"{s:.0f}s"
    if s < 5400:
        return f"{s / 60:.0f}m"
    if s < 172800:
        return f"{s / 3600:.1f}h"
    return f"{s / 86400:.1f}d"


# ---------------------------------------------------------------- data: Solana (Helius enhanced history)
async def _sol_history(creator: str, until_ts: float) -> tuple[list[dict] | None, bool]:
    """Newest-first enriched txs (up to HISTORY_PAGES × 100). (None, False) when the API is unavailable.
    Second value: True when the wallet has MORE history than we fetched (deep = old, active wallet)."""
    if not _API_KEY:
        return None, False
    out: list[dict] = []
    before = None
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            for _ in range(HISTORY_PAGES):
                params = {"api-key": _API_KEY, "limit": 100}
                if before:
                    params["before"] = before
                r = await c.get(HELIUS_ENHANCED.format(addr=creator), params=params)
                if r.status_code != 200:
                    logger.debug(f"creator_audit helius {r.status_code} for {creator[:8]}")
                    return (out if out else None), False
                page = r.json() or []
                out.extend(page)
                if len(page) < 100:
                    return out, False
                before = page[-1].get("signature")
                if not before:
                    return out, False
        return out, True
    except Exception as e:
        logger.debug(f"creator_audit helius error {creator[:8]}: {e}")
        return (out if out else None), False


def _tx_programs(tx: dict) -> set[str]:
    pids = set()
    for ix in tx.get("instructions") or []:
        if ix.get("programId"):
            pids.add(ix["programId"])
        for inner in ix.get("innerInstructions") or []:
            if inner.get("programId"):
                pids.add(inner["programId"])
    return pids


def _is_pump_create(tx: dict) -> bool:
    t = (tx.get("type") or "").upper()
    if "CREATE" in t and (tx.get("source") or "").upper() in ("PUMP_FUN", "PUMP_AMM"):
        return True
    desc = (tx.get("description") or "").lower()
    return PUMP_PROGRAM in _tx_programs(tx) and ("create" in desc or "mint" in desc and "created" in desc)


def _is_sell_of(tx: dict, mint: str, creator: str) -> bool:
    for tt in tx.get("tokenTransfers") or []:
        if tt.get("mint") == mint and tt.get("fromUserAccount") == creator:
            return True
    return False


async def _sol_metadata(mint: str) -> dict:
    """DAS getAsset → name / symbol / uri (+ whether the uri answers). {} when DAS is unavailable."""
    try:
        import solana_client
        r = await solana_client.rpc_call("getAsset", {"id": mint}, timeout=8.0)
        res = (r or {}).get("result") or {}
        meta = ((res.get("content") or {}).get("metadata") or {})
        uri = (res.get("content") or {}).get("json_uri") or ""
        reachable = None          # True / False / None (= gateways rate-limited us: unknown, not a failure)
        if uri:
            candidates = [uri]
            if "/ipfs/" in uri:
                cid = uri.split("/ipfs/", 1)[1]
                candidates += [f"https://gateway.pinata.cloud/ipfs/{cid}", f"https://cloudflare-ipfs.com/ipfs/{cid}"]
            async with httpx.AsyncClient(timeout=4.0, follow_redirects=True) as c:
                for u in candidates:
                    try:
                        code = (await c.get(u)).status_code
                    except Exception:
                        continue
                    if code < 400:
                        reachable = True
                        break
                    if code in (404, 410):
                        reachable = False
                        break
        return {"name": meta.get("name") or "", "symbol": meta.get("symbol") or "", "uri": uri, "uri_reachable": reachable, "ok": True}
    except Exception as e:
        logger.debug(f"creator_audit getAsset {mint[:8]}: {e}")
        return {}


def _clean_text(s: str) -> bool:
    s = (s or "").strip()
    return bool(s) and s.isprintable() and not any(ord(ch) > 0xFFFF for ch in s) and len(s) <= 64


async def _our_deploys(db, chain: str, creator: str, deploy_ts: float, window_s: float) -> list[float]:
    """Launch timestamps we saw from this creator within ±window_s of the deploy (excluding the deploy itself by time)."""
    if db is None:
        return []
    lo = datetime.fromtimestamp(deploy_ts - window_s, tz=timezone.utc)
    hi = datetime.fromtimestamp(deploy_ts + window_s, tz=timezone.utc)
    out = []
    try:
        cur = db.launches.find({"chain": chain, "creator": creator, "detected_at": {"$gte": lo, "$lte": hi}}, {"_id": 0, "detected_at": 1, "mint": 1}).limit(50)
        async for d in cur:
            dt = d.get("detected_at")
            if isinstance(dt, datetime):
                out.append(dt.replace(tzinfo=dt.tzinfo or timezone.utc).timestamp())
    except Exception as e:
        logger.debug(f"creator_audit launches lookup: {e}")
    return out


async def _tags(db, creator: str) -> dict:
    try:
        from creator_greylist import _get_blacklisted_creators
        from creator_history import derive_rug_count
        black = await _get_blacklisted_creators(db) if db is not None else set()
        doc = await db.creators.find_one({"_id": creator}) if db is not None else None
        return {"ok": True, "blacklisted": creator in black, "rugs": derive_rug_count(doc), "pattern": (doc or {}).get("greylist_pattern") or "unknown",
                "score": float((doc or {}).get("greylist_score") or 0.0)}
    except Exception as e:
        logger.debug(f"creator_audit tags: {e}")
        return {"ok": False}


# ---------------------------------------------------------------- data: Solscan (explorer)
async def _solscan_profile(creator: str, mint: str, deploy_ts: float) -> dict:
    """Explorer view of the creator. Each field is None when that endpoint is unavailable on the current plan/quota."""
    import solscan
    if not solscan.enabled():
        return {}
    out: dict = {"provider": "solscan", "errors": {}}

    async def grab(key, coro):
        try:
            out[key] = await coro
        except Exception as e:
            out[key] = None
            out["errors"][key] = str(e)[:80]
            logger.debug(f"creator_audit solscan {key} {creator[:8]}: {e}")

    await grab("detail", solscan.account_detail(creator))
    await grab("funded_by", solscan.account_funded_by(creator))
    await grab("metadata", solscan.account_metadata(creator))
    await grab("token", solscan.token_meta(mint))
    await grab("defi", solscan.account_defi_activities(creator, to_time=int(deploy_ts) - 1))
    txs: list[dict] = []
    before = None
    deep = False
    for _ in range(3):
        try:
            page = await solscan.account_transactions(creator, before=before)
        except Exception as e:
            out["errors"]["transactions"] = str(e)[:80]
            page = None
        if page is None:
            break
        txs.extend(page)
        if len(page) < 40:
            break
        before = page[-1].get("tx_hash")
        if not before:
            break
    else:
        deep = True
    out["txs"] = txs if ("transactions" not in out["errors"] or txs) else None
    out["deep"] = deep
    return out


def _tx_ts(t: dict) -> float:
    return float(t.get("block_time") or t.get("blockTime") or t.get("timestamp") or 0)


def _tx_pids(t: dict) -> set[str]:
    pids = set(t.get("program_ids") or [])
    for ix in t.get("parsed_instructions") or []:
        if ix.get("program_id"):
            pids.add(ix["program_id"])
    return pids


def _is_solscan_create(t: dict) -> bool:
    if PUMP_PROGRAM not in _tx_pids(t):
        return False
    return any("create" in str(ix.get("type") or "").lower() for ix in t.get("parsed_instructions") or [])


# ---------------------------------------------------------------- evaluation
async def audit(cfg, db, *, chain: str, creator: str, mint: str, deploy_ts: float, deploy_block: int | None = None,
                name: str | None = None, symbol: str | None = None, force: bool = False) -> dict:
    """Full audit → {"verdict": "pass"|"skip", "reason": str|None, "checks": [...], "chain", "creator", "ts"}."""
    key = f"{chain}:{creator}:{mint}"
    now = time.time()
    hit = _cache.get(key)
    if hit and not force and now - hit[0] < CACHE_TTL_S:
        return hit[1]
    checks = await (_audit_sol if chain == "sol" else _audit_rh)(cfg, db, creator, mint, deploy_ts, deploy_block, name, symbol)
    policy = str(getattr(cfg, "creator_audit_unavailable", "pass") or "pass")
    failed = [c for c in checks if c["status"] == "fail"]
    unavailable = [c for c in checks if c["status"] == "unavailable"]
    verdict, reason = "pass", None
    if failed:
        verdict, reason = "skip", f"creator-audit: {failed[0]['label'].lower()} — {failed[0]['detail']}"
    elif unavailable and policy == "skip":
        verdict, reason = "skip", f"creator-audit: {unavailable[0]['label'].lower()} unavailable (fail-closed)"
    res = {"verdict": verdict, "reason": reason, "checks": checks, "chain": chain, "creator": creator, "mint": mint, "ts": now,
           "failed": [c["key"] for c in failed], "unavailable": [c["key"] for c in unavailable], "policy": policy}
    stats["audits"] += 1
    stats["passed" if verdict == "pass" else "skipped"] += 1
    for c in failed:
        stats["fails_by_check"][c["key"]] = stats["fails_by_check"].get(c["key"], 0) + 1
    for c in unavailable:
        stats["unavailable_by_check"][c["key"]] = stats["unavailable_by_check"].get(c["key"], 0) + 1
    _cache[key] = (now, res)
    return res


def _solscan_token_md(tok: dict) -> dict:
    return {"name": tok.get("name") or "", "symbol": tok.get("symbol") or "", "uri": tok.get("metadata_uri") or tok.get("uri") or "",
            "uri_reachable": True if (tok.get("metadata") or tok.get("icon")) else None, "ok": True}


def _solscan_checks(prof: dict, out: list, deploy_ts: float, min_lead: float, min_age: float, min_dex: int, max_per_h: int,
                    want_post: bool, ours: list[float]) -> bool:
    """Fill the history checks from the Solscan profile. Returns True when at least one check came from Solscan."""
    used = False
    fb = prof.get("funded_by")
    if fb:
        used = True
        f_ts = float(fb.get("block_time") or 0)
        if f_ts and f_ts < deploy_ts:
            lead = deploy_ts - f_ts
            out.append(_c("funded_before", "pass", f"funded by {str(fb.get('funded_by') or '?')[:8]}… {_hms(lead)} before deploy", 1))
            out.append(_c("funding_lead", "pass" if lead >= min_lead else "fail", f"first funding {_hms(lead)} before deploy (min {_hms(min_lead)})", round(lead)))
        elif f_ts:
            out.append(_c("funded_before", "fail", "first funding landed after the deploy", 0))
            out.append(_c("funding_lead", "fail", "funded after the deploy", None))
    txs = prof.get("txs")
    if txs is not None:
        used = True
        before = [t for t in txs if _tx_ts(t) and _tx_ts(t) < deploy_ts - 1]
        after = [t for t in txs if _tx_ts(t) > deploy_ts + 1]
        if prof.get("deep"):
            out.append(_c("wallet_age", "pass", "> 120 txs — established wallet", None))
        elif before:
            age = deploy_ts - min(_tx_ts(t) for t in before)
            out.append(_c("wallet_age", "pass" if age >= min_age else "fail", f"first activity {_hms(age)} before deploy (min {_hms(min_age)})", round(age)))
        else:
            out.append(_c("wallet_age", "fail", "no activity before the deploy — fresh wallet", 0))
        if not fb:
            out.append(_c("funded_before", "pass" if before or prof.get("deep") else "fail", f"{len(before)} tx before deploy", len(before)))
            out.append(_c("funding_lead", "unavailable", "funded-by not available on this plan"))
        dex_hits: dict[str, int] = {}
        for t in before:
            for pid in _tx_pids(t) & set(DEX_PROGRAMS):
                dex_hits[DEX_PROGRAMS[pid]] = dex_hits.get(DEX_PROGRAMS[pid], 0) + 1
        n_defi = len(prof.get("defi") or [])
        n_dex = max(sum(dex_hits.values()), n_defi)
        if n_dex >= min_dex or (prof.get("deep") and n_dex > 0):
            out.append(_c("prior_dex", "pass", ", ".join(f"{k}×{v}" for k, v in sorted(dex_hits.items())) or f"{n_defi} DeFi activities", n_dex))
        else:
            out.append(_c("prior_dex", "fail", f"{n_dex} prior DEX / launchpad tx (min {min_dex})", n_dex))
        creates = [_tx_ts(t) for t in txs if _is_solscan_create(t)]
        others = sorted({round(ts) for ts in creates + list(ours) if abs(ts - deploy_ts) > 2 and abs(ts - deploy_ts) <= 3600})
        n_hour = 1 + len(others)
        out.append(_c("deploys_per_hour", "pass" if n_hour <= max_per_h else "fail", f"{n_hour} deploy(s) in the hour (max {max_per_h})", n_hour))
        if after:
            out.append(_c("post_activity", "pass", f"{len(after)} tx after deploy", len(after)))
        else:
            out.append(_c("post_activity", "fail" if want_post else "n/a", "no activity after deploy yet" + ("" if want_post else " (not required)"), 0))
    elif fb and prof.get("detail") is not None:
        used = True
    return used


async def _audit_sol(cfg, db, creator, mint, deploy_ts, deploy_block, name, symbol) -> list[dict]:
    min_lead = float(getattr(cfg, "creator_audit_min_funding_lead_h", 1.0)) * 3600.0
    min_age = float(getattr(cfg, "creator_audit_min_wallet_age_h", 24.0)) * 3600.0
    min_dex = int(getattr(cfg, "creator_audit_min_prior_dex", 1))
    max_per_h = int(getattr(cfg, "creator_audit_max_deploys_per_hour", 1))
    want_post = bool(getattr(cfg, "creator_audit_require_post_activity", False))
    max_rugs = int(getattr(cfg, "creator_audit_max_rug_tags", 1))
    out: list[dict] = []

    prof = await _solscan_profile(creator, mint, deploy_ts)
    used_solscan = False
    if prof:
        used_solscan = _solscan_checks(prof, out, deploy_ts, min_lead, min_age, min_dex, max_per_h, want_post,
                                       await _our_deploys(db, "sol", creator, deploy_ts, 3600.0))
    done = {c["key"] for c in out}
    txs, deep = (None, False) if done >= {"funded_before", "prior_dex", "funding_lead", "wallet_age", "deploys_per_hour", "post_activity"} else await _sol_history(creator, deploy_ts)
    if txs is None:
        for k in ("funded_before", "prior_dex", "funding_lead", "wallet_age", "deploys_per_hour", "post_activity"):
            if k not in done:
                out.append(_c(k, "unavailable", "wallet history unavailable (Solscan + Helius)" if prof else "wallet history unavailable (Helius)"))
    else:
        before = [t for t in txs if float(t.get("timestamp") or 0) < deploy_ts - 1]
        after = [t for t in txs if float(t.get("timestamp") or 0) > deploy_ts + 1]
        # funding: inbound native transfers before the deploy
        inbound = []
        for t in before:
            for nt in t.get("nativeTransfers") or []:
                if nt.get("toUserAccount") == creator and nt.get("fromUserAccount") != creator:
                    inbound.append((float(t.get("timestamp") or 0), int(nt.get("amount") or 0)))
        if inbound:
            out.append(_c("funded_before", "pass", f"{len(inbound)} inbound transfer(s) before deploy", len(inbound)))
            big_ts = max(inbound, key=lambda x: x[1])[0]
            lead = deploy_ts - big_ts
            out.append(_c("funding_lead", "pass" if lead >= min_lead else "fail",
                          f"main funding {_hms(lead)} before deploy (min {_hms(min_lead)})", round(lead)))
        elif deep:
            out.append(_c("funded_before", "pass", "deep history — funded long before", None))
            out.append(_c("funding_lead", "pass", "deep history — funding predates the fetched window", None))
        else:
            out.append(_c("funded_before", "fail", "no inbound funding seen before the deploy", 0))
            out.append(_c("funding_lead", "fail", "no funding transfer before the deploy", None))
        # wallet age
        if deep:
            out.append(_c("wallet_age", "pass", f"> {HISTORY_PAGES * 100} txs — established wallet", None))
        elif before:
            oldest = min(float(t.get("timestamp") or deploy_ts) for t in before)
            age = deploy_ts - oldest
            out.append(_c("wallet_age", "pass" if age >= min_age else "fail", f"first activity {_hms(age)} before deploy (min {_hms(min_age)})", round(age)))
        else:
            out.append(_c("wallet_age", "fail", "no activity before the deploy — fresh wallet", 0))
        # prior DEX use
        dex_hits: dict[str, int] = {}
        for t in before:
            src = (t.get("source") or "").upper()
            if src in DEX_SOURCES:
                dex_hits[src.lower()] = dex_hits.get(src.lower(), 0) + 1
                continue
            for pid in _tx_programs(t) & set(DEX_PROGRAMS):
                dex_hits[DEX_PROGRAMS[pid]] = dex_hits.get(DEX_PROGRAMS[pid], 0) + 1
        n_dex = sum(dex_hits.values())
        if n_dex >= min_dex or (deep and n_dex > 0):
            out.append(_c("prior_dex", "pass", ", ".join(f"{k}×{v}" for k, v in sorted(dex_hits.items())) or "seen", n_dex))
        else:
            out.append(_c("prior_dex", "fail", f"{n_dex} prior DEX / launchpad tx (min {min_dex})", n_dex))
        # batch + deploys per hour (Helius creates + our own feed)
        creates = [float(t.get("timestamp") or 0) for t in txs if _is_pump_create(t)]
        ours = await _our_deploys(db, "sol", creator, deploy_ts, 3600.0)
        others = sorted({round(ts) for ts in creates + ours if abs(ts - deploy_ts) > 2})
        in_hour = [ts for ts in others if abs(ts - deploy_ts) <= 3600]
        n_hour = 1 + len(in_hour)
        out.append(_c("deploys_per_hour", "pass" if n_hour <= max_per_h else "fail", f"{n_hour} deploy(s) in the hour (max {max_per_h})", n_hour))
        # post-deploy activity (non-sell)
        post = [t for t in after if not _is_sell_of(t, mint, creator)]
        sells = [t for t in after if _is_sell_of(t, mint, creator)]
        if sells:
            out.append(_c("post_activity", "fail", f"creator sold this token {len(sells)}× after deploy", len(sells)))
        elif post:
            out.append(_c("post_activity", "pass", f"{len(post)} non-sell tx after deploy", len(post)))
        else:
            out.append(_c("post_activity", "fail" if want_post else "n/a", "no activity after deploy yet" + ("" if want_post else " (not required)"), 0))

    _seen: set = set()
    out = [c for c in out if not (c["key"] in _seen or _seen.add(c["key"]))]   # Solscan answers win over the Helius fallback
    tg = await _tags(db, creator)
    meta = (prof or {}).get("metadata") or {}
    labels = [str(x) for x in ([meta.get("account_label")] if meta.get("account_label") else []) + list(meta.get("account_tags") or [])]
    bad = [l for l in labels if any(w in l.lower() for w in BAD_LABELS)]
    if bad:
        out.append(_c("tags", "fail", f"explorer label: {', '.join(bad)[:60]}", bad))
    elif not tg.get("ok") and not meta:
        out.append(_c("tags", "unavailable", "greylist unavailable" + ("" if not prof else " · explorer metadata unavailable")))
    elif tg.get("blacklisted"):
        out.append(_c("tags", "fail", "creator is blacklisted (greylist)", "blacklisted"))
    elif tg.get("ok") and tg["rugs"] >= max_rugs:
        out.append(_c("tags", "fail", f"{tg['rugs']} failed/rugged launch(es) on record (max {max_rugs - 1})", tg["rugs"]))
    elif tg.get("ok") and any(p in str(tg["pattern"]).lower() for p in BAD_PATTERNS):
        out.append(_c("tags", "fail", f"greylist pattern '{tg['pattern']}'", tg["pattern"]))
    else:
        src = f"explorer: {', '.join(labels)[:40]}" if labels else ("explorer: no labels" if meta else "no explorer tag source")
        out.append(_c("tags", "pass", f"{src} · greylist {tg.get('pattern', '?')} · rugs {tg.get('rugs', '?')}", labels or None))

    md = await _sol_metadata(mint) if not ((prof or {}).get("token")) else _solscan_token_md(prof["token"])
    nm, sy = (md.get("name") or name or ""), (md.get("symbol") or symbol or "")
    if not md and not (name or symbol):
        out.append(_c("metadata", "unavailable", "DAS metadata unavailable"))
    elif not (_clean_text(nm) and _clean_text(sy)):
        out.append(_c("metadata", "fail", f"name/symbol missing or unprintable ({nm[:16]!r} / {sy[:8]!r})"))
    elif md.get("uri") and md.get("uri_reachable") is False:
        out.append(_c("metadata", "fail", "metadata URI does not resolve (404)"))
    elif md.get("uri") and md.get("uri_reachable") is None:
        out.append(_c("metadata", "unavailable", f"{nm[:20]} / {sy[:10]} · IPFS gateways rate-limited, URI unverified"))
    elif md and not md.get("uri"):
        out.append(_c("metadata", "fail", "no metadata URI"))
    else:
        out.append(_c("metadata", "pass", f"{nm[:20]} / {sy[:10]}" + (" · uri ok" if md.get("uri_reachable") else "")))
    out.append(_c("verified", "n/a", "Pump.fun mints share one audited program"))
    out.append({"key": "_provider", "label": "", "status": "n/a", "detail": "solscan" if (prof and used_solscan) else "helius", "value": None})
    return out


async def _audit_rh(cfg, db, creator, mint, deploy_ts, deploy_block, name, symbol) -> list[dict]:
    min_dex = int(getattr(cfg, "creator_audit_min_prior_dex", 1))
    max_per_h = int(getattr(cfg, "creator_audit_max_deploys_per_hour", 1))
    want_post = bool(getattr(cfg, "creator_audit_require_post_activity", False))
    max_rugs = int(getattr(cfg, "creator_audit_max_rug_tags", 1))
    out: list[dict] = []
    tag = hex(int(deploy_block) - 1) if deploy_block else "latest"
    try:
        import rh_wallet
        bal = int(await rh_wallet.rpc("eth_getBalance", [creator, tag], timeout=8.0), 16)
        nonce = int(await rh_wallet.rpc("eth_getTransactionCount", [creator, tag], timeout=8.0), 16)
        out.append(_c("funded_before", "pass" if bal > 0 else "fail", f"{bal / 1e18:.5f} ETH at block {int(tag, 16) if tag != 'latest' else 'latest'}", bal / 1e18))
        out.append(_c("prior_dex", "pass" if nonce >= min_dex else "fail", f"{nonce} tx sent before deploy (nonce proxy, min {min_dex})", nonce))
    except Exception as e:
        logger.debug(f"creator_audit rh rpc {creator[:10]}: {e}")
        out.append(_c("funded_before", "unavailable", "RPC unavailable"))
        out.append(_c("prior_dex", "unavailable", "RPC unavailable"))
    out.append(_c("funding_lead", "unavailable", "no tx history API on Robinhood Chain (explorer blocked)"))
    out.append(_c("wallet_age", "unavailable", "no tx history API on Robinhood Chain (explorer blocked)"))
    ours = await _our_deploys(db, "rh", creator, deploy_ts, 3600.0)
    others = [ts for ts in ours if abs(ts - deploy_ts) > 2]
    n_hour = 1 + len(others)
    out.append(_c("deploys_per_hour", "pass" if n_hour <= max_per_h else "fail", f"{n_hour} deploy(s) in the hour (max {max_per_h}, our feed)", n_hour))
    out.append(_c("post_activity", "fail" if want_post else "n/a", "not observable on Robinhood Chain" + ("" if want_post else " (not required)")))
    tg = await _tags(db, creator)
    if not tg.get("ok"):
        out.append(_c("tags", "unavailable", "greylist unavailable"))
    elif tg["blacklisted"]:
        out.append(_c("tags", "fail", "creator is blacklisted (greylist)", "blacklisted"))
    elif tg["rugs"] >= max_rugs:
        out.append(_c("tags", "fail", f"{tg['rugs']} failed/rugged launch(es) on record (max {max_rugs - 1})", tg["rugs"]))
    else:
        out.append(_c("tags", "pass", f"rugs {tg['rugs']} (explorer tags unavailable)", tg["rugs"]))
    if _clean_text(name or "") and _clean_text(symbol or ""):
        out.append(_c("metadata", "pass", f"{(name or '')[:20]} / {(symbol or '')[:10]}"))
    else:
        out.append(_c("metadata", "fail", f"name/symbol missing or unprintable ({(name or '')[:16]!r} / {(symbol or '')[:8]!r})"))
    out.append(_c("verified", "unavailable", "Blockscout API blocked from this host"))
    return out
