"""
Creator wallet history & rug detection.

Strategy (real, not mocked):
  1) Local Mongo index `creators` grows as we observe Create events.
  2) On first encounter, best-effort backfill via Helius enhanced txns
     API to count prior Pump.fun activity (gracefully optional).
  3) When a launch's tracking window ends, mark outcome:
       - graduated  (bonding curve complete)
       - failed     (abandoned / low fill after window)
       - active     (still ongoing — re-checkable later)
  4) creator_rugs = tokens_failed (used by classifier rules)
"""
import os
import time
import logging
import httpx
from datetime import datetime, timezone

logger = logging.getLogger("creator_history")

HELIUS_RPC = os.environ.get("HELIUS_RPC_URL", "")
# Derive enhanced-API endpoint from RPC key
_API_KEY = ""
if "api-key=" in HELIUS_RPC:
    _API_KEY = HELIUS_RPC.split("api-key=", 1)[1].split("&")[0]
HELIUS_ENHANCED = f"https://api.helius.xyz/v0/addresses/{{addr}}/transactions"

PUMP_PROGRAM_ID = os.environ.get("PUMP_PROGRAM_ID", "")

# In-process small TTL cache to avoid re-backfilling within a session
_BACKFILL_CACHE: dict[str, tuple[float, dict]] = {}
_BACKFILL_TTL = 3600.0  # 1h


async def _helius_backfill(creator: str) -> dict:
    """Best-effort: count prior Pump.fun token creates by this wallet."""
    if not _API_KEY:
        return {"backfill_attempted": False}
    cached = _BACKFILL_CACHE.get(creator)
    if cached and time.time() - cached[0] < _BACKFILL_TTL:
        return cached[1]
    url = HELIUS_ENHANCED.format(addr=creator)
    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get(url, params={"api-key": _API_KEY, "limit": 50})
            if r.status_code != 200:
                logger.debug(f"helius backfill non-200 for {creator}: {r.status_code}")
                result = {"backfill_attempted": True, "backfill_ok": False}
                _BACKFILL_CACHE[creator] = (time.time(), result)
                return result
            txs = r.json() or []
    except Exception as e:
        logger.debug(f"helius backfill error {creator}: {e}")
        result = {"backfill_attempted": True, "backfill_ok": False}
        _BACKFILL_CACHE[creator] = (time.time(), result)
        return result

    pump_txs = 0
    creates = 0
    mints_seen: set[str] = set()
    for tx in txs:
        instructions = tx.get("instructions") or []
        for ix in instructions:
            pid = ix.get("programId")
            if pid == PUMP_PROGRAM_ID:
                pump_txs += 1
                # Crude: a "create" tx often has a unique mint account list early
                accs = ix.get("accounts") or []
                if accs:
                    mints_seen.add(accs[0])
        # Some enhanced-API entries have parsed `type` like "CREATE_RAYDIUM_POOL" or vendor labels
        t = (tx.get("type") or "").upper()
        if "CREATE" in t and "PUMP" in t:
            creates += 1

    result = {
        "backfill_attempted": True,
        "backfill_ok": True,
        "prior_pump_txs": pump_txs,
        "prior_distinct_mints": len(mints_seen),
        "prior_creates_estimate": max(creates, len(mints_seen)),
    }
    _BACKFILL_CACHE[creator] = (time.time(), result)
    return result


async def record_new_launch(db, creator: str, mint: str) -> dict:
    """
    Called on every new launch. Returns the current creator stats dict
    (after this launch is recorded).
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    existing = await db.creators.find_one({"_id": creator}, {"_id": 0})
    if existing is None:
        # First time seeing this creator — try Helius backfill
        backfill = await _helius_backfill(creator)
        base = {
            "_id": creator,
            "tokens_created": 0,
            "tokens_graduated": 0,
            "tokens_failed": 0,
            "tokens_active": 0,
            "recent_mints": [],
            "first_seen": now_iso,
            **backfill,
        }
        await db.creators.insert_one(base)
        existing = {k: v for k, v in base.items() if k != "_id"}

    await db.creators.update_one(
        {"_id": creator},
        {
            "$inc": {"tokens_created": 1, "tokens_active": 1},
            "$set": {"last_seen": now_iso},
            "$push": {"recent_mints": {"$each": [mint], "$slice": -10}},
        },
    )
    # Return the post-update view
    updated = await db.creators.find_one({"_id": creator}, {"_id": 0})
    return updated or existing


async def mark_outcome(db, creator: str, outcome: str):
    """outcome in {'graduated','failed'} — adjusts the counters."""
    inc = {"tokens_active": -1}
    if outcome == "graduated":
        inc["tokens_graduated"] = 1
    elif outcome == "failed":
        inc["tokens_failed"] = 1
    else:
        return
    await db.creators.update_one({"_id": creator}, {"$inc": inc})


def derive_rug_count(creator_doc: dict | None) -> int:
    if not creator_doc:
        return 0
    return int(creator_doc.get("tokens_failed", 0))


async def get_creator(db, creator: str) -> dict | None:
    return await db.creators.find_one({"_id": creator}, {"_id": 0})
