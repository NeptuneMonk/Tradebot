"""Reconnect backfill: replay a short gap, and do not copy a late buy."""
import asyncio

from tailbot.backfill import plan_backfill
from tailbot.redact import RedactFilter, install_redaction, redact
from tests.test_engine import LEADER, MINT, make

import logging


def _tx(keys, pre_sol, post_sol, pre_tok, post_tok):
    return {
        "meta": {
            "err": None, "fee": 5000,
            "preBalances": pre_sol, "postBalances": post_sol,
            "preTokenBalances": pre_tok, "postTokenBalances": post_tok,
            "logMessages": [],
        },
        "transaction": {"message": {"accountKeys": [{"pubkey": k, "signer": i == 0} for i, k in enumerate(keys)]}},
    }


def _tok(idx, mint, owner, amount, decimals=6):
    return {"accountIndex": idx, "mint": mint, "owner": owner, "uiTokenAmount": {"amount": str(amount), "decimals": decimals}}


def _buy_tx():
    # Leader spends 1 SOL and receives 100 tokens. Quote in the test engine is the same price.
    return _tx([LEADER, "ata"], [2_000_000_000, 0], [1_000_000_000, 0], [], [_tok(1, MINT, LEADER, 100_000_000)])


def _sell_tx():
    return _tx(
        [LEADER, "ata"],
        [1_000_000_000, 0],
        [2_000_000_000, 0],
        [_tok(1, MINT, LEADER, 100_000_000)],
        [_tok(1, MINT, LEADER, 0)],
    )


class FakeRPC:
    def __init__(self, rows, txs):
        self.rows = rows
        self.txs = txs
        self.calls = []

    async def get_signatures(self, address, limit=25, until=None):
        self.calls.append(("sigs", address, limit, until))
        return list(self.rows)

    async def get_transaction(self, sig):
        self.calls.append(("tx", sig))
        return self.txs.get(sig)


def test_plan_orders_oldest_first_and_marks_stale():
    now = 1_000.0
    rows = [
        {"signature": "new", "blockTime": now - 2, "err": None},
        {"signature": "old", "blockTime": now - 40, "err": None},
        {"signature": "done", "blockTime": now - 1, "err": None},
        {"signature": "bad", "blockTime": now - 1, "err": {"InstructionError": []}},
    ]
    planned = plan_backfill(rows, now=now, window_s=15, seen={"done"}, limit=25)
    assert [item["signature"] for item in planned] == ["bad", "done", "old", "new"]
    assert [item["action"] for item in planned] == ["failed", "dup", "stale", "fetch"]


def test_backfill_copies_a_fresh_buy_and_skips_a_stale_one(tmp_path):
    engine, box, clock = make(tmp_path)
    fresh, stale = "FreshSig111111", "StaleSig222222"
    rpc = FakeRPC(
        [
            {"signature": fresh, "blockTime": clock.t - 1.5, "err": None},
            {"signature": stale, "blockTime": clock.t - 40, "err": None},
        ],
        {fresh: _buy_tx()},
    )
    engine.rpc = rpc

    async def go():
        await engine.backfill_leaders()

    asyncio.run(go())
    assert engine.db.open_positions(), box.msgs
    assert any("stale" in text for text, _m in box.msgs)
    assert any(text.startswith("COPY") for text, _m in box.msgs)
    assert ("tx", stale) not in rpc.calls
    assert ("tx", fresh) in rpc.calls
    assert rpc.calls[0] == ("sigs", LEADER, 25, None)
    assert engine.db.cursor(LEADER) == fresh
    assert engine.db.seen(stale, LEADER)
    assert engine.db.seen(fresh, LEADER)


def test_backfill_dedups_and_passes_the_cursor(tmp_path):
    engine, _box, clock = make(tmp_path / "dedup")
    sig = "AlreadySeen111"
    engine.db.mark_seen(sig, LEADER, clock.t)
    engine.db.set_cursor(LEADER, "OlderCursor111")
    rpc = FakeRPC([{"signature": sig, "blockTime": clock.t - 1, "err": None}], {sig: _buy_tx()})
    engine.rpc = rpc

    asyncio.run(engine.backfill_leaders())
    assert rpc.calls == [("sigs", LEADER, 25, "OlderCursor111")]
    assert engine.db.open_positions() == []


def test_stale_sell_still_closes_an_open_position(tmp_path):
    engine, box, clock = make(tmp_path / "sell", sell_px=0.01)

    async def go():
        from tests.test_engine import buy
        await engine.on_leader_fill(buy())
        assert engine.db.open_positions()
        sell = "LateSell111111"
        engine.rpc = FakeRPC(
            [{"signature": sell, "blockTime": clock.t - 40, "err": None}],
            {sell: _sell_tx()},
        )
        await engine.backfill_leaders()
        assert engine.db.open_positions() == []
        assert any(text.startswith("EXIT leader sold") for text, _m in box.msgs)
        assert ("tx", sell) in engine.rpc.calls

    asyncio.run(go())


def test_http_logs_do_not_keep_secrets():
    install_redaction()
    assert logging.getLogger("httpx").level >= logging.WARNING
    assert logging.getLogger("httpcore").level >= logging.WARNING
    url = "https://mainnet.helius-rpc.com/?api-key=not-a-real-key"
    token = "https://api.telegram.org/bot12345678:shorttoken/sendMessage"
    assert "not-a-real-key" not in redact(url)
    assert "shorttoken" not in redact(token)
    assert "REDACTED" in redact(url) and "REDACTED" in redact(token)
    record = logging.LogRecord("httpx", logging.INFO, "", 0, "HTTP Request: POST %s", (url,), None)
    assert RedactFilter().filter(record) is True
    assert "not-a-real-key" not in record.args[0]
