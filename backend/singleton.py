"""One bot per deployment, however many pods the platform runs.

* LeaderLease — Mongo lease (`leader_lease/_id=leader`, TTL 30 s, renewed every 10 s). Only the leader runs the
  trading loops; losing the lease stops them in-process (no process kill). `is_leader_now()` re-reads the lease
  and is the send fence for every entry/exit.
* CommandRelay — a follower parks any /api request it cannot answer from RAM in `pod_commands`; the leader
  executes it against its own ASGI app and writes the reply. No pod IPs, no reverse proxy: Mongo is the bus.
* WS mirror — the leader appends every hub event to the capped `ws_events` collection; followers tail it and
  fan out to their own sockets, so a client on any replica sees the same feed.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import socket
import time
import uuid
from typing import Awaitable, Callable

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

LEASE_TTL_S = 30.0
RENEW_S = 10.0
POD_STALE_S = 60.0
RELAY_TIMEOUT_S = 12.0
RELAY_POLL_S = 0.15
EXEC_POLL_S = 0.25
WS_TAIL_S = 0.3
HOST = socket.gethostname()
POD_ID = f"{HOST}-{os.getpid()}-{uuid.uuid4().hex[:4]}"


class LeaderLease:
    def __init__(self, db, *, on_gain: Callable[[], Awaitable[None]], on_loss: Callable[[str], Awaitable[None]],
                 pod_id: str = POD_ID, ttl_s: float = LEASE_TTL_S, renew_s: float = RENEW_S):
        self.db, self.pod_id, self.ttl_s, self.renew_s = db, pod_id, ttl_s, renew_s
        self.on_gain, self.on_loss = on_gain, on_loss
        self.is_leader = False
        self.leader_id: str | None = None
        self.leader_heartbeat = 0.0
        self.pods: list[dict] = []
        self.two_leaders = False
        self.since: float | None = None
        self.started_at = time.time()
        self.stats = {"gains": 0, "losses": 0, "renew_failures": 0, "ticks": 0}
        self._last_ok = 0.0
        self._task: asyncio.Task | None = None

    # ---- lease primitives ----
    async def try_acquire(self) -> bool:
        now = time.time()
        fields = {"holder": self.pod_id, "expires_at": now + self.ttl_s, "heartbeat": now, "acquired_at": now, "host": HOST}
        try:
            res = await self.db.leader_lease.update_one(
                {"_id": "leader", "$or": [{"expires_at": {"$lt": now}}, {"holder": self.pod_id}]}, {"$set": fields})
            if res.matched_count:
                return True
            if await self.db.leader_lease.count_documents({"_id": "leader"}) == 0:
                try:
                    await self.db.leader_lease.insert_one({"_id": "leader", **fields})
                    return True
                except Exception:
                    return False          # duplicate key: another pod won the race
            return False
        except Exception as e:
            logger.warning(f"leader lease acquire failed: {e}")
            return False

    async def renew(self) -> bool:
        now = time.time()
        res = await self.db.leader_lease.update_one(
            {"_id": "leader", "holder": self.pod_id, "expires_at": {"$gt": now}},
            {"$set": {"expires_at": now + self.ttl_s, "heartbeat": now}})
        return res.matched_count == 1

    async def is_leader_now(self) -> bool:
        """Send fence: the in-memory flag is not enough during an overlap — re-read the lease."""
        if not self.is_leader:
            return False
        try:
            doc = await self.db.leader_lease.find_one({"_id": "leader"})
        except Exception as e:
            logger.warning(f"fence read failed ({e}) — treating as not leader")
            return False
        return bool(doc and doc.get("holder") == self.pod_id and float(doc.get("expires_at") or 0) > time.time())

    def leader_alive(self) -> bool:
        return self.leader_id is not None and time.time() - self.leader_heartbeat < self.ttl_s + 5.0

    # ---- loop ----
    async def tick(self):
        now = time.time()
        self.stats["ticks"] += 1
        if self.is_leader:
            try:
                ok = await self.renew()
            except Exception as e:
                logger.warning(f"lease renew error: {e}")
                ok = time.time() - self._last_ok < self.ttl_s      # Mongo blip: keep going until the lease would have expired
            if ok:
                self._last_ok = time.time()
            else:
                self.stats["renew_failures"] += 1
                await self._lose("lease renew failed or taken by another pod")
        elif await self.try_acquire():
            self._last_ok = time.time()
            await self._gain()
        try:
            doc = await self.db.leader_lease.find_one({"_id": "leader"})
            live = bool(doc and float(doc.get("expires_at") or 0) > now)
            self.leader_id = doc.get("holder") if live else None
            self.leader_heartbeat = float(doc.get("heartbeat") or 0) if live else 0.0
            await self.db.pods.update_one({"_id": self.pod_id}, {"$set": {
                "seen_at": now, "role": "leader" if self.is_leader else "follower", "host": HOST,
                "started_at": self.started_at, "pid": os.getpid()}}, upsert=True)
            self.pods = [p async for p in self.db.pods.find({"seen_at": {"$gt": now - POD_STALE_S}}).sort("started_at", 1)]
            fresh = now - (self.renew_s * 2 + 5)     # a pod that stopped heartbeating cannot think it is the leader
            leaders = [p["_id"] for p in self.pods if p.get("role") == "leader" and float(p.get("seen_at") or 0) > fresh]
            self.two_leaders = len(leaders) > 1
            if self.is_leader and self.leader_id and self.leader_id != self.pod_id:
                await self._lose(f"lease is held by {self.leader_id}")
            if self.stats["ticks"] % 30 == 0:
                await self.db.pods.delete_many({"seen_at": {"$lt": now - 3600}})
        except Exception as e:
            logger.debug(f"lease bookkeeping error: {e}")

    async def _gain(self):
        self.is_leader, self.since = True, time.time()
        self.stats["gains"] += 1
        logger.warning(f"LEADER: {self.pod_id} holds the bot lease — starting trading loops")
        try:
            await self.on_gain()
        except Exception as e:
            logger.exception(f"on_gain failed: {e}")

    async def _lose(self, reason: str):
        if not self.is_leader:
            return
        self.is_leader = False
        self.stats["losses"] += 1
        logger.error(f"LEADERSHIP LOST ({reason}) — {self.pod_id} becomes a follower, stopping trading loops in-process")
        try:
            await self.on_loss(reason)
        except Exception as e:
            logger.exception(f"on_loss failed: {e}")

    async def release(self):
        """Graceful shutdown: hand the lease over immediately and drop this pod from the registry."""
        try:
            if self.is_leader:
                await self.db.leader_lease.update_one({"_id": "leader", "holder": self.pod_id}, {"$set": {"expires_at": 0.0}})
            await self.db.pods.delete_one({"_id": self.pod_id})
        except Exception:
            pass
        self.is_leader = False

    async def _loop(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"lease loop error: {e}")
            await asyncio.sleep(self.renew_s)

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    def stop(self):
        if self._task:
            self._task.cancel()

    def info(self) -> dict:
        now = time.time()
        return {
            "pod_id": self.pod_id, "host": HOST, "role": "leader" if self.is_leader else "follower",
            "leader_id": self.leader_id, "leader_alive": self.leader_alive(),
            "leader_seen_ago_s": round(now - self.leader_heartbeat, 1) if self.leader_heartbeat else None,
            "since": self.since, "two_leaders": self.two_leaders,
            "pods": [{"id": p["_id"], "role": p.get("role"), "host": p.get("host"), "seen_ago_s": round(now - float(p.get("seen_at") or 0), 1),
                      "up_s": round(now - float(p.get("started_at") or now), 0)} for p in self.pods],
            "pods_seen": len(self.pods), "stats": self.stats,
        }


class CommandRelay:
    """Follower side parks the request; leader side executes it in-process (ASGI transport) and replies via Mongo."""

    def __init__(self, db, lease: LeaderLease, app, *, resolve_user=None, issue_nonce=None):
        """resolve_user(request) -> user_id|None validates the caller on the follower; issue_nonce(user_id) -> str
        mints the one-shot header the leader's auth dependency accepts. Credentials never touch Mongo."""
        self.db, self.lease, self.app = db, lease, app
        self.resolve_user, self.issue_nonce = resolve_user, issue_nonce
        self._task: asyncio.Task | None = None
        self.stats = {"relayed": 0, "executed": 0, "timeouts": 0, "unauthorized": 0}

    async def submit(self, request: Request) -> Response:
        user_id = None
        if self.resolve_user is not None:
            user_id = await self.resolve_user(request)
            if not user_id:
                self.stats["unauthorized"] += 1
                return JSONResponse({"detail": "Not authenticated"}, status_code=401, headers={"X-Pod-Role": "follower"})
        body = await request.body()
        cid = uuid.uuid4().hex
        hdrs = {k: v for k, v in request.headers.items() if k.lower() in ("content-type", "accept")}
        await self.db.pod_commands.insert_one({
            "_id": cid, "method": request.method, "path": request.url.path, "query": request.url.query or "",
            "headers": hdrs, "user_id": user_id, "body": base64.b64encode(body).decode(), "created_at": time.time(),
            "status": "pending", "by": self.lease.pod_id})
        self.stats["relayed"] += 1
        deadline = time.time() + RELAY_TIMEOUT_S
        while time.time() < deadline:
            await asyncio.sleep(RELAY_POLL_S)
            doc = await self.db.pod_commands.find_one({"_id": cid, "status": "done"})
            if doc:
                return Response(content=base64.b64decode(doc.get("resp_body") or ""), status_code=int(doc.get("resp_code") or 500),
                                media_type=doc.get("resp_ct") or "application/json",
                                headers={"X-Pod-Role": "follower", "X-Pod-Relayed": "1", "X-Pod-Leader": str(doc.get("leader") or "")})
        self.stats["timeouts"] += 1
        await self.db.pod_commands.update_one({"_id": cid, "status": "pending"}, {"$set": {"status": "expired"}})
        return JSONResponse({"detail": "the leader pod did not answer in time — retry in a few seconds"}, status_code=503,
                            headers={"X-Pod-Role": "follower", "X-Pod-Relayed": "1"})

    async def _execute(self, client: httpx.AsyncClient, cmd: dict):
        try:
            url = cmd["path"] + (f"?{cmd['query']}" if cmd.get("query") else "")
            hdrs = {**cmd.get("headers", {}), "X-Pod-Relayed": "1"}
            if cmd.get("user_id") and self.issue_nonce is not None:
                hdrs["X-Pod-Exec"] = self.issue_nonce(cmd["user_id"])
            r = await client.request(cmd["method"], url, content=base64.b64decode(cmd.get("body") or ""),
                                     headers=hdrs, timeout=RELAY_TIMEOUT_S - 2)
            code, content, ct = r.status_code, r.content, r.headers.get("content-type")
        except Exception as e:
            code, content, ct = 500, f'{{"detail":"relay execution failed: {e}"}}'.encode(), "application/json"
        await self.db.pod_commands.update_one({"_id": cmd["_id"]}, {"$set": {
            "status": "done", "resp_code": code, "resp_body": base64.b64encode(content).decode(), "resp_ct": ct,
            "done_at": time.time(), "leader": self.lease.pod_id}})
        self.stats["executed"] += 1

    async def executor_loop(self):
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://leader.local") as client:
            n = 0
            while True:
                try:
                    if self.lease.is_leader:
                        now = time.time()
                        cur = self.db.pod_commands.find({"status": "pending", "created_at": {"$gt": now - RELAY_TIMEOUT_S}}).sort("created_at", 1).limit(25)
                        for cmd in [c async for c in cur]:
                            claim = await self.db.pod_commands.update_one({"_id": cmd["_id"], "status": "pending"},
                                                                          {"$set": {"status": "running", "leader": self.lease.pod_id}})
                            if claim.matched_count:
                                asyncio.create_task(self._execute(client, cmd))
                        n += 1
                        if n % 400 == 0:
                            await self.db.pod_commands.delete_many({"created_at": {"$lt": now - 600}})
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.debug(f"relay executor error: {e}")
                await asyncio.sleep(EXEC_POLL_S)

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.executor_loop())


