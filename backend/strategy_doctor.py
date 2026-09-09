"""Strategy Doctor — autonomous analyst that watches the bot's actual trade
history and surfaces concrete, one-click-implementable config changes.

The Doctor:
- Runs every `interval_minutes` minutes in the background (also forceable via
  POST /api/doctor/run-now).
- Produces *suggestions* not changes. The user reviews each suggestion and
  hits Apply (auto-merges `actions` into bot_config) or Dismiss (hidden for
  the cooldown window).
- Only suggests changes that map to EXISTING config keys with code support —
  no "wishful thinking" suggestions.
- Tells the user when sample size is too low rather than make low-confidence
  calls. The "needs_more_data" suggestion category is informational only.

Persistence model:
- `strategy_suggestions` collection, schema:
  {
    id: str (uuid),
    category: str (sizing | sl | tp | partial | hold | gate | scanner |
                   classifier | timing | needs_more_data),
    title: str (short headline),
    rationale: str (multi-line explainer + stats),
    actions: dict[str, Any] (bot_config keys → new values; empty for info)
    confidence: str (high | med | low),
    metrics: dict (raw numbers backing the suggestion — for the UI tooltip),
    status: str (pending | applied | dismissed),
    created_at: ISO timestamp,
    applied_at: ISO timestamp | None,
    dismissed_at: ISO timestamp | None,
    expires_at: ISO timestamp (auto-dismissed after this if untouched),
  }
"""
from __future__ import annotations

import asyncio
import time
import hashlib
import logging
import math
import os
import statistics
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from doctor_learning import LearningEngine

logger = logging.getLogger("strategy_doctor")

# ---------- Tunables ----------
# How long an analysis window covers
ANALYSIS_LOOKBACK_HOURS = 24
# How long a dismissed suggestion stays hidden before being re-evaluated
DISMISS_COOLDOWN_HOURS = 24
# Suggestions auto-expire after this if neither applied nor dismissed
SUGGESTION_TTL_HOURS = 72
# Default loop interval
DEFAULT_INTERVAL_MINUTES = 30


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mfe_pct(t: dict) -> float | None:
    """Max favourable excursion: peak price vs entry, either chain."""
    if t.get("chain") == "rh":
        ep, pk = t.get("entry_price_quote") or 0, t.get("peak_price_quote") or 0
    else:
        ep, pk = t.get("entry_price_sol") or 0, t.get("peak_price_sol") or 0
    if not ep or not pk:
        return None
    return (pk / ep - 1.0) * 100.0


def _exit_class(t: dict) -> str:
    """Normalise SOL ("stop-loss hit (-14%)") and RH ("stop_loss") vocabularies."""
    r = (t.get("exit_reason") or "").lower()
    if "no-momentum" in r or r == "no_momentum":
        return "no_momentum"
    if "stop-loss" in r or r == "stop_loss":
        return "sl"
    if "take-profit" in r or "partial-tp" in r or r == "take_profit":
        return "tp"
    if "trailing" in r:
        return "trail"
    if "timeout" in r or r == "max_hold" or "clock" in r:
        return "timeout"
    if "ladder" in r or "target" in r:
        return "ladder"
    return "other"


def _hold_s(t: dict) -> float | None:
    try:
        e = datetime.fromisoformat(str(t["entry_time"]).replace("Z", "+00:00"))
        x = datetime.fromisoformat(str(t["exit_time"]).replace("Z", "+00:00"))
        return (x - e).total_seconds()
    except Exception:
        return None


def _scope_label(scope: str) -> str:
    return "RH · PONS (paper)" if scope == "rh" else "Solana"


def _hash_signature(category: str, action_keys: list[str], action_values: dict | None = None) -> str:
    """Stable signature for a (category, action-set) combo. When action_values
    is supplied, includes the proposed VALUES so the doctor doesn't re-suggest
    a fix that's already in force (e.g. "set TP=8" when TP is already 8 — the
    rule keeps detecting a frequent-TP pattern from old trades and re-firing).
    Backward-compatible: older code paths that pass only keys get a value-less
    signature."""
    raw = f"{category}|{','.join(sorted(action_keys))}"
    if action_values:
        vals = ",".join(f"{k}={action_values[k]}" for k in sorted(action_values))
        raw = f"{raw}|{vals}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


