"""
helius_budget — running tally of Helius API consumption.

Why: Developer plan = 10M credits / 30 days. At our trade rate, naive bot
defaults could easily blow through that on a busy market day. We need a
live counter so the Doctor can warn well before the cap and (optionally)
throttle the scanner when burn rate trends over budget.

What we count (best-effort, conservative):
  - Every `solana_client.rpc_call` invocation: 1 credit
  - Each WSS message received on the listener / account-event-bus:
      counted as max(1, size_kb // 100 * 2) credits (Helius bills WS at
      2 credits per 0.1MB streamed; small notifications are 1 credit).
  - Each new WSS subscription open: 1 credit.

This is approximate — Helius's billing is the source of truth — but good
enough for budget-management decisions inside the bot.

Counters are kept in-memory (process-local, reset on restart) PLUS persisted
to a singleton Mongo doc every 60s so a restart doesn't lose ~all data.
The doc has a `start_ts` so the user can see budget burn since their billing
period started; we don't try to align with the Helius billing window
ourselves (the user can reset via /api/diagnostics/helius-budget/reset).
"""
from __future__ import annotations
import asyncio
import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger("helius_budget")

# Module-level counters (process lifetime). Persisted snapshot lives in Mongo.
_counts = {
    "rpc_calls": 0,
    "ws_messages": 0,
    "ws_bytes": 0,
    "ws_subscribes": 0,
    "estimated_credits": 0,
}
_persist_ts = 0.0
_db = None  # set by `attach_db(db)` at app startup
_period_start_iso = None


def attach_db(db):
    global _db
    _db = db


async def hydrate_from_mongo():
    """At startup, load any persisted counters so a backend restart doesn't
    zero the budget. Sets `_period_start_iso` from the persisted doc, or
    creates a new period if none exists."""
    global _period_start_iso
    if _db is None:
        return
    try:
        doc = await _db.helius_budget.find_one({"_id": "helius_budget"}, {"_id": 0})
        if doc:
            for k in ("rpc_calls", "ws_messages", "ws_bytes",
                      "ws_subscribes", "estimated_credits"):
                if k in doc:
                    _counts[k] = int(doc[k])
            _period_start_iso = doc.get("period_start_iso")
        if not _period_start_iso:
            _period_start_iso = datetime.now(timezone.utc).isoformat()
            await _persist_now()
    except Exception as e:
        logger.warning(f"helius_budget hydrate failed: {e}")


async def _persist_now():
    if _db is None:
        return
    try:
        await _db.helius_budget.update_one(
            {"_id": "helius_budget"},
            {"$set": {**_counts, "period_start_iso": _period_start_iso,
                      "last_updated": datetime.now(timezone.utc).isoformat()}},
            upsert=True,
        )
    except Exception as e:
        logger.debug(f"helius_budget persist failed: {e}")


def _maybe_persist_sync():
    """Throttled persist: only writes to Mongo every 60s."""
    global _persist_ts
    now = time.time()
    if now - _persist_ts < 60.0:
        return
    _persist_ts = now
    try:
        asyncio.create_task(_persist_now())
    except RuntimeError:
        pass  # no event loop yet (import-time)


def record_rpc_call():
    """Count a successful Helius JSON-RPC call (1 credit)."""
    _counts["rpc_calls"] += 1
    _counts["estimated_credits"] += 1
    _maybe_persist_sync()


def record_ws_message(byte_size: int):
    """Helius bills WS at 2 credits per 0.1MB = 0.00002 credits/byte.
    Accumulated fractionally; small messages contribute proportionally rather
    than getting floored to 1 credit each (which over-counts by ~100x for
    typical 500-byte notifications)."""
    _counts["ws_messages"] += 1
    _counts["ws_bytes"] += byte_size
    _counts["estimated_credits"] += byte_size / 50_000.0  # 2cr per 100KB
    _maybe_persist_sync()


def record_ws_subscribe():
    _counts["ws_subscribes"] += 1
    _counts["estimated_credits"] += 1
    _maybe_persist_sync()


def snapshot(monthly_limit: int = 10_000_000) -> dict:
    """Compute a UX-friendly view: credit consumption, projected month
    burn, and a warning/critical signal the Doctor can act on."""
    used = _counts["estimated_credits"]
    period_start_iso = _period_start_iso or datetime.now(timezone.utc).isoformat()
    try:
        start_ts = datetime.fromisoformat(period_start_iso.replace("Z", "+00:00")).timestamp()
    except Exception:
        start_ts = time.time()
    elapsed_s = max(1.0, time.time() - start_ts)
    # Warmup: don't project from less than 30 min of data — burn rate hasn't
    # had time to converge. Mark severity green during warmup so we don't
    # alarm the user with a meaningless extrapolation.
    if elapsed_s < 1800:
        severity = "green"
        projected_30d = None
        pct_of_limit = None
        warmup = True
    else:
        daily_rate = used / (elapsed_s / 86400.0)
        projected_30d = daily_rate * 30
        pct_of_limit = projected_30d / monthly_limit if monthly_limit > 0 else 0
        warmup = False
    daily_rate = used / (elapsed_s / 86400.0) if elapsed_s > 0 else 0
    pct_consumed = used / monthly_limit if monthly_limit > 0 else 0
    if not warmup:
        if pct_consumed > 0.9 or (pct_of_limit or 0) > 1.2:
            severity = "red"
        elif pct_consumed > 0.6 or (pct_of_limit or 0) > 1.0:
            severity = "yellow"
        else:
            severity = "green"
    return {
        "monthly_limit": monthly_limit,
        "period_start_iso": period_start_iso,
        "elapsed_hours": round(elapsed_s / 3600.0, 2),
        "warmup": warmup,
        "rpc_calls": _counts["rpc_calls"],
        "ws_messages": _counts["ws_messages"],
        "ws_bytes": _counts["ws_bytes"],
        "ws_subscribes": _counts["ws_subscribes"],
        "estimated_credits_used": round(used, 1),
        "estimated_daily_burn": int(daily_rate),
        "projected_30d_burn": int(projected_30d) if projected_30d is not None else None,
        "pct_of_monthly_consumed": round(pct_consumed * 100, 2),
        "pct_of_monthly_projected": round(pct_of_limit * 100, 1) if pct_of_limit is not None else None,
        "severity": severity,
    }


async def reset_period():
    """Reset counters to zero and start a new tracking window. Use when your
    Helius billing cycle resets, or after a one-time burst you want to
    exclude (e.g. backfilling creator histories)."""
    global _period_start_iso
    for k in _counts:
        _counts[k] = 0
    _period_start_iso = datetime.now(timezone.utc).isoformat()
    await _persist_now()
