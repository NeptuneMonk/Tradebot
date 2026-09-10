"""Desk allocator — continuous, reversible capital weights per book from rolling expectancy in R.

Every book keeps an exploration FLOOR (never 0); a book earns the full ×2 when blended expectancy_r ≥
TARGET_EXPECTANCY_R with enough fills. One STEP per Doctor cycle, rails-clamped. Runs only under Autopilot."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from book_params import SIZE_KEYS

logger = logging.getLogger("allocator")

BOOK_KEYS = dict(SIZE_KEYS)
FLOOR, CAP, STEP = 0.25, 2.0, 0.25
RUNNER_MIN_N = 20               # runner is judged only on its own fills, never before 20 of them
TARGET_EXPECTANCY_R = 0.30      # R/fill that earns the full ×2 scale-up (stake-relative, never $)


def _blend(st24: dict, st7: dict) -> tuple[float | None, int]:
    e24, n24 = st24.get("expectancy_r"), int(st24.get("n") or 0)
    e7, n7 = st7.get("expectancy_r"), int(st7.get("n") or 0)
    if e24 is None and e7 is None:
        return None, 0
    w24, w7 = 2 * n24, n7
    if w24 + w7 == 0:
        return None, 0
    return ((e24 or 0) * w24 + (e7 or 0) * w7) / (w24 + w7), n24 + n7


def target_weight(expectancy_r: float | None, n: int, current: float, min_n: int) -> tuple[float, str]:
    cur = max(FLOOR, float(current or 0.0))
    if expectancy_r is None or n < min_n:
        return max(FLOOR, cur), f"n={n} < {min_n}: not enough fills to judge — keep exploring"
    if expectancy_r <= 0:
        return FLOOR, f"{expectancy_r:+.2f}R/fill over {n} fills — cut to the exploration floor ×{FLOOR:g} (never off)"
    w = min(CAP, 1.0 + min(1.0, expectancy_r / TARGET_EXPECTANCY_R))
    return w, f"{expectancy_r:+.2f}R/fill over {n} fills — scale to ×{w:.2f}"


def plan(cfg: dict, books24: dict, books7: dict, min_n: int, enabled_books: dict[str, bool]) -> list[dict]:
    out = []
    for book, key in BOOK_KEYS.items():
        if not enabled_books.get(book, True):
            continue
        cur = float(cfg.get(key) if cfg.get(key) is not None else 1.0)
        e, n = _blend(books24.get(book) or {}, books7.get(book) or {})
        tgt, why = target_weight(e, n, cur, max(min_n, RUNNER_MIN_N) if book == "runner" else min_n)
        nxt = round(max(FLOOR, min(CAP, cur + max(-STEP, min(STEP, tgt - cur)))), 2)
        out.append({"book": book, "key": key, "current": round(cur, 2), "target": round(tgt, 2), "next": nxt,
                    "expectancy_r": None if e is None else round(e, 3), "n": n, "reason": why, "change": abs(nxt - cur) >= 0.01})
    return out


async def apply(db, rows: list[dict], reload_cb=None) -> list[dict]:
    changes = [r for r in rows if r["change"]]
    if not changes:
        return []
    from rails import clamp_actions
    clamped, _ = clamp_actions({r["key"]: r["next"] for r in changes}, "allocator")
    changes = [{**r, "next": clamped[r["key"]]} for r in changes if r["key"] in clamped]
    await db.bot_config.update_one({}, {"$set": {r["key"]: r["next"] for r in changes}})
    now = datetime.now(timezone.utc).isoformat()
    docs = [{"category": "allocator", "title": f"[{r['book']}] {r['key']} {r['current']:g} → {r['next']:g}", "rationale": r["reason"],
             "actions": {r["key"]: r["next"]}, "status": "applied", "applied_at": now, "auto_applied": True,
             "metrics": {"book": r["book"], "expectancy_r": r["expectancy_r"], "n": r["n"], "target": r["target"]}} for r in changes]
    try:
        await db.strategy_suggestions.insert_many(docs)
    except Exception as e:
        logger.debug(f"allocator log failed: {e}")
    for r in changes:
        logger.warning(f"allocator [{r['book']}] {r['key']} {r['current']:g} → {r['next']:g}: {r['reason']}")
    if reload_cb:
        await reload_cb()
    return changes
