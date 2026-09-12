"""
Lightweight WebSocket hub. Singleton.
Backend code calls `hub.broadcast(event_type, data)` and all connected
clients receive the JSON-encoded message.
"""
import asyncio
import json
import logging
from fastapi import WebSocket

logger = logging.getLogger("ws_hub")


class WSHub:
    def __init__(self):
        self.clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self.mirror = None  # async (event_type, data) -> None; set by server.py when the pod singleton is wired

    async def connect(self, ws: WebSocket):
        await ws.accept()
        async with self._lock:
            self.clients.add(ws)
        logger.info(f"WS connected (n={len(self.clients)})")

    async def disconnect(self, ws: WebSocket):
        async with self._lock:
            self.clients.discard(ws)
        try:
            await ws.close()
        except Exception:
            pass
        logger.info(f"WS disconnected (n={len(self.clients)})")

    # Raw launches run at hundreds/min; the dashboard only needs CANDIDATES (tens/min). `launch` / `launch_update`
    # become `candidate` / `candidate_update` and are dropped unless the payload (merged with what we last sent for
    # that id) is a candidate. A mint that turns into a skip gets one final `candidate_update` so the UI can drop it.
    CANDIDATE_ACTIONS = {"scalp", "hunt", "greylist_snipe", "reentry", "manual"}
    PENDING_MIN_BUYERS = 5
    _seen: dict = {}          # launch id → last merged payload we broadcast

    @classmethod
    def is_candidate(cls, d: dict) -> bool:
        if d.get("entered") or d.get("scanner_eligible"):
            return True
        a = d.get("classifier_action")
        if a in cls.CANDIDATE_ACTIONS:
            return True
        return a == "pending" and int(d.get("unique_buyers") or 0) >= cls.PENDING_MIN_BUYERS

    def _gate_launch(self, event_type: str, data):
        lid = data.get("id")
        merged = {**(self._seen.get(lid) or {}), **data} if lid else dict(data)
        was = lid in self._seen
        ok = self.is_candidate(merged)
        if ok:
            if lid:
                self._seen[lid] = merged
                if len(self._seen) > 600:
                    for k in list(self._seen)[:100]:
                        self._seen.pop(k, None)
            return ("candidate" if event_type == "launch" or not was else "candidate_update"), (merged if not was else data)
        if was:
            self._seen.pop(lid, None)
            return "candidate_update", {**data, "dropped": True}
        return None, None

    async def broadcast(self, event_type: str, data, *, mirror: bool = True):
        if event_type in ("launch", "launch_update") and isinstance(data, dict):
            event_type, data = self._gate_launch(event_type, data)
            if event_type is None:
                return
        if mirror and self.mirror is not None:
            # leader → capped ws_events collection; follower pods tail it into their own sockets
            await self.mirror(event_type, data)
        if not self.clients:
            return
        msg = json.dumps({"type": event_type, "data": data}, default=str)
        dead: list[WebSocket] = []
        for ws in list(self.clients):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self.clients.discard(ws)


hub = WSHub()
