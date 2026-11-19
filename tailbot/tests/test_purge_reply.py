"""Reply /purge deletes a span of the chat and puts the board back at the bottom."""
import asyncio

from tailbot.board import TradeBoard, purge_id_span
from tailbot.config import Config
from tailbot.db import DB
from tailbot.telegram_bot import Telegram, route_update

MINT = "2tKjPFvzeWqBmmxjtGBb3zfb8zfFVePjpBhHbE2epump"
LEADER = "AvGiSdrkwtGYhrrFXZPtdhNhV4pASUR67TmRjfaKGdMP"


class Clock:
    def __init__(self):
        self.t = 1_700_000_000.0

    def now(self):
        return self.t

    async def sleep(self, dt):
        self.t += float(dt)


class Transport:
    def __init__(self):
        self.ops = []
        self.next_id = 100

    async def send_message(self, text, markup=None):
        self.ops.append(("send", text))
        self.next_id += 1
        return self.next_id

    async def edit_message(self, message_id, text, markup=None):
        self.ops.append(("edit", int(message_id)))

    async def delete_message(self, message_id):
        self.ops.append(("delete", int(message_id)))
        if int(message_id) in (11, 40):
            raise RuntimeError("can't delete user message")


def test_span_is_inclusive_and_skips_kept_ids():
    assert purge_id_span(10, 14, {12}) == [10, 11, 13, 14]
    assert purge_id_span(14, 10, set()) == [10, 11, 12, 13, 14]


def test_reply_purge_deletes_the_range_and_reposts_the_board(tmp_path):
    db = DB(str(tmp_path / "t.sqlite"))
    clock = Clock()
    now = clock.t
    db.insert_position(
        leader=LEADER, mint=MINT, status="open", tokens_raw=1, cost_sol=0.1, entry_price=0.01,
        opened_ts=now, peak_price=0.01, last_price=0.01, decimals=6, protocol="pumpfun",
        pool=None, curve=None, quote_vault=None, quote_mint=None, next_prompt_ts=0, prompted=1, created_sig="s",
    )
    db.record_tg(15, now, "alert", None)
    db.record_tg(20, now, "prompt", 1)
    db.record_tg(30, now, "board", None)
    db.record_tg(35, now, "alert", None)
    transport = Transport()
    board = TradeBoard(db, transport, lambda: "Active trades\nrestored", clock.now, clock.sleep, Config(tg_min_interval_s=0))
    board._board_id = 30

    asyncio.run(board.purge_reply(10, 40))

    deleted = [op[1] for op in transport.ops if op[0] == "delete"]
    assert 15 in deleted and 30 in deleted and 35 in deleted and 40 in deleted and 11 in deleted
    assert 20 not in deleted
    assert transport.ops[-1] == ("send", "Active trades\nrestored")
    ids = {row["message_id"]: row["kind"] for row in db.tg_messages()}
    assert ids[20] == "prompt"
    assert 15 not in ids and 35 not in ids
    assert board._board_id == 101
    assert ids[board._board_id] == "board"


def test_other_chats_are_ignored_and_bare_purge_does_not_delete(tmp_path):
    assert route_update(
        {"message": {"chat": {"id": 9}, "message_id": 5, "text": "/purge", "reply_to_message": {"message_id": 1}}},
        "2",
    ) is None
    db = DB(str(tmp_path / "t.sqlite"))
    clock = Clock()
    calls = []

    class Control:
        def __init__(self):
            self.db = db
            self.cfg = Config(tg_min_interval_s=0)
            self.now = clock.now
            self.sleep = clock.sleep

        def active_trades_text(self):
            return "Active trades"

        async def on_command(self, text):
            return "should-not-run"

    async def poster(method, payload):
        calls.append((method, payload.get("message_id"), payload.get("text")))
        if method == "deleteMessage" and payload.get("message_id") == 7:
            raise RuntimeError("can't delete user message")
        if method == "sendMessage":
            return {"message_id": 50}
        return True

    tg = Telegram("token", "2", Control(), poster=poster)
    foreign = {"message": {"chat": {"id": 9}, "message_id": 8, "text": "/purge", "reply_to_message": {"message_id": 1}}}
    bare = {"message": {"chat": {"id": 2}, "message_id": 9, "text": "/purge"}}
    replied = {"message": {"chat": {"id": 2}, "message_id": 12, "text": "/purge", "reply_to_message": {"message_id": 7}}}

    async def go():
        await tg.handle_update(foreign)
        await tg.handle_update(bare)
        db.record_tg(10, clock.t, "alert", None)
        tg.board._board_id = 10
        await tg.handle_update(replied)

    asyncio.run(go())
    assert calls[0][0] == "sendMessage" and "reply to a message" in calls[0][2]
    deleted = [call[1] for call in calls if call[0] == "deleteMessage"]
    assert 7 in deleted and 12 in deleted and 10 in deleted
    assert not any(call[2] and "should-not-run" in str(call[2]) for call in calls)
