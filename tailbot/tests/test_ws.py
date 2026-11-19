"""logsSubscribe must send mentions as a list, and subscribe errors must not be dropped."""
import asyncio
import json
import logging

from tailbot.credits import CreditMeter
from tailbot.helius_ws import HeliusWS, logs_subscribe_params


class FakeSocket:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


def _ws(*leaders, accounts=()):
    ws = HeliusWS("test-key", None, None, CreditMeter(100, 100))
    ws.set_leaders(list(leaders))
    ws.accounts = set(accounts)
    ws._resub = False
    return ws


def test_mentions_param_is_a_sequence():
    params = logs_subscribe_params("AvGiSdrkwtGYhrrFXZPtdhNhV4pASUR67TmRjfaKGdMP")
    assert params[0]["mentions"] == ["AvGiSdrkwtGYhrrFXZPtdhNhV4pASUR67TmRjfaKGdMP"]
    assert isinstance(params[0]["mentions"], list)


def test_subscribe_error_without_result_is_logged(caplog):
    ws = _ws("LeaderWallet111")
    sock = FakeSocket()

    async def go():
        await ws._subscribe_all(sock)
        req = sock.sent[0]
        assert isinstance(req["params"][0]["mentions"], list)
        await ws._on_message({
            "jsonrpc": "2.0",
            "id": req["id"],
            "error": {"code": -32602, "message": "Invalid params: expected a sequence"},
        })

    caplog.set_level(logging.WARNING, logger="tailbot.ws")
    asyncio.run(go())
    assert "expected a sequence" in caplog.text
    assert "helius subscribe error" in caplog.text
    assert ws._resub is True
    assert "subscriptions confirmed" not in caplog.text


def test_subscriptions_confirmed_when_every_ack_arrives(caplog):
    ws = _ws("LeaderWallet111", accounts={"CurveAcct111"})
    sock = FakeSocket()

    async def go():
        await ws._subscribe_all(sock)
        assert [item["method"] for item in sock.sent] == ["logsSubscribe", "accountSubscribe"]
        for i, req in enumerate(sock.sent):
            await ws._on_message({"jsonrpc": "2.0", "id": req["id"], "result": 70 + i})

    caplog.set_level(logging.INFO, logger="tailbot.ws")
    asyncio.run(go())
    assert "subscriptions confirmed: 1 logs, 1 accounts" in caplog.text
    assert ws._resub is False


def test_missing_acks_resubscribe(caplog):
    ws = _ws("LeaderWallet111")
    sock = FakeSocket()

    async def go():
        await ws._subscribe_all(sock)

    caplog.set_level(logging.WARNING, logger="tailbot.ws")
    asyncio.run(go())
    assert ws.check_ack_timeout(ws._ack_deadline - 1) is False
    assert ws.check_ack_timeout(ws._ack_deadline + 1) is True
    assert ws._resub is True
    assert "acks missing" in caplog.text
