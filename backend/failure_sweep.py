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

    Primary signal is `sol_inflow` — a launch that died with < 2 SOL of
    total buy-side inflow never reached tradeable volume regardless of how
    many micro-buys hit it. This eliminates the long tail of noise mints
    that would otherwise dilute the creator's peak-MC median.
    """
    sol_inflow = float(launch.get("sol_inflow") or 0)
    buys = int(launch.get("buy_count") or 0)
    unique_buyers = int(launch.get("unique_buyers") or 0)
    peak_mc = float(launch.get("peak_mc_usd") or 0)
    if peak_mc <= 0:
        peak_mc = _estimate_peak_mc(launch)
    # Dead-on-arrival — < 2 SOL inflow OR < 5 buys+buyers (handles edge
    # cases where inflow is unset).
    if sol_inflow < 2.0 or (buys < 5 and unique_buyers < 5):
        return "failed_instant"
    # Had volume — separate "fizzled gracefully" from "chaotic":
    #   fizzled = peak MC ≥ $5k (meaningful pump pre-dormancy)
    #   chaotic = below threshold (volume but never sustained)
    if peak_mc >= 5_000:
        return "failed_fizzled"
    return "failed_chaotic"


# Pump.fun bonding curve graduates at 100% fill which corresponds to ~$69k
# market cap. The mapping is roughly linear (slight curve, but linear is a
# good approximation for our greylist purposes — we need a ROUGH ceiling,
# not a precise valuation).
_PUMPFUN_GRADUATION_MC_USD = 69_000.0


def _estimate_peak_mc(launch: dict) -> float:
    """Derive an approximate peak MC for a launch when `peak_mc_usd` was
    never populated by the scanner. Uses `curve_fill_pct` primarily,
    falling back to a rough SOL-inflow heuristic."""
    fill = launch.get("curve_fill_pct")
    if fill is not None and fill > 0:
        return float(fill) / 100.0 * _PUMPFUN_GRADUATION_MC_USD
    inflow = launch.get("sol_inflow")
    if inflow is not None and inflow > 0:
        # ~32 SOL accumulates around $25k MC on early curve. Rough.
        return float(inflow) * 800.0
    return 0.0


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
        # Uses `detected_at` (the actual launch timestamp field) — earlier
        # versions of this file queried `first_seen` which doesn't exist on
        # launches docs, silently making the sweep a no-op. The
        # `$or` lets older docs that DID have first_seen still match.
        cur = self.db.launches.find(
            {
                "$or": [
                    {"detected_at": {"$lt": cutoff}},
                    {"first_seen": {"$lt": cutoff}},
                ],
                "outcome": {"$in": [None]},
            },
            {"_id": 1, "mint": 1, "creator": 1, "buy_count": 1,
             "unique_buyers": 1, "peak_mc_usd": 1,
             "curve_fill_pct": 1, "sol_inflow": 1,
             "detected_at": 1, "first_seen": 1},
        ).limit(2000)

        classified = 0
        creators_touched: set[str] = set()
        now_iso = datetime.now(timezone.utc).isoformat()

        async for d in cur:
            fail_class = classify_failed_launch(d)
            # Stamp final_peak_mc_usd from observed peak OR derive from
            # curve_fill_pct (pump.fun 100% fill ≈ $69k MC). Necessary for
            # the greylist's median-MC pattern signal — without this every
            # historical failed launch shows up as $0 peak.
            peak_mc = float(d.get("peak_mc_usd") or 0)
            if peak_mc <= 0:
                peak_mc = _estimate_peak_mc(d)
            # Derive Bing-reference behavioral signatures (accel_class /
            # flow_class / rug_speed_class). Computed inline so the stamped
            # launch doc carries the per-launch signature that
            # `aggregate_signatures()` reads when building the creator's
            # repeatability score — without forcing the classifier to
            # recompute them from raw fields every score update.
            from launch_signatures import derive_signatures
            launch_for_sig = dict(d)
            launch_for_sig["outcome"] = "failed"
            launch_for_sig["outcome_at"] = now_iso
            sig_fields = derive_signatures(launch_for_sig)
            await self.db.launches.update_one(
                {"_id": d["_id"]},
                {"$set": {
                    "outcome": "failed",
                    "outcome_at": now_iso,
                    "fail_class": fail_class,
                    "final_peak_mc_usd": peak_mc,
                    **sig_fields,
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
        # not per-launch — cheaper). Honor the live BotConfig F-band so a
        # sweep-touched creator outside [min_fails, max_fails) gets stats
        # persisted but composite score zeroed.
        try:
            cfg_doc = await self.db.bot_config.find_one({}, {"_id": 0}) or {}
            min_f = int(cfg_doc.get("creator_greylist_min_fails", 5))
            max_f = int(cfg_doc.get("creator_greylist_max_fails", 80))
            tp_buf = float(cfg_doc.get("pattern_tp_buffer_pct", 2.0))
        except Exception:
            min_f, max_f, tp_buf = 5, 80, 2.0
        for creator in creators_touched:
            try:
                from creator_greylist import update_creator_score
                await update_creator_score(self.db, creator,
                                            min_fails=min_f, max_fails=max_f,
                                            tp_buffer=tp_buf)
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
