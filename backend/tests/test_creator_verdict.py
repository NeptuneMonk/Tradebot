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


def test_own_book_win_rate_blocks_after_min_trades_only():
    cfg = BotConfig(creator_min_trades_for_rate=6, creator_min_win_rate_pct=40.0)
    L, W = {"pnl_usd": -1}, {"pnl_usd": 2}
    # five trades, all losses → not judged yet (no block, no caution)
    five = _run(_DB(creator_doc={}, trades=[L] * 5), cfg)
    assert five["level"] == "neutral" and five["size_mult"] == 1.0 and any("more to judge" in r for r in five["reasons"])
    # six trades, 2 wins = 33% < 40% → block
    bad = _run(_DB(creator_doc={}, trades=[L, L, L, L, W, W]), cfg)
    assert bad["level"] == "block" and any("33% < 40%" in r for r in bad["reasons"]) and bad["own_win_rate_pct"] == 33.3
    # six trades, 3 wins = 50% ≥ 40% → favor
    good = _run(_DB(creator_doc={}, trades=[L, L, L, W, W, W]), cfg)
    assert good["level"] == "favor" and good["own_win_rate_pct"] == 50.0
    # knob: raise the bar to 60% → the same 50% creator is blocked
    assert _run(_DB(creator_doc={}, trades=[L, L, L, W, W, W]), BotConfig(creator_min_win_rate_pct=60.0))["level"] == "block"
    # knob: judge after 3 → three losses now block
    assert _run(_DB(creator_doc={}, trades=[L, L, L]), BotConfig(creator_min_trades_for_rate=3))["level"] == "block"
    # break-even trades (pnl 0) are neither a win nor a loss
    assert _run(_DB(creator_doc={}, trades=[{"pnl_usd": 0}] * 6), cfg)["level"] == "neutral"


def test_caution_lifted_by_three_smart_wallets_any_kind():
    db = _DB(creator_doc={"greylist_pattern": "slow_rug_tradeable"})
    cfg = BotConfig(creator_caution_smart_override_n=3)
    two = _run(db, cfg, _hits(1, 1))
    three = _run(db, cfg, _hits(1, 2))
    assert two["level"] == "caution" and two["size_mult"] == 0.5
    assert three["level"] == "neutral" and three["size_mult"] == 1.0 and any("caution lifted" in r for r in three["reasons"])


def test_dev_already_out_is_favor_partial_sell_is_never_a_penalty():
    db = _DB(creator_doc={})
    out = {"creator_sold_quote": 3.0, "creator_sold_tokens": 80.0, "creator_start_tokens": 100.0}
    v = _run(db, BotConfig(creator_dev_out_pct=70.0), out)
    assert v["level"] == "favor" and any("already out" in r for r in v["reasons"])
    mid = {"creator_sold_quote": 0.4, "creator_sold_tokens": 10.0, "creator_start_tokens": 100.0}
    v2 = _run(db, BotConfig(), mid)
    assert v2["level"] == "neutral" and v2["size_mult"] == 1.0 and any("can't be dumped later" in r for r in v2["reasons"])
    # a partial sell never downgrades an otherwise-favored creator
    fav = _run(_DB(creator_doc={"greylist_score": 80.0, "greylist_score_updated_at": time.time()}), BotConfig(), mid)
    assert fav["level"] == "favor"
    # dev-out never rescues a blacklisted creator
    assert _run(_DB(creator_doc={"greylist_blacklisted": True}), BotConfig(), out)["level"] == "block"


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
    assert cfg.creator_verdict_enabled and cfg.creator_min_trades_for_rate == 6 and cfg.creator_min_win_rate_pct == 40.0 and cfg.creator_caution_smart_override_n == 3
    assert cfg.creator_block_manual_smart_override is True and cfg.creator_caution_size_mult == 0.5
    src = open(os.path.join(os.path.dirname(__file__), "..", "bot.py")).read()
    assert '"creator-block"' in src and "_manual_force" in src and "_cvm" in src
