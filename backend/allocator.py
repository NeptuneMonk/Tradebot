"""Desk allocator — continuous, reversible capital weights across books, from rolling net-of-cost expectancy.

Replaces the binary "disable the losing machine": every book keeps an EXPLORATION FLOOR (never 0) so it
keeps generating fills and can earn its way back; books that pay get scaled up (capped). Weights move at
most one STEP per Doctor cycle. Runs only under Autopilot.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger("allocator")

BOOK_KEYS = {"momentum": "book_momentum_size_mult", "greylist_snipe": "book_snipe_size_mult",
             "reentry": "reentry_size_multiplier", "rh_pons": "book_rh_size_mult"}
FLOOR, CAP, STEP = 0.25, 2.0, 0.25
TARGET_EXPECTANCY_USD = 0.50      # $/fill that earns the full ×2 scale-up


def _blend(st24: dict, st7: dict) -> tuple[float | None, int]:
    """Expectancy blended by sample: 24h counts double when it has a real sample, 7d anchors it."""
    e24, n24 = st24.get("expectancy_usd"), int(st24.get("n") or 0)
    e7, n7 = st7.get("expectancy_usd"), int(st7.get("n") or 0)
    if e24 is None and e7 is None:
        return None, 0
    w24, w7 = 2 * n24, n7
    if w24 + w7 == 0:
        return None, 0
    return ((e24 or 0) * w24 + (e7 or 0) * w7) / (w24 + w7), n24 + n7


def target_weight(expectancy: float | None, n: int, current: float, min_n: int) -> tuple[float, str]:
    """Desired multiplier and why. Thin sample → drift toward 1.0 but never below the floor."""
    cur = max(FLOOR, float(current or 0.0))
    if expectancy is None or n < min_n:
        return max(FLOOR, cur), f"n={n} < {min_n}: not enough fills to judge — keep exploring"
    if expectancy <= 0:
        return FLOOR, f"{expectancy:+.3f} $/fill over {n} fills — cut to the exploration floor ×{FLOOR:g} (never off)"
    w = min(CAP, 1.0 + min(1.0, expectancy / TARGET_EXPECTANCY_USD))
    return w, f"{expectancy:+.3f} $/fill over {n} fills — scale to ×{w:.2f}"


def plan(cfg: dict, books24: dict, books7: dict, min_n: int, enabled_books: dict[str, bool]) -> list[dict]:
    """Per book: current, target, next (one step), reason. Books that are switched off by the user are skipped."""
    out = []
    for book, key in BOOK_KEYS.items():
        if not enabled_books.get(book, True):
            continue
        cur = float(cfg.get(key) if cfg.get(key) is not None else 1.0)
        e, n = _blend(books24.get(book) or {}, books7.get(book) or {})
        tgt, why = target_weight(e, n, cur, min_n)
        nxt = cur + max(-STEP, min(STEP, tgt - cur))
        nxt = round(max(FLOOR, min(CAP, nxt)), 2)
        out.append({"book": book, "key": key, "current": round(cur, 2), "target": round(tgt, 2), "next": nxt,
                    "expectancy_usd": None if e is None else round(e, 4), "n": n, "reason": why,
                    "change": abs(nxt - cur) >= 0.01})
    return out


async def apply(db, rows: list[dict], reload_cb=None) -> list[dict]:
    """Write the changed weights to bot_config and log them as applied 'allocator' suggestions."""
    changes = [r for r in rows if r["change"]]
    if not changes:
        return []
    await db.bot_config.update_one({}, {"$set": {r["key"]: r["next"] for r in changes}})
    now = datetime.now(timezone.utc).isoformat()
    docs = [{"category": "allocator", "title": f"[{r['book']}] {r['key']} {r['current']:g} → {r['next']:g}", "rationale": r["reason"],
             "actions": {r["key"]: r["next"]}, "status": "applied", "applied_at": now, "auto_applied": True,
             "metrics": {"book": r["book"], "expectancy_usd": r["expectancy_usd"], "n": r["n"], "target": r["target"]}} for r in changes]
    try:
        await db.strategy_suggestions.insert_many(docs)
    except Exception as e:
        logger.debug(f"allocator log failed: {e}")
    for r in changes:
        logger.warning(f"allocator [{r['book']}] {r['key']} {r['current']:g} → {r['next']:g}: {r['reason']}")
    if reload_cb:
        await reload_cb()
    return changes
