"""Creator ↔ smart-wallet affinity: credit smart buys to the creator, favour a magnet's next launch at t=0."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
import creator_smart as cs  # noqa: E402
import creator_verdict as cv  # noqa: E402
from models import BotConfig  # noqa: E402
from tests.test_creator_verdict import _DB, _run  # noqa: E402


def test_summary_rate_and_recurring_wallets():
    assert cs.summary({}) is None and cs.summary(None) is None
    d = {"tokens_created": 4, "smart_mints": ["a", "b", "c"], "smart_hits_total": 7, "smart_manual_total": 2,
         "smart_wallets": {"W1": 3, "W2": 1, "W3": 2}, "last_smart_at": 123.0}
    s = cs.summary(d)
    assert s["launches"] == 4 and s["smart_launches"] == 3 and s["rate_pct"] == 75.0 and s["hits"] == 7 and s["manual_hits"] == 2
    assert [w["wallet"] for w in s["recurring_wallets"]] == ["W1", "W3"]
    # tokens_created lagging behind smart_mints never yields > 100 %
    assert cs.summary({"tokens_created": 1, "smart_mints": ["a", "b"]})["rate_pct"] == 100.0


def test_is_magnet_thresholds():
    cfg = BotConfig(creator_smart_min_launches=2, creator_smart_min_rate_pct=50.0)
    assert cs.is_magnet(cs.summary({"tokens_created": 4, "smart_mints": ["a", "b"]}), cfg)
    assert not cs.is_magnet(cs.summary({"tokens_created": 10, "smart_mints": ["a", "b"]}), cfg)          # 20 % < 50 %
    assert not cs.is_magnet(cs.summary({"tokens_created": 1, "smart_mints": ["a"]}), cfg)                 # 1 launch < 2
    assert not cs.is_magnet(cs.summary({"tokens_created": 4, "smart_mints": ["a", "b"]}), BotConfig(creator_smart_magnet_enabled=False))
    assert "smart magnet" in cs.magnet_reason(cs.summary({"tokens_created": 4, "smart_mints": ["a", "b"], "smart_hits_total": 5}))


def test_record_hit_builds_the_right_update():
    captured = {}

    class _Creators:
        async def update_one(self, q, upd, upsert=False):
            captured.update({"q": q, "upd": upd, "upsert": upsert})

    class _Db:
        creators = _Creators()

    async def go():
        cs.record_hit(_Db(), "CREATOR", "MINT", "W1", "manual", now=99.0)
        await asyncio.sleep(0)
    asyncio.run(go())
    assert captured["q"] == {"_id": "CREATOR"} and captured["upsert"] is True
    u = captured["upd"]
    assert u["$addToSet"] == {"smart_mints": "MINT"} and u["$inc"]["smart_hits_total"] == 1 and u["$inc"]["smart_manual_total"] == 1
    assert u["$inc"]["smart_wallets.W1"] == 1 and u["$set"]["last_smart_at"] == 99.0
    cs.record_hit(_Db(), None, "MINT", "W1", "manual")          # no creator → no-op


def test_verdict_favors_a_magnet_creator_but_never_rescues_a_block():
    magnet = {"tokens_created": 4, "smart_mints": ["a", "b", "c"], "smart_hits_total": 6, "smart_wallets": {"W1": 2}}
    v = _run(_DB(creator_doc=magnet), BotConfig())
    assert v["level"] == "favor" and any("smart magnet" in r for r in v["reasons"]) and v["smart_affinity"]["smart_launches"] == 3
    assert _run(_DB(creator_doc={**magnet, "greylist_blacklisted": True}), BotConfig())["level"] == "block"
    assert _run(_DB(creator_doc=magnet), BotConfig(creator_smart_magnet_enabled=False))["level"] == "neutral"
    cv.clear_cache()
