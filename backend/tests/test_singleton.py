"""One bot per deployment: lease, in-process loop stop on loss, takeover, send fence, relay round-trip (real local Mongo)."""
import asyncio
import os
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from motor.motor_asyncio import AsyncIOMotorClient
import singleton as sg


def _db():
    return AsyncIOMotorClient(os.environ["MONGO_URL"])[f"test_singleton_{uuid.uuid4().hex[:8]}"]


def _lease(db, pod, gains, losses, **kw):
    async def on_gain():
        gains.append(pod)

    async def on_loss(reason):
        losses.append((pod, reason))
    return sg.LeaderLease(db, on_gain=on_gain, on_loss=on_loss, pod_id=pod, ttl_s=kw.get("ttl", 2.0), renew_s=0.2)


def test_two_states_one_lease_only_one_starts_loops():
    async def run():
        db = _db()
        gains, losses = [], []
        a, b = _lease(db, "pod-a", gains, losses), _lease(db, "pod-b", gains, losses)
        await a.tick(); await b.tick()
        assert a.is_leader and not b.is_leader and gains == ["pod-a"]
        assert await a.is_leader_now() is True and await b.is_leader_now() is False
        # second tick: b still cannot take it while a renews
        await b.tick(); await a.tick(); await b.tick()
        assert a.is_leader and not b.is_leader and a.two_leaders is False and b.pods_seen if hasattr(b, "pods_seen") else True
        assert [p["role"] for p in b.pods] == ["leader", "follower"]
        await db.client.drop_database(db.name)
    asyncio.run(run())


def test_lease_drop_stops_loops_and_second_instance_takes_over():
    async def run():
        db = _db()
        gains, losses = [], []
        a, b = _lease(db, "pod-a", gains, losses, ttl=1.0), _lease(db, "pod-b", gains, losses, ttl=1.0)
        await a.tick(); await b.tick()
        assert a.is_leader
        await asyncio.sleep(1.2)                 # a stops renewing → lease expires
        await b.tick()                           # b takes it
        assert b.is_leader and gains == ["pod-a", "pod-b"]
        await a.tick()                           # a's renew fails → in-process loss, no exit
        assert not a.is_leader and losses and losses[0][0] == "pod-a"
        assert await a.is_leader_now() is False and await b.is_leader_now() is True
        await db.client.drop_database(db.name)
    asyncio.run(run())


def test_graceful_release_hands_over_immediately():
    async def run():
        db = _db()
        gains, losses = [], []
        a, b = _lease(db, "pod-a", gains, losses, ttl=30.0), _lease(db, "pod-b", gains, losses, ttl=30.0)
        await a.tick(); await b.tick()
        await a.release()
        await b.tick()
        assert b.is_leader and not a.is_leader
        assert all(p["_id"] != "pod-a" for p in b.pods)
        await db.client.drop_database(db.name)
    asyncio.run(run())


def test_send_fence_blocks_entries_and_exits_on_a_non_leader():
    from tests.test_profitability_refactor import _bot_stub
    from tests.test_runner_book import _patched, _slot
    import bot as botmod

    class _Lease:
        is_leader = True
        two_leaders = False

        async def is_leader_now(self):
            return False
    st = _patched(_bot_stub())
    st.singleton = _Lease()
    assert asyncio.run(st.leader_fence("test")) is False
    s = _slot("hunt")
    st.active_trades["M" * 44] = s
    asyncio.run(botmod.BotState._exit_impl(st, "M" * 44, "stop-loss", s))
    assert "M" * 44 in st.active_trades and s["trade"].get("status") != "closed"      # exit was a no-op, slot kept
    assert not [c for c in st.calls if c[0] == "db"]
    st.singleton = None
    assert asyncio.run(st.leader_fence("test")) is True                                  # single process: no fence


def test_rh_entry_and_exit_are_fenced_too():
    from tests.test_rh_paper import make_state, hot_bucket, enter, TOKEN
    st = make_state()
    st.leader_fence = AsyncMock(return_value=False)
    hot_bucket(st.rh_discovery, time.time())
    asyncio.run(st.rh_paper._enter(TOKEN))
    assert TOKEN not in st.rh_paper.positions and st.leader_fence.await_count == 1
    st.leader_fence = AsyncMock(return_value=True)
    enter(st)
    assert TOKEN in st.rh_paper.positions
    st.leader_fence = AsyncMock(return_value=False)
    asyncio.run(st.rh_paper.exit(TOKEN, "stop_loss"))
    assert TOKEN in st.rh_paper.positions and st.rh_paper.positions[TOKEN].get("_exiting") is None


def test_relay_round_trip_executes_on_leader_in_process():
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    async def run():
        db = _db()
        gains, losses = [], []
        leader = _lease(db, "pod-l", gains, losses, ttl=30.0)
        follower = _lease(db, "pod-f", gains, losses, ttl=30.0)
        await leader.tick(); await follower.tick()
        app = FastAPI()
        seen = {}

        @app.post("/api/echo")
        async def echo(request: Request):
            seen["auth"] = request.headers.get("authorization")
            seen["relayed"] = request.headers.get("x-pod-relayed")
            return JSONResponse({"got": await request.json(), "q": request.url.query})
        exec_relay = sg.CommandRelay(db, leader, app)
        exec_task = asyncio.create_task(exec_relay.executor_loop())
        sub_relay = sg.CommandRelay(db, follower, app)
        scope = {"type": "http", "method": "POST", "path": "/api/echo", "query_string": b"x=1", "headers": [
            (b"authorization", b"Bearer tok"), (b"content-type", b"application/json")], "scheme": "http", "server": ("f", 80)}
        body = b'{"a": 1}'
        got = {"sent": False}

        async def receive():
            if got["sent"]:
                return {"type": "http.request", "body": b"", "more_body": False}
            got["sent"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        resp = await sub_relay.submit(Request(scope, receive))
        exec_task.cancel()
        assert resp.status_code == 200 and resp.headers["x-pod-relayed"] == "1" and resp.headers["x-pod-leader"] == "pod-l"
        import json
        assert json.loads(resp.body) == {"got": {"a": 1}, "q": "x=1"}
        assert seen == {"auth": "Bearer tok", "relayed": "1"}
        await db.client.drop_database(db.name)
    asyncio.run(run())


def test_relay_times_out_cleanly_without_a_leader(monkeypatch):
    monkeypatch.setattr(sg, "RELAY_TIMEOUT_S", 0.5)
    from fastapi import Request

    async def run():
        db = _db()
        follower = _lease(db, "pod-f", [], [], ttl=30.0)
        r = sg.CommandRelay(db, follower, None)
        scope = {"type": "http", "method": "GET", "path": "/api/x", "query_string": b"", "headers": [], "scheme": "http", "server": ("f", 80)}

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        resp = await r.submit(Request(scope, receive))
        assert resp.status_code == 503 and r.stats["timeouts"] == 1
        assert (await db.pod_commands.find_one({}))["status"] == "expired"
        await db.client.drop_database(db.name)
    asyncio.run(run())