class StrategyDoctor:
    def __init__(self, db, hub=None):
        self.db = db
        self.hub = hub  # broadcast new suggestions over WS if available
        self.reload_cb = None  # set by server: bot_state.load (re-entrant)
        self.learning = LearningEngine(db, hub=hub)
        self._task: asyncio.Task | None = None
        self.interval_minutes = DEFAULT_INTERVAL_MINUTES

    # ---------- public lifecycle ----------
    async def start(self, interval_minutes: int = DEFAULT_INTERVAL_MINUTES):
        self.interval_minutes = max(5, int(interval_minutes))
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="strategy_doctor")

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self):
        # Initial delay so we don't analyse on bare-fresh DB at startup
        await asyncio.sleep(60)
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception(f"doctor cycle failed: {e}")
            await asyncio.sleep(self.interval_minutes * 60)

    # ---------- analysis ----------
    async def run_once(self) -> list[dict]:
        """Run all rules, persist new suggestions, expire stale ones.
        Returns the suggestions that were freshly inserted this cycle."""
        self.last_run_ts = time.time()
        # Expire untouched suggestions older than TTL
        await self._expire_stale()

        cfg_doc = await self.db.bot_config.find_one({}, {"_id": 0}) or {}
        trades = await self._fetch_recent_trades()

        # LEARNING LOOP runs first (doctor_learning.py): book scoring, one
        # structural proposal max, canary promote/revert. If it fired (or a
        # canary is running) the legacy rules stay quiet this cycle.
        learning_out: list[dict] = []
        learning_busy = False
        try:
            trades_7d = await self._fetch_recent_trades(hours=24 * 7)
            learning_out = await self.learning.cycle(cfg_doc, trades, trades_7d)
            can = await self.learning.canary()
            learning_busy = bool(can and can.get("state") == "running")
        except Exception as e:
            logger.exception(f"learning engine failed: {e}")

        existing_pending = await self._existing_pending_signatures()
        existing_dismissed = await self._existing_recently_dismissed_signatures()
        # NEW: also dedup against recently-applied suggestions whose actions
        # are still in force in bot_config. Without this, a rule that detects
        # "frequent TP hits" keeps re-suggesting `take_profit_pct=8` every
        # cycle for the next 24h even after you applied it, because the OLD
        # trades that triggered the rule are still in the lookback window.
        existing_applied = await self._existing_applied_active_signatures(cfg_doc)
        skip = existing_pending | existing_dismissed | existing_applied

        suggestions: list[dict] = []
        suggestions.extend(learning_out)

        # The learning loop (book-scoped, next-window, R) is the ONLY proposer. Legacy 24h-WR knob rules are gone.

        # Deduplicate against pending + dismissed signatures
        fresh = []
        for s in suggestions:
            # Value-aware signature so "set X=N" doesn't dedup against
            # "set X=M" — different proposals deserve separate evaluation.
            sig = _hash_signature(
                s["category"],
                list((s.get("actions") or {}).keys()),
                s.get("actions") or {},
            )
            s["signature"] = sig
            if sig in skip:
                continue
            s["id"] = str(uuid.uuid4())
            s["status"] = "pending"
            s["created_at"] = _now_iso()
            s["applied_at"] = None
            s["dismissed_at"] = None
            s["expires_at"] = (datetime.now(timezone.utc) + timedelta(hours=SUGGESTION_TTL_HOURS)).isoformat()
            fresh.append(s)

        if fresh:
            await self.db.strategy_suggestions.insert_many([dict(s) for s in fresh])
            logger.info(f"doctor inserted {len(fresh)} new suggestion(s)")
            if self.hub:
                try:
                    # Strip _id before broadcasting (insert_many mutates dicts)
                    for s in fresh:
                        s.pop("_id", None)
                    await self.hub.broadcast("doctor_new_suggestions", {"count": len(fresh)})
                except Exception:
                    pass
        return fresh

    async def _fetch_recent_trades(self, hours: float = ANALYSIS_LOOKBACK_HOURS) -> list[dict]:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        # Include both modes — paper data is honest for strategy analysis;
        # live data is what we ultimately care about. Strategy logic is
        # mode-independent (only execution differs).
        cur = self.db.trades.find({
            "status": "closed",
            "exit_time": {"$gte": since},
            "ghost_entry": {"$ne": True},
        }, {"_id": 0})
        return await cur.to_list(5000)

    async def _existing_pending_signatures(self) -> set[str]:
        cur = self.db.strategy_suggestions.find(
            {"status": "pending"}, {"signature": 1, "_id": 0},
        )
        return {d["signature"] async for d in cur if d.get("signature")}

    async def _existing_recently_dismissed_signatures(self) -> set[str]:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=DISMISS_COOLDOWN_HOURS)).isoformat()
        cur = self.db.strategy_suggestions.find({
            "status": "dismissed",
            "dismissed_at": {"$gte": cutoff},
        }, {"signature": 1, "_id": 0})
        return {d["signature"] async for d in cur if d.get("signature")}

    async def _existing_applied_active_signatures(self, cfg_doc: dict) -> set[str]:
        """Dedup against APPLIED suggestions whose action values still match
        the current bot_config. Without this, after a rule like "set
        take_profit_pct=8" is applied, the SAME rule keeps re-suggesting it
        next cycle until the lookback rolls past — annoying user noise.

        We consider an applied suggestion "still active" when EVERY key in
        its `actions` dict still equals the current value in bot_config.
        If the user later changed any of those keys manually, the suggestion
        is no longer in effect and we should let the rule re-fire."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=DISMISS_COOLDOWN_HOURS)).isoformat()
        cur = self.db.strategy_suggestions.find({
            "status": "applied",
            "applied_at": {"$gte": cutoff},
        }, {"signature": 1, "actions": 1, "_id": 0})
        active: set[str] = set()
        async for d in cur:
            sig = d.get("signature")
            actions = d.get("actions") or {}
            if not sig or not actions:
                continue
            # Are all action values STILL in force in bot_config?
            still_in_force = all(
                cfg_doc.get(k) == v for k, v in actions.items()
            )
            if still_in_force:
                active.add(sig)
        return active

    async def _expire_stale(self):
        now = _now_iso()
        await self.db.strategy_suggestions.update_many(
            {"status": "pending", "expires_at": {"$lt": now}},
            {"$set": {"status": "expired"}},
        )

    # ============== RULES ==============
    # Each rule receives (trades, cfg_doc) and returns 0+ suggestions.
    # Each suggestion MUST include: category, title, rationale, actions,
    # confidence, metrics. `actions` is a dict of bot_config keys → new values.

def get_doctor() -> StrategyDoctor | None:
    return _doctor


def set_doctor(d: StrategyDoctor) -> None:
    global _doctor
    _doctor = d
