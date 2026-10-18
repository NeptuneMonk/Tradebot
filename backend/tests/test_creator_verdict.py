"""Creator Verdict — one holistic creator gate (greylist + own book + solvency + audit + smart wallets)."""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
import creator_verdict as cv  # noqa: E402
from models import BotConfig  # noqa: E402


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    def sort(self, *a, **k):
        return self

    def limit(self, n):
        return self

    async def to_list(self, n):
        return self.rows


class _Coll:
    def __init__(self, doc=None, rows=None):
        self.doc, self.rows = doc, rows or []

    async def find_one(self, q, proj=None):
        return self.doc

    def find(self, q, proj=None):
        return _Cursor(self.rows)


class _DB:
    def __init__(self, creator_doc=None, trades=None):
        self.creators = _Coll(doc=creator_doc)
        self.trades = _Coll(rows=trades)


def _hits(n_manual, n_learned):
    now = time.time()
    h = {}
    for i in range(n_manual):
        h[f"M{i}"] = {"ts": now, "kind": "manual", "sol": 1.0}
    for i in range(n_learned):
        h[f"L{i}"] = {"ts": now, "kind": "learned", "sol": 1.0}
    return {"smart_hits": h}


def _run(db, cfg, bucket=None, action="scan", trigger_kind=None, creator="C1"):
    cv.clear_cache()
    return asyncio.run(cv.compute(db, cfg, "MINT", creator, bucket, action=action, trigger_kind=trigger_kind, use_cache=False))


def test_blacklisted_creator_blocks_every_path():
    db = _DB(creator_doc={"greylist_blacklisted": True, "greylist_pattern": "untradeable_rug"})
    for action in ("scan", "smart_wallet", "reentry", "flush_addon"):
        v = _run(db, BotConfig(), action=action, trigger_kind="learned" if action == "smart_wallet" else None)
        assert v["level"] == "block", action
    assert any("blacklisted" in r for r in v["reasons"]) and any("untradeable_rug" in r for r in v["reasons"])


def test_manual_smart_wallet_lifts_block_to_caution_learned_does_not():
    db = _DB(creator_doc={"greylist_blacklisted": True})
    cfg = BotConfig()
    learned = _run(db, cfg, _hits(0, 1), action="smart_wallet", trigger_kind="learned")
    manual = _run(db, cfg, _hits(1, 0), action="smart_wallet", trigger_kind="manual")
    assert learned["level"] == "block"
    assert manual["level"] == "caution" and manual["size_mult"] == 0.5
    off = _run(db, BotConfig(creator_block_manual_smart_override=False), _hits(1, 0), action="smart_wallet", trigger_kind="manual")
    assert off["level"] == "block"


def test_own_book_three_losses_blocks_one_loss_cautions():
    cfg = BotConfig(creator_block_own_losses=3)
    three = _DB(creator_doc={}, trades=[{"pnl_usd": -1}, {"pnl_usd": -2}, {"pnl_usd": -0.5}])
    one = _DB(creator_doc={}, trades=[{"pnl_usd": -1}])
    mixed = _DB(creator_doc={}, trades=[{"pnl_usd": -1}, {"pnl_usd": -2}, {"pnl_usd": -3}, {"pnl_usd": 4}])
    assert _run(three, cfg)["level"] == "block"
    v1 = _run(one, cfg)
    assert v1["level"] == "caution" and v1["size_mult"] == 0.5
    assert _run(mixed, cfg)["level"] == "neutral"            # a win on the creator → not a serial loser


def test_caution_lifted_by_three_smart_wallets_any_kind():
    db = _DB(creator_doc={"greylist_pattern": "slow_rug_tradeable"})
    cfg = BotConfig(creator_caution_smart_override_n=3)
    two = _run(db, cfg, _hits(1, 1))
    three = _run(db, cfg, _hits(1, 2))
    assert two["level"] == "caution" and two["size_mult"] == 0.5
    assert three["level"] == "neutral" and three["size_mult"] == 1.0 and any("caution lifted" in r for r in three["reasons"])


def test_creator_dump_in_window_blocks_or_cautions():
    db = _DB(creator_doc={})
    dumping = {"creator_sold_quote": 3.0, "creator_sold_tokens": 60.0, "creator_start_tokens": 100.0}
    assert _run(db, BotConfig(creator_dump_block_pct=50.0), dumping)["level"] == "block"
    selling = {"creator_sold_quote": 0.4, "creator_sold_tokens": 10.0, "creator_start_tokens": 100.0}
    v = _run(db, BotConfig(), selling)
    assert v["level"] == "caution" and any("selling" in r for r in v["reasons"])


def test_favor_from_greylist_tier_and_smart_hits():
    db = _DB(creator_doc={"greylist_score": 80.0, "greylist_score_updated_at": time.time()})
    v = _run(db, BotConfig())
    assert v["level"] == "favor" and v["size_mult"] >= 1.0
    plain = _run(_DB(creator_doc={}), BotConfig(), _hits(0, 1))
    assert plain["level"] == "favor"
    unknown = _run(_DB(creator_doc=None), BotConfig(), None, creator=None)
    assert unknown["level"] == "neutral" and unknown["creator"] is None


def test_config_defaults_and_entry_wiring():
    cfg = BotConfig()
    assert cfg.creator_verdict_enabled and cfg.creator_block_own_losses == 3 and cfg.creator_caution_smart_override_n == 3
    assert cfg.creator_block_manual_smart_override is True and cfg.creator_caution_size_mult == 0.5
    src = open(os.path.join(os.path.dirname(__file__), "..", "bot.py")).read()
    assert '"creator-block"' in src and "_manual_force" in src and "_cvm" in src
