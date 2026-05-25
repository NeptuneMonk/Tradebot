"""
AccountEventBus tests. Verifies:
  - subscribe()/unsubscribe() are idempotent
  - wait_for_change returns True when an event is set, False on timeout
  - accountNotification messages dispatch to the right Event
  - subscription ACK populates the wss_sub_ids map
  - reconnect re-sends all active accountSubscribes
"""
import asyncio
import json
import os
from unittest.mock import patch, AsyncMock

os.environ.setdefault("HELIUS_RPC_URL", "https://x")
os.environ.setdefault("HELIUS_WSS_URL", "wss://x")
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")

from account_event_bus import AccountEventBus  # noqa: E402


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_subscribe_returns_same_event_on_repeat():
    bus = AccountEventBus()

    async def go():
        e1 = bus.subscribe("AcctA")
        e2 = bus.subscribe("AcctA")
        return e1 is e2

    assert _run(go()) is True


def test_wait_for_change_returns_true_when_pushed():
    bus = AccountEventBus()

    async def go():
        ev = bus.subscribe("AcctA")
        # Simulate Helius push by setting the event from another task
        async def pusher():
            await asyncio.sleep(0.05)
            ev.set()
        asyncio.create_task(pusher())
        return await bus.wait_for_change("AcctA", timeout=1.0)

    assert _run(go()) is True


def test_wait_for_change_returns_false_on_timeout():
    bus = AccountEventBus()

    async def go():
        bus.subscribe("AcctA")  # nothing pushes
        return await bus.wait_for_change("AcctA", timeout=0.05)

    assert _run(go()) is False


def test_wait_for_change_no_subscription_falls_back_to_sleep():
    bus = AccountEventBus()

    async def go():
        # No subscription on AcctZ — wait_for_change should sleep & return False
        t0 = asyncio.get_event_loop().time()
        r = await bus.wait_for_change("AcctZ", timeout=0.05)
        elapsed = asyncio.get_event_loop().time() - t0
        return r, elapsed

    result, elapsed = _run(go())
    assert result is False
    assert elapsed >= 0.045  # honored the timeout


def test_handle_subscription_ack_populates_sub_id():
    bus = AccountEventBus()

    async def go():
        bus.subscribe("AcctA")
        # Simulate ACK shape: {"id": <req_id>, "result": 42}
        # The pending_acks map gets the req_id when _send_subscribe runs;
        # since the bus isn't connected we manually inject it here:
        bus._pending_acks[12345] = "AcctA"
        await bus._handle_message(json.dumps({"id": 12345, "result": 42}))
        return bus._wss_sub_ids.get("AcctA")

    assert _run(go()) == 42


def test_handle_account_notification_sets_event():
    bus = AccountEventBus()

    async def go():
        ev = bus.subscribe("AcctA")
        bus._wss_sub_ids["AcctA"] = 42
        await bus._handle_message(json.dumps({
            "jsonrpc": "2.0",
            "method": "accountNotification",
            "params": {
                "subscription": 42,
                "result": {"context": {"slot": 1}, "value": {"lamports": 0}},
            },
        }))
        return ev.is_set(), bus.stats["events_received"]

    is_set, count = _run(go())
    assert is_set is True
    assert count == 1


def test_unsubscribe_drops_event_and_sub_id():
    bus = AccountEventBus()

    async def go():
        bus.subscribe("AcctA")
        bus._wss_sub_ids["AcctA"] = 99
        bus.unsubscribe("AcctA")
        return "AcctA" in bus._events, "AcctA" in bus._wss_sub_ids

    in_events, in_subs = _run(go())
    assert in_events is False
    assert in_subs is False


def test_resubscribe_all_resends_for_every_tracked_account():
    bus = AccountEventBus()
    sent = []

    async def go():
        # Pre-populate two active subscriptions + simulate "connected"
        bus._events["AcctA"] = asyncio.Event()
        bus._events["AcctB"] = asyncio.Event()
        # Stub the wire — record what gets sent
        bus._connected.set()

        async def fake_send(payload):
            sent.append(payload)

        with patch.object(bus, "_ws_send_safe", new=AsyncMock(side_effect=fake_send)):
            await bus._resubscribe_all()

    _run(go())
    accounts_sent = {p["params"][0] for p in sent}
    assert accounts_sent == {"AcctA", "AcctB"}
    # All requests are accountSubscribe with confirmed commitment
    for p in sent:
        assert p["method"] == "accountSubscribe"
        assert p["params"][1] == {"encoding": "base64", "commitment": "confirmed"}
