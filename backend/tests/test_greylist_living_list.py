"""Living greylist: inactivity flag, dead recency, Stage-1 exclusion, prune."""
import asyncio, os, sys
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
import creator_greylist as cg
from models import BotConfig

NOW = datetime.now(timezone.utc)


def test_activity_component_dead_after_inactive_window():
    doc = {"tokens_created": 8, "first_seen": (NOW - timedelta(days=60)).isoformat()}
    assert cg._activity_component({**doc, "last_seen": (NOW - timedelta(hours=2)).isoformat()}) > 0
    assert cg._activity_component({**doc, "last_seen": (NOW - timedelta(days=10)).isoformat()}) > 0
    assert cg._activity_component({**doc, "last_seen": (NOW - timedelta(days=40)).isoformat()}) == 0.0


def test_stage1_rejects_inactive_creator():
    ok, reason = cg.stage1_filter({"tokens_failed": 5, "greylist_inactive": True}, [])
    assert ok is False and "inactive" in reason
    ok, _ = cg.stage1_filter({"tokens_failed": 5}, [])
    assert ok is True


class _Res:
    def __init__(self, n): self.modified_count = n


class _Creators:
    def __init__(self): self.calls = []
    async def update_many(self, flt, upd):
        self.calls.append((flt, upd)); return _Res(len(self.calls))


def test_prune_flags_stale_and_revives_active():
    db = type("DB", (), {})(); db.creators = _Creators()
    out = asyncio.run(cg.prune_inactive_creators(db, 30))
    (f_flt, f_upd), (r_flt, r_upd) = db.creators.calls
    assert "$lt" in f_flt["last_seen"] and f_upd["$set"]["greylist_inactive"] is True
    assert "$gte" in r_flt["last_seen"] and r_flt["greylist_inactive"] is True and "greylist_inactive" in r_upd["$unset"]
    assert out["flagged"] == 1 and out["revived"] == 2
    assert BotConfig().creator_greylist_inactive_days == 30
