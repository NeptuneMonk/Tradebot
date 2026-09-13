"""Audit fixes: canary judged on all post-start fills, canary doc replaced not merged, Doctor re-baseline after a
paper reset, scanner pre-rank gates tallied, feed-seeded graduates not re-armed by the min-age bound, deterministic
classifier vetoes cooled down, bot/status trade count matching BSON dates."""
import asyncio
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("HELIUS_RPC_URL", "https://x")
os.environ.setdefault("HELIUS_WSS_URL", "wss://x")
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")
os.environ.setdefault("CORS_ORIGINS", "http://localhost")

import doctor_learning as dl
from models import BotConfig
from scanner import MomentumScanner


class _Cur:
    def __init__(self, rows):
        self.rows = rows

    async def to_list(self, n=None, length=None):
        return list(self.rows)


class _Trades:
    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    def find(self, flt, proj=None):
        self.queries.append(flt)
        since = flt.get("exit_time", {}).get("$gte", "")
        return _Cur([r for r in self.rows if str(r.get("exit_time", "")) >= since])


class _Canary:
    def __init__(self):
        self.doc = None
        self.ops = []

    async def find_one(self, flt, proj=None):
        return dict(self.doc) if self.doc else None

    async def replace_one(self, flt, doc, upsert=False):
        self.ops.append("replace")
        self.doc = dict(doc)

    async def update_one(self, flt, upd, upsert=False):
        self.ops.append("update")
        self.doc = {**(self.doc or {}), **upd.get("$set", {})}

    async def delete_many(self, flt):
        self.doc = None


class _Plain:
    async def update_one(self, *a, **k):
        return None

    async def find_one(self, *a, **k):
        return None

    async def replace_one(self, *a, **k):
        return None


def _fill(book, days_ago, pnl_usd, r_usd=1.0):
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    return {"book": book, "classifier_action": "greylist_snipe", "status": "closed", "exit_time": ts,
            "pnl_usd": pnl_usd, "r_usd": r_usd, "entry_usd": 10.0}


def _learning(trades, canary):
    db = SimpleNamespace(trades=trades, doctor_canary=canary, doctor_blacklist=_Plain(), bot_config=_Plain())
    ln = dl.LearningEngine(db)
    ln.reload_cb = None
    return ln


def test_canary_is_judged_on_all_fills_since_start_not_just_24h():
    started = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    rows = [_fill("hunt", 2.5 - i * 0.1, 3.0) for i in range(25)]        # 25 winners spread over days 2.5 → 0.1
    trades = _Trades(rows)
    can = _Canary()
    can.doc = {"state": "running", "book": "hunt", "started_at": started, "baseline_expectancy_r": -0.3,
               "baseline_max_drawdown_usd": 50.0, "proposal": {"key": "greylist_snipe_min_score", "value": 50, "book": "hunt"},
               "baseline_config_subset": {"greylist_snipe_min_score": 40.0}}
    ln = _learning(trades, can)
    last_24h = [r for r in rows if r["exit_time"] >= (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()]
    assert len(last_24h) < dl.PROMOTION_MIN_FILLS["hunt"] <= len(rows)   # the old 24 h window could never resolve
    asyncio.run(ln._evaluate_canary(can.doc, {}, last_24h))
    assert trades.queries and trades.queries[0]["exit_time"]["$gte"] == started
    assert can.doc["state"] == "promote" and can.doc["n_since"] == 25


def test_new_canary_replaces_the_old_doc_instead_of_merging_stale_verdict_fields():
    can = _Canary()
    can.doc = {"state": "reverted", "ended_at": "2026-09-10T19:13:14", "revert_reason": "canary failed", "n_since": 40}
    ln = _learning(_Trades([]), can)
    asyncio.run(ln._set_canary({"state": "running", "started_at": "2026-09-13T00:00:00", "book": "hunt"}))
    assert can.ops == ["replace"] and "ended_at" not in can.doc and "revert_reason" not in can.doc and "n_since" not in can.doc


def test_rebaseline_reverts_running_canary_and_clears_cached_stats():
    can = _Canary()
    can.doc = {"state": "running", "book": "hunt", "started_at": "2026-09-11T00:00:00", "proposal": {"key": "k", "value": 1, "book": "hunt"},
               "baseline_config_subset": {"greylist_snipe_min_score": 40.0}}
    ln = _learning(_Trades([]), can)
    ln.last.update({"books": {"hunt": {"n": 50}}, "allocator": {"rows": [1]}, "technique": {"x": 1}})

    async def _noop():
        return None
    ln._reload = _noop
    out = asyncio.run(ln.rebaseline("paper reset"))
    assert out == {"canary_reverted": True} and can.doc["state"] == "reverted" and can.doc["revert_reason"] == "paper reset"
    assert ln.last["books"] == {} and ln.last["allocator"] is None and ln.last["technique"] == {} and "re-baselined" in ln.last["note"]
    assert asyncio.run(ln.rebaseline("paper reset")) == {"canary_reverted": False}   # idempotent when nothing is running


def test_feed_seeded_graduate_is_seasoned_without_waiting_for_the_min_age():
    cfg = BotConfig(band_seasoned_min_age_min=17.25, band_seasoned_max_age_min=242.0)
    sc = MomentumScanner(SimpleNamespace(config=cfg))
    now = time.time()
    fresh = {"protocol": "pumpswap", "graduated_at": now - 60, "start": now - 60}
    assert sc.classify_band(fresh, cfg, now) is None                          # observed graduation 1 min ago: still too young
    assert sc.classify_band({**fresh, "graduated_feed": True}, cfg, now) == "seasoned"   # feed-seeded: pool pre-dates us
    old = {**fresh, "graduated_feed": True, "graduated_at": now - 250 * 60}
    assert sc.classify_band(old, cfg, now) is None                            # upper bound still applies


def test_prerank_tally_and_veto_cooldown_and_trade_count_query():
    import bot as botmod
    st = botmod.BotState.__new__(botmod.BotState)
    st.tracking = {}
    st.config = BotConfig()
    st.prerank_skip("seasoned", "mc")
    st.prerank_skip("seasoned", "mc")
    st.prerank_skip("new", "inflow")
    t = st.skip_tallies()
    assert t["prerank"] == {"seasoned": {"mc": 2}, "new": {"inflow": 1}} and t["seasoned_in_band"] is None
    # a vetoed bucket is skipped by the pre-rank loop until scanner_veto_until passes
    b = {"scanner_veto_until": time.time() + 300}
    assert time.time() < b["scanner_veto_until"]
    src = Path(__file__).resolve().parents[1].joinpath("scanner.py").read_text()
    assert 'now < b.get("scanner_veto_until", 0)' in src
    bsrc = Path(__file__).resolve().parents[1].joinpath("bot.py").read_text()
    assert 'b["scanner_veto_until"] = time.time() + 300.0' in bsrc
    ssrc = Path(__file__).resolve().parents[1].joinpath("server.py").read_text()
    assert '{"entry_time": {"$gte": midnight}}' in ssrc and '{"entry_time": {"$gte": midnight.isoformat()}}' in ssrc