class WSMirror:
    """Leader appends hub events to a capped collection; followers tail it into their own sockets."""

    def __init__(self, db, lease: LeaderLease, hub):
        self.db, self.lease, self.hub = db, lease, hub
        self._task: asyncio.Task | None = None
        self._last_seq = time.time_ns()
        self.stats = {"written": 0, "forwarded": 0}

    async def ensure_collection(self):
        """Guarantee `ws_events` is capped (50k docs / 32 MB) + indexed on `seq`. An uncapped copy (created by an
        insert racing the first create) grows unbounded (seen at 2.4M docs in prod) and makes every 0.3 s tail scan
        crawl — drop and recreate it: it is a transient mirror, nothing durable lives there."""
        try:
            infos = [c async for c in await self.db.list_collections(filter={"name": "ws_events"})]
            if infos and not (infos[0].get("options") or {}).get("capped"):
                n = await self.db.ws_events.estimated_document_count()
                await self.db.ws_events.drop()
                infos = []
                logger.warning(f"ws_events was NOT capped ({n} docs) — dropped and recreating as capped")
            if not infos:
                await self.db.create_collection("ws_events", capped=True, size=32 * 1024 * 1024, max=50000)
            await self.db.ws_events.create_index("seq")
            await self.db.pod_commands.create_index("created_at")
        except Exception as e:
            logger.warning(f"ws_events collection: {e}")

    async def write(self, event_type: str, data) -> None:
        if not self.lease.is_leader:
            return
        try:
            await self.db.ws_events.insert_one({"seq": time.time_ns(), "type": event_type, "data": data, "leader": self.lease.pod_id})
            self.stats["written"] += 1
        except Exception as e:
            logger.debug(f"ws mirror write: {e}")

    async def tail_once(self):
        cur = self.db.ws_events.find({"seq": {"$gt": self._last_seq}}).sort("seq", 1).limit(500)
        for ev in [e async for e in cur]:
            self._last_seq = max(self._last_seq, int(ev["seq"]))
            if self.hub.clients:
                await self.hub.broadcast(ev["type"], ev["data"], mirror=False)
                self.stats["forwarded"] += 1

    async def _loop(self):
        while True:
            try:
                if not self.lease.is_leader:
                    await self.tail_once()
                else:
                    self._last_seq = time.time_ns()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"ws mirror tail: {e}")
            await asyncio.sleep(WS_TAIL_S)

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
