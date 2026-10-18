"""Holder ↔ smart-wallet cross-check — the tape can miss a smart wallet's buy (bought before we tracked the mint, via a
router, on the AMM, under the min-buy size, tracker slot pruned…). Every scan, for the busiest in-band candidates, read the
token's holders (DAS `getTokenAccounts`) and intersect the OWNERS with the smart-wallet list (your manual list first,
promoted learned too). A match is registered exactly like a tape hit → trending-name leader, creator-verdict override
(manual lifts BLOCK), soft rank boost, and the hard trigger."""
from __future__ import annotations

import logging
import time

import solana_client

logger = logging.getLogger("smart_holders")
PAGES = 3                      # ≤ 3 × 1000 token accounts per mint per check


async def holder_owners(mint: str) -> set[str]:
    owners: set[str] = set()
    for page in range(1, PAGES + 1):
        r = await solana_client.rpc_call("getTokenAccounts", {"mint": mint, "limit": 1000, "page": page, "options": {"showZeroBalance": False}}, timeout=15.0)
        if r.get("error"):
            raise RuntimeError((r["error"] or {}).get("message") or str(r["error"]))
        accts = list((r.get("result") or {}).get("token_accounts") or [])
        owners.update(a.get("owner") for a in accts if a.get("owner") and int(a.get("amount") or 0) > 0)
        if len(accts) < 1000:
            break
    return owners


def pick_candidates(tracking: dict, cfg, now: float, classify_band, active: set) -> list[tuple[str, dict]]:
    """In-band buckets worth a holder read, busiest first, not re-read inside `smart_holder_check_every_s`."""
    every = float(getattr(cfg, "smart_holder_check_every_s", 30) or 30)
    min_buyers = int(getattr(cfg, "smart_holder_check_min_buyers", 15) or 0)
    cap = max(0, int(getattr(cfg, "smart_holder_check_max_per_scan", 5) or 0))
    rows = []
    for mint, b in tracking.items():
        if mint in active or classify_band(b, cfg, now) is None:
            continue
        buyers = max(len(b.get("buyers") or ()), int(b.get("holder_count") or 0))
        if buyers < min_buyers:
            continue
        if now - float(b.get("_holder_check_ts") or 0) < every:
            continue
        rows.append((buyers + (1000 if (b.get("entry_refusal") or {}).get("reason") in ("creator-block", "trending-name") else 0), mint, b))
    rows.sort(key=lambda r: r[0], reverse=True)
    return [(m, b) for _, m, b in rows[:cap]]


async def sweep(state, now: float | None = None) -> dict:
    """One pass: read holders for the picked candidates, register smart-wallet holders as hits. Returns a small tally."""
    cfg = state.config
    now = now or time.time()
    tally = {"checked": 0, "matched": 0, "new_hits": 0, "errors": 0}
    if not bool(getattr(cfg, "smart_holder_check_enabled", True)) or not state.smart_wallets.enabled():
        return tally
    try:
        from helius_gate import is_helius_paused
        if is_helius_paused():
            return tally
    except Exception:
        pass
    from scanner import MomentumScanner
    sw = state.smart_wallets
    for mint, b in pick_candidates(state.tracking, cfg, now, MomentumScanner.classify_band, set(state.active_trades.keys())):
        b["_holder_check_ts"] = now
        tally["checked"] += 1
        try:
            owners = await holder_owners(mint)
        except Exception as e:
            tally["errors"] += 1
            logger.debug(f"holder check {mint[:8]}: {e}")
            continue
        b["holder_count"] = max(int(b.get("holder_count") or 0), len(owners))
        matches = [(w, sw.kind_of(w)) for w in owners if w != b.get("creator") and sw.kind_of(w) is not None]
        if not matches:
            continue
        tally["matched"] += 1
        hits = b.setdefault("smart_hits", {})
        fresh = [(w, k) for w, k in matches if w not in hits]
        for w, k in matches:
            if w in hits and hits[w].get("source") == "holder":
                hits[w]["ts"] = now                   # still holding → stays a fresh hit
        for w, k in fresh:
            hits[w] = {"ts": now, "kind": k, "sol": None, "source": "holder"}
            tally["new_hits"] += 1
            try:
                from creator_smart import record_hit
                record_hit(getattr(state, "db", None), b.get("creator"), mint, w, k, now)
            except Exception:
                pass
        if fresh:
            b["smart_hit_ts"] = now
            sw.stats["hits"] = sw.stats.get("hits", 0) + len(fresh)
            sw.stats["holder_hits"] = sw.stats.get("holder_hits", 0) + len(fresh)
            labels = ", ".join(f"{(sw.manual.get(w) or {}).get('label') or w[:4] + '…' + w[-4:]} [{k}]" for w, k in fresh)
            logger.info(f"smart HOLDER {b.get('symbol') or mint[:8]}: {len(fresh)} smart wallet{'s' if len(fresh) != 1 else ''} among {len(owners)} holders — {labels}")
            if sw.mode() in ("hard", "both") and not b.get("_sw_fired"):
                w, k = next(((w, k) for w, k in fresh if k == "manual"), fresh[0])
                sw._maybe_fire(mint, b, w, k, 0, now)
    return tally
