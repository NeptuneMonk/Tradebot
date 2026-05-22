"""
Per-source P/L analytics.

Classifies each trade into one of three buckets based on `classifier_action`:
  - "scanner"  : entered by the 4h momentum scanner ("scanner_momentum")
  - "reentry"  : re-entered after a profitable exit ("reentry")
  - "sniper"   : everything else (fresh mempool launches)
"""
from datetime import datetime, timezone, timedelta
from typing import Iterable


SOURCE_LABELS = {
    "sniper": "Launch Sniper",
    "scanner": "Momentum Scanner",
    "reentry": "Winner Re-entry",
}


def classify_source(classifier_action: str | None) -> str:
    if classifier_action == "scanner_momentum":
        return "scanner"
    if classifier_action == "reentry":
        return "reentry"
    return "sniper"


def _empty_bucket(source: str) -> dict:
    return {
        "source": source,
        "label": SOURCE_LABELS.get(source, "All Sources"),
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "win_rate_pct": 0.0,
        "pnl_usd": 0.0,
        "pnl_sol": 0.0,
        "avg_pnl_pct": 0.0,
        "best_pct": 0.0,
        "worst_pct": 0.0,
    }


def _finalize(b: dict) -> dict:
    if b["trades"] > 0:
        b["win_rate_pct"] = b["wins"] / b["trades"] * 100
        b["avg_pnl_pct"] = b["_sum_pct"] / b["trades"]
    b.pop("_sum_pct", None)
    return b


async def compute_pl_by_source(db, days: int = 7) -> dict:
    """Aggregate closed trades over the last `days` and split by entry source."""
    start = datetime.now(timezone.utc) - timedelta(days=days)
    buckets = {s: {**_empty_bucket(s), "_sum_pct": 0.0} for s in SOURCE_LABELS.keys()}
    total = {**_empty_bucket("total"), "_sum_pct": 0.0}
    total["label"] = "All Sources"

    cursor = db.trades.find(
        {"status": "closed", "exit_time": {"$gte": start.isoformat()}},
        {
            "_id": 0,
            "classifier_action": 1,
            "pnl_usd": 1,
            "pnl_sol": 1,
            "pnl_pct": 1,
            "mode": 1,
        },
    )
    async for d in cursor:
        src = classify_source(d.get("classifier_action"))
        b = buckets[src]
        pnl_usd = float(d.get("pnl_usd", 0.0))
        pnl_sol = float(d.get("pnl_sol", 0.0))
        pnl_pct = float(d.get("pnl_pct", 0.0))
        for tgt in (b, total):
            tgt["trades"] += 1
            tgt["pnl_usd"] += pnl_usd
            tgt["pnl_sol"] += pnl_sol
            tgt["_sum_pct"] += pnl_pct
            if pnl_usd > 0:
                tgt["wins"] += 1
            elif pnl_usd < 0:
                tgt["losses"] += 1
            tgt["best_pct"] = max(tgt["best_pct"], pnl_pct) if tgt["trades"] > 1 else pnl_pct
            tgt["worst_pct"] = min(tgt["worst_pct"], pnl_pct) if tgt["trades"] > 1 else pnl_pct

    return {
        "days": days,
        "sources": [_finalize(buckets[s]) for s in ("sniper", "scanner", "reentry")],
        "total": _finalize(total),
    }
