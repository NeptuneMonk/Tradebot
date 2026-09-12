"""Scorecard — the learning memory. Every closed trade lands in a situation cell:

    book × greylist_pattern_or_none × band(new|seasoned|n/a) × hour_utc_bucket(4h) × cost_bucket

Cells with n ≥ MIN_N and expectancy_r < 0 are DISABLED (new entries in that cell skip); cells with
expectancy_r ≥ UPWEIGHT_R are eligible for allocator upweight. Disabled cells re-open only after a timed
decay AND a minimum number of fresh paper samples — never because an exit key moved."""
from __future__ import annotations

import statistics
import time
from datetime import datetime, timezone

from book_params import pnl_r

MIN_N = 30
DISABLE_BELOW_R = -0.15      # a cell must be clearly negative, not breakeven noise
UPWEIGHT_R = 0.30
REOPEN_AFTER_H = 72
REOPEN_MIN_PAPER = 10
COL = "scorecard"


def cost_bucket(cost_pct) -> str:
    try:
        c = float(cost_pct)
    except (TypeError, ValueError):
        return "na"
    return "lo" if c < 3 else "mid" if c < 6 else "hi"


def hour_bucket(iso) -> str:
    try:
        h = datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(timezone.utc).hour
        return f"h{(h // 4) * 4:02d}"
    except Exception:
        return "hna"


def cell_key(*, book: str, pattern: str | None, band: str | None, entry_time, cost_pct) -> str:
    return "|".join([book, pattern or "none", band or "na", hour_bucket(entry_time), cost_bucket(cost_pct)])


def cell_for_trade(t: dict) -> str:
    return cell_key(book=t.get("book") or "scalp", pattern=t.get("greylist_pattern_at_entry"),
                    band=(t.get("entry_ctx") or {}).get("band"), entry_time=t.get("entry_time"), cost_pct=t.get("expected_cost_pct"))


def _book_view(t: dict) -> dict:
    """Promoted runners: the runner cell sees only the post-promotion leg — chips banked before promotion never mix in."""
    if t.get("book") == "runner" and t.get("runner_pnl_usd") is not None:
        return {**t, "pnl_usd": t["runner_pnl_usd"]}
    return t


def stats(trades: list[dict]) -> dict:
    trades = [_book_view(t) for t in trades if t.get("r_usd") and not t.get("misclose")]   # post-migration fills only; venue-change mis-closes never judge a cell
    rs = [r for r in (pnl_r(t) for t in trades) if r is not None]
    if not rs:
        return {"n": 0}
    wins, losses = [r for r in rs if r > 0], [r for r in rs if r <= 0]
    usd = [float(t.get("pnl_usd") or 0) for t in trades]
    mfe = [float(t.get("mfe_pct")) for t in trades if t.get("mfe_pct") is not None]
    sl = sum(1 for t in trades if "stop-loss" in str(t.get("exit_reason") or ""))
    return {"n": len(rs), "wr": len(wins) / len(rs), "avg_win_r": statistics.mean(wins) if wins else 0.0,
            "avg_loss_r": statistics.mean(losses) if losses else 0.0, "expectancy_r": statistics.mean(rs),
            "expectancy_usd": statistics.mean(usd), "sl_rate": sl / len(rs),
            "median_mfe": statistics.median(mfe) if mfe else None,
            "post_exit_run_pct": statistics.mean(float(t["post_exit_run_pct"]) for t in trades if t.get("post_exit_run_pct") is not None)
            if any(t.get("post_exit_run_pct") is not None for t in trades) else None}


class Scorecard:
    def __init__(self, db):
        self.db = db
        self._disabled: dict[str, dict] = {}
        self._loaded = False

    async def load(self):
        self._disabled = {d["_id"]: d async for d in self.db[COL].find({"disabled": True})}
        self._loaded = True

    def is_disabled(self, key: str) -> bool:
        return key in self._disabled

    async def record(self, t: dict) -> str:
        """Roll a closed trade into its cell (windowed recompute over the cell's last 200 fills)."""
        key = cell_for_trade(t)
        await self.db.trades.update_one({"_id": t.get("id")}, {"$set": {"scorecard_cell": key}})
        rows = [d async for d in self.db.trades.find({"scorecard_cell": key, "status": "closed"}).sort("exit_time", -1).limit(200)]
        st = stats(rows)
        prev = await self.db[COL].find_one({"_id": key}) or {}
        doc = {**st, "updated_at": time.time(), "disabled": bool(prev.get("disabled")),
               "disabled_at": prev.get("disabled_at"), "paper_since_disable": prev.get("paper_since_disable", 0),
               "upweight_eligible": st.get("n", 0) >= MIN_N and (st.get("expectancy_r") or 0) >= UPWEIGHT_R}
        if not doc["disabled"] and st.get("n", 0) >= MIN_N and st["expectancy_r"] <= DISABLE_BELOW_R:
            doc.update(disabled=True, disabled_at=time.time(), paper_since_disable=0)
        elif doc["disabled"]:
            if t.get("mode") == "paper":
                doc["paper_since_disable"] = int(doc["paper_since_disable"]) + 1
            aged = time.time() - float(doc.get("disabled_at") or 0) >= REOPEN_AFTER_H * 3600
            if aged and doc["paper_since_disable"] >= REOPEN_MIN_PAPER and st.get("expectancy_r", -1) >= 0:
                doc.update(disabled=False, disabled_at=None, paper_since_disable=0, reopened_at=time.time())
        await self.db[COL].update_one({"_id": key}, {"$set": doc}, upsert=True)
        if doc["disabled"]:
            self._disabled[key] = doc
        else:
            self._disabled.pop(key, None)
        return key

    async def set_disabled(self, key: str, disabled: bool):
        await self.db[COL].update_one({"_id": key}, {"$set": {"disabled": disabled, "disabled_at": time.time() if disabled else None,
                                                             "paper_since_disable": 0, "updated_at": time.time()}}, upsert=True)
        if disabled:
            self._disabled[key] = {"disabled": True}
        else:
            self._disabled.pop(key, None)

    async def snapshot(self, limit: int = 200) -> list[dict]:
        rows = [d async for d in self.db[COL].find({}).sort("n", -1).limit(limit)]
        return [{"cell": d.pop("_id"), **d} for d in rows]
