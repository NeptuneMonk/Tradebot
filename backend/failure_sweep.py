"""
failure_sweep — deferred classification of launches that never graduated.

Why we need this:
The 60s in-tracker check ONLY marks "graduated". Per the rug-patterns spec
(memory/RUG_PATTERNS.md), the "Dead in 60s" cohort is the USELESS pattern
we don't want to greylist — chasing them is what got us into trouble. The
USEFUL cohort is "had volume, fizzled over hours/days, predictable peak MC"
— which can only be detected by looking BACK at launches that sat dormant.

What this does (cheap — Mongo-only after the initial 24h delay):
  - Every 6 hours, find launches with:
      first_seen older than 24h
      AND outcome is unset (i.e. they didn't graduate)
  - For each, classify based on what we observed during tracking:
      "failed_instant"  — total buys < 5 (this is the "dead in 60s" cohort;
                           we still STAMP it so the creator's tokens_failed
                           is accurate, but the greylist scorer ignores them
                           because peak_mc_usd will be tiny)
      "failed_fizzled"  — had ≥5 buys, never graduated, dormant now
                           THIS is the tradeable greylist cohort.
      "failed_chaotic"  — ≥5 buys but no consistent pattern; gets stamped
                           but the predictability component pushes the
                           creator's score down.
  - Stamp launches.outcome, .outcome_at, .final_peak_mc_usd, .fail_class
  - Refresh the creator's greylist score

Cost: zero Helius calls. Reads + writes only against Mongo.
"""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("failure_sweep")

SWEEP_INTERVAL_S = 6 * 3600  # 6 hours
SWEEP_AGE_THRESHOLD_HOURS = 24


def classify_failed_launch(launch: dict) -> str:
    """Mechanical classification per RUG_PATTERNS.md. Returns one of:
      'failed_instant' | 'failed_fizzled' | 'failed_chaotic'
    Used to filter what the greylist scorer should weight."""
    buys = int(launch.get("buy_count") or 0)
    unique_buyers = int(launch.get("unique_buyers") or 0)
    peak_mc = float(launch.get("peak_mc_usd") or 0)
    if buys < 5 and unique_buyers < 5:
        return "failed_instant"
    # Had volume — separate "fizzled gracefully" from "chaotic":
    #   fizzled = peak MC > $5k (meaningful pump pre-dormancy)
    #   chaotic = below threshold or otherwise low signal
    if peak_mc >= 5_000:
        return "failed_fizzled"
    return "failed_chaotic"


class FailureSweeper:
    """Background task. Started from lifespan."""

    def __init__(self, db):
        self.db = db
        self._task: asyncio.Task | None = None
        self._stop = False
        self.stats = {
            "sweeps_run": 0,
            "launches_classified": 0,
            "last_sweep_at": None,
        }

    def start(self):
        if self._task and not self._task.done():
            return
        self._stop = False
        self._task = asyncio.create_task(self._loop())

    def stop(self):
        self._stop = True
        if self._task:
            self._task.cancel()

    async def _loop(self):
        # Initial 5-min delay so we don't sweep during a noisy startup
        await asyncio.sleep(300)
        while not self._stop:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"failure_sweep loop error: {e}")
            await asyncio.sleep(SWEEP_INTERVAL_S)

    async def run_once(self) -> dict:
        cutoff = (datetime.now(timezone.utc) -
                  timedelta(hours=SWEEP_AGE_THRESHOLD_HOURS)).isoformat()
        # Find launches: older than 24h, outcome unset (i.e. didn't graduate
        # by 60s AND we never came back to stamp them).
        cur = self.db.launches.find(
            {
                "first_seen": {"$lt": cutoff},
                "outcome": {"$in": [None]},
            },
            {"_id": 1, "mint": 1, "creator": 1, "buy_count": 1,
             "unique_buyers": 1, "peak_mc_usd": 1, "first_seen": 1},
        ).limit(2000)

        classified = 0
        creators_touched: set[str] = set()
        now_iso = datetime.now(timezone.utc).isoformat()

        async for d in cur:
            fail_class = classify_failed_launch(d)
            await self.db.launches.update_one(
                {"_id": d["_id"]},
                {"$set": {
                    "outcome": "failed",
                    "outcome_at": now_iso,
                    "fail_class": fail_class,
                    # Stamp final_peak_mc_usd from whatever we observed.
                    # For tokens we stopped tracking before they fizzled,
                    # this is the peak we DID see (better than nothing).
                    "final_peak_mc_usd": float(d.get("peak_mc_usd") or 0.0),
                }},
            )
            # Decrement creators.tokens_active and increment tokens_failed
            # via the existing helper (kept consistent with the rest of the
            # codebase). Done one at a time so a partial failure doesn't
            # de-sync the counter.
            try:
                from creator_history import mark_outcome
                await mark_outcome(self.db, d.get("creator"), "failed")
            except Exception as e:
                logger.debug(f"mark_outcome failed for {d.get('mint','?')}: {e}")
            if d.get("creator"):
                creators_touched.add(d["creator"])
            classified += 1

        # Refresh greylist scores for every affected creator (one pass,
        # not per-launch — cheaper).
        for creator in creators_touched:
            try:
                from creator_greylist import update_creator_score
                await update_creator_score(self.db, creator)
            except Exception as e:
                logger.debug(f"greylist refresh failed for {creator[:8]}…: {e}")

        self.stats["sweeps_run"] += 1
        self.stats["launches_classified"] += classified
        self.stats["last_sweep_at"] = now_iso
        logger.info(
            f"failure_sweep: classified {classified} dormant launches across "
            f"{len(creators_touched)} creators"
        )
        return {
            "classified": classified,
            "creators_touched": len(creators_touched),
            "last_sweep_at": now_iso,
        }
