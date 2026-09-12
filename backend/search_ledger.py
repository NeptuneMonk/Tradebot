"""Search vs harvest ledger — DISPLAY ONLY (no gate yet).
harvest = runner closes + banked promotion proceeds; search = scalp/hunt/rh_pons closes that never became runners.
remaining = seed + search_budget_pct * max(harvest, 0) - |search losses|. Recomputed on every close."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

SEARCH_BOOKS = ("scalp", "hunt", "rh_pons")
SEED_USD_PER_DAY = 10.0
WINDOW_DAYS = 7


def compute(rows: list[dict], *, search_budget_pct: float = 0.40, window_days: int = WINDOW_DAYS) -> dict:
    """Pure math over closed trade docs inside the window (any chain)."""
    harvest = search = 0.0
    search_losses = 0.0
    runners = 0
    n_search = n_harvest = 0
    for t in rows:
        pnl = float(t.get("pnl_usd") or 0.0)
        if t.get("book") == "runner":
            harvest += pnl + float(t.get("promotion_banked_usd") or 0.0)
            runners += 1
            n_harvest += 1
        elif t.get("book") in SEARCH_BOOKS:
            search += pnl
            n_search += 1
            if pnl < 0:
                search_losses += -pnl
    seed = SEED_USD_PER_DAY * window_days
    remaining = seed + search_budget_pct * max(harvest, 0.0) - search_losses
    return {
        "window_days": window_days, "harvest_realised_usd": round(harvest, 4), "search_realised_usd": round(search, 4),
        "search_losses_usd": round(search_losses, 4), "seed_usd": seed, "search_budget_pct": search_budget_pct,
        "remaining_usd": round(remaining, 4), "runners_promoted": runners, "search_fills": n_search, "harvest_fills": n_harvest,
        "cost_per_runner_usd": round(search_losses / runners, 4) if runners else None,
        "search_r_per_fill": round(search / n_search, 4) if n_search else None,
        "harvest_r_per_fill": round(harvest / n_harvest, 4) if n_harvest else None,
    }


async def refresh(db, cfg) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).isoformat()
    rows = await db.trades.find({"status": "closed", "exit_time": {"$gte": since}},
                                {"_id": 0, "book": 1, "pnl_usd": 1, "promotion_banked_usd": 1}).to_list(length=20000)
    led = compute(rows, search_budget_pct=float(getattr(cfg, "search_budget_pct", 0.40) or 0.40))
    led["updated_at"] = datetime.now(timezone.utc).isoformat()
    await db.search_ledger.update_one({"_id": "current"}, {"$set": led}, upsert=True)
    logger.info(f"search ledger: remaining ${led['remaining_usd']:.2f} | harvest ${led['harvest_realised_usd']:.2f} | "
                f"search ${led['search_realised_usd']:.2f} | cost/runner {led['cost_per_runner_usd']}")
    return led
