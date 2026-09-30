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
            seen["cookie"] = request.headers.get("cookie")
            seen["relayed"] = request.headers.get("x-pod-relayed")
            seen["nonce"] = request.headers.get("x-pod-exec")
            return JSONResponse({"got": await request.json(), "q": request.url.query})

        async def resolve_user(request):
            return "user-42" if request.headers.get("authorization") == "Bearer tok" else None
        exec_relay = sg.CommandRelay(db, leader, app, issue_nonce=lambda uid: f"nonce-for-{uid}")
        exec_task = asyncio.create_task(exec_relay.executor_loop())
        sub_relay = sg.CommandRelay(db, follower, app, resolve_user=resolve_user)
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
        # credentials never cross Mongo: the leader sees a one-shot nonce, no bearer, no cookie
        assert seen == {"auth": None, "cookie": None, "relayed": "1", "nonce": "nonce-for-user-42"}
        stored = await db.pod_commands.find_one({})
        assert stored["user_id"] == "user-42" and "authorization" not in stored["headers"] and "cookie" not in stored["headers"]
        assert "tok" not in json.dumps(stored, default=str)
        # unauthenticated caller is refused on the follower, nothing is parked
        scope2 = {**scope, "headers": [(b"content-type", b"application/json")]}
        got["sent"] = False
        r2 = await sub_relay.submit(Request(scope2, receive))
        assert r2.status_code == 401 and await db.pod_commands.count_documents({}) == 1
        await db.client.drop_database(db.name)
    asyncio.run(run())


def test_exec_nonce_is_one_shot_and_expires():
    import auth
    n = auth.issue_exec_nonce("u1")
    assert auth.consume_exec_nonce(n) == "u1" and auth.consume_exec_nonce(n) is None
    n2 = auth.issue_exec_nonce("u2")
    auth._EXEC_NONCES[n2] = ("u2", time.time() - 1)
    assert auth.consume_exec_nonce(n2) is None


def test_active_mint_unique_index_and_entry_lock():
    async def run():
        db = _db()
        await db.trades.create_index([("mint", 1)], name="uniq_active_mint", unique=True, partialFilterExpression={"status": "active"})
        await db.trades.insert_one({"_id": "t1", "mint": "M1", "status": "active"})
        from pymongo.errors import DuplicateKeyError
        with pytest.raises(DuplicateKeyError):
            await db.trades.update_one({"_id": "t2"}, {"$set": {"mint": "M1", "status": "active"}}, upsert=True)
        await db.trades.insert_one({"_id": "t3", "mint": "M1", "status": "closed"})          # closed rows are free
        # entry lock: first claim wins; a DIFFERENT pod aborts before send; the same pod may retry (re-entrant)
        import bot as botmod
        from types import SimpleNamespace
        st = SimpleNamespace(db=db, singleton=SimpleNamespace(pod_id="pod-A"))
        other = SimpleNamespace(db=db, singleton=SimpleNamespace(pod_id="pod-B"))
        assert await botmod.BotState.claim_entry_lock(st, "M2") is True
        assert await botmod.BotState.claim_entry_lock(other, "M2") is False
        assert await botmod.BotState.claim_entry_lock(st, "M2") is True
        assert await botmod.BotState.claim_entry_lock(st, "M2", "rh") is True
        await db.client.drop_database(db.name)
    asyncio.run(run())


def test_persist_trade_duplicate_parks_fill_instead_of_losing_it():
    async def run():
        db = _db()
        await db.trades.create_index([("mint", 1)], name="uniq_active_mint", unique=True, partialFilterExpression={"status": "active"})
        await db.trades.insert_one({"_id": "first", "mint": "M1", "status": "active"})
        import bot as botmod
        from models import Trade
        from types import SimpleNamespace
        st = SimpleNamespace(db=db)
        t = Trade(mint="M1", symbol="X", name="X", risk_score=1, classifier_action="manual", mode="live", entry_sol=0.1,
                  entry_usd=10.0, entry_tokens=1, entry_price_sol=0.1)
        await botmod.BotState._persist_trade(st, t)
        doc = await db.trades.find_one({"_id": t.id})
        assert t.status == "exit_failed_terminal" and doc["status"] == "exit_failed_terminal" and doc["venue_stage"] == "duplicate_fill"
        assert doc["pnl_pct"] is None
        await db.client.drop_database(db.name)
    asyncio.run(run())


def test_stop_loops_cancels_rh_feed_and_background_tasks():
    from tests.test_profitability_refactor import _bot_stub

    async def run():
        st = _bot_stub()

        async def forever():
            await asyncio.sleep(3600)
        from types import SimpleNamespace
        for name in ("rh_feed", "rh_discovery", "discovery", "rh_paper", "pnl_reconciler"):
            if not hasattr(st, name):
                setattr(st, name, SimpleNamespace())
        st.rh_feed._task = asyncio.create_task(forever())
        st.rh_discovery._task = asyncio.create_task(forever())
        st._bg_tasks = [asyncio.create_task(forever())]
        st.active_trades["M"] = {"trade": {}}
        await st.stop_loops("test")
        await asyncio.sleep(0)
        assert st.rh_feed._task.cancelled() and st.rh_discovery._task.cancelled()
        assert st.active_trades == {} and st.leader_ok is False and st._initial_load_done is False
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


def test_ws_mirror_recreates_uncapped_collection_as_capped():
    async def run():
        db = _db()
        await db.ws_events.insert_many([{"seq": i, "type": "x", "data": {}} for i in range(3)])   # racing insert → uncapped
        m = sg.WSMirror(db, _lease(db, "pod-m", [], [], ttl=30.0), None)
        await m.ensure_collection()
        info = [c async for c in await db.list_collections(filter={"name": "ws_events"})][0]
        assert info["options"].get("capped") is True and info["options"].get("max") == 50000
        assert await db.ws_events.count_documents({}) == 0
        assert any(list(ix.get("key")) == [("seq", 1)] for ix in (await db.ws_events.index_information()).values())
        await m.ensure_collection()   # idempotent: still capped, nothing dropped
        await db.ws_events.insert_one({"seq": 1, "type": "x", "data": {}})
        await m.ensure_collection()
        assert await db.ws_events.count_documents({}) == 1
        await db.client.drop_database(db.name)
    asyncio.run(run())
