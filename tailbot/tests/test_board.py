"""Active-trades board and message purge, with a fake Telegram transport."""
import asyncio

import pytest

from tailbot.board import TradeBoard, format_active_trades
from tailbot.config import Config
from tailbot.db import DB


MINT = "2tKjPFvzeWqBmmxjtGBb3zfb8zfFVePjpBhHbE2epump"
LEADER = "AvGiSdrkwtGYhrrFXZPtdhNhV4pASUR67TmRjfaKGdMP"


class Clock:
    def __init__(self):
        self.t = 1_700_000_000.0
        self.slept = []

    def now(self):
        return self.t

    async def sleep(self, dt):
        self.slept.append(dt)
        self.t += float(dt)


class FakeTG:
    def __init__(self, fail_delete=None):
        self.ops = []
        self.next_id = 10
        self.fail_delete = fail_delete or {}

    async def send_message(self, text, markup=None):
        self.ops.append(("send", text))
        self.next_id += 1
        return self.next_id

    async def edit_message(self, message_id, text, markup=None):
        self.ops.append(("edit", int(message_id), text))

    async def delete_message(self, message_id):
        self.ops.append(("delete", int(message_id)))
        if int(message_id) in self.fail_delete:
            raise RuntimeError(self.fail_delete[int(message_id)])


def _pos(**kw):
    base = dict(
        mint=MINT, symbol="MICKEY", leader=LEADER, opened_ts=1_700_000_000.0 - 3700,
        entry_price=0.01, last_price=0.013, peak_price=0.016, tokens_raw=10_000_000,
        decimals=6, cost_sol=0.10, status="open", prompted=0, next_prompt_ts=0,
    )
    base.update(kw)
    return base


def test_active_trades_text_lists_the_position():
    cfg = Config()
    now = 1_700_000_000.0
    text = format_active_trades([_pos()], pot=1.2, deployed=0.4, realized=0.04, now=now, cfg=cfg)
    assert text.startswith("Active trades")
    assert "pot 1.2000 SOL" in text
    assert "deployed 33%" in text
    assert "today +0.0400 SOL" in text
    assert "MICKEY" in text and "2tKj" in text and "AvGi" in text
    assert "CT" in text and "held" in text
    assert "entry 0.1000" in text
    assert "trail" in text and "momentum" in text and "stop" in text
    flat = format_active_trades(
        [_pos(last_price=0.01, peak_price=0.01)], pot=1, deployed=0, realized=0, now=now, cfg=cfg,
    )
    assert "armed: stop" in flat
    assert "trail" not in flat.split("armed:")[-1]


def _board(tmp_path, text="board v1", fail_delete=None):
    db = DB(str(tmp_path / "t.sqlite"))
    clock = Clock()
    holder = {"text": text}
    tg = FakeTG(fail_delete)
    cfg = Config(tg_min_interval_s=1.1, board_debounce_s=2, board_edit_s=45, purge_after_minutes=60)
    board = TradeBoard(db, tg, lambda: holder["text"], clock.now, clock.sleep, cfg)
    return db, clock, tg, board, holder


def test_bump_deletes_the_old_board_and_resends_it_last(tmp_path):
    db, clock, tg, board, _holder = _board(tmp_path)

    async def go():
        await board.start()
        first = board._board_id
        board.schedule_bump()
        board.schedule_bump()
        await board.tick()
        assert board._board_id == first
        clock.t += 2
        await board.tick()
        return first

    first = asyncio.run(go())
    sends = [op for op in tg.ops if op[0] == "send"]
    deletes = [op for op in tg.ops if op[0] == "delete"]
    assert len(sends) == 2
    assert deletes == [("delete", first)]
    assert tg.ops[-1][0] == "send"
    assert db.kv_get("tg_board_id") == str(board._board_id)
    assert any(row["message_id"] == board._board_id and row["kind"] == "board" for row in db.tg_messages())


def test_price_change_edits_in_place(tmp_path):
    _db, clock, tg, board, holder = _board(tmp_path)

    async def go():
        await board.start()
        mid = board._board_id
        holder["text"] = "board v2"
        clock.t += 10
        await board.tick()
        clock.t += 45
        holder["text"] = "board v3"
        await board.tick()
        return mid

    mid = asyncio.run(go())
    edits = [op for op in tg.ops if op[0] == "edit"]
    assert edits == [("edit", mid, "board v3")]
    assert board._board_id == mid
    assert sum(1 for op in tg.ops if op[0] == "send") == 1


def test_restart_deletes_the_saved_board(tmp_path):
    db, _clock, tg, board, _holder = _board(tmp_path)
    db.kv_set("tg_board_id", "77")
    db.record_tg(77, 1_700_000_000.0, "board", None)
    asyncio.run(board.start())
    assert ("delete", 77) in tg.ops
    assert board._board_id != 77
    assert db.kv_get("tg_board_id") == str(board._board_id)


def test_purge_keeps_board_and_unanswered_prompt(tmp_path):
    db, clock, tg, board, _holder = _board(tmp_path, fail_delete={4: "message can't be deleted"})
    now = clock.t
    db.record_tg(1, now - 3700, "alert", None)
    db.record_tg(3, now - 3700, "prompt", 1)
    db.record_tg(4, now - 3700, "alert", None)
    db.record_tg(5, now - 50 * 3600, "alert", None)
    db.insert_position(
        leader=LEADER, mint=MINT, status="open", tokens_raw=1, cost_sol=0.1, entry_price=0.01,
        opened_ts=now, peak_price=0.01, last_price=0.01, decimals=6, protocol="pumpfun",
        pool=None, curve=None, quote_vault=None, quote_mint=None, next_prompt_ts=0, prompted=1, created_sig="s",
    )

    async def go():
        await board.start()
        await board.purge()

    asyncio.run(go())
    deleted = {op[1] for op in tg.ops if op[0] == "delete"}
    assert 1 in deleted
    assert 3 not in deleted
    assert board._board_id not in deleted
    ids = {row["message_id"] for row in db.tg_messages()}
    assert 3 in ids
    assert board._board_id in ids
    assert 5 not in ids
    assert 4 not in ids
    assert ("delete", 5) not in tg.ops


def test_rate_limit_spaces_calls(tmp_path):
    _db, clock, _tg, board, _holder = _board(tmp_path)

    async def go():
        await board.gate()
        await board.gate()

    asyncio.run(go())
    assert len(clock.slept) == 1
    assert clock.slept[0] == pytest.approx(1.1)


def test_429_stops_the_purge_batch(tmp_path):
    db, clock, tg, board, _holder = _board(tmp_path / "rate", fail_delete={1: "429 too many requests"})
    now = clock.t
    db.record_tg(1, now - 3700, "alert", None)
    db.record_tg(2, now - 3700, "alert", None)

    async def go():
        board._board_id = 99
        await board.purge()

    asyncio.run(go())
    assert ("delete", 1) in tg.ops
    assert ("delete", 2) not in tg.ops
    ids = {row["message_id"] for row in db.tg_messages()}
    assert 1 in ids and 2 in ids
