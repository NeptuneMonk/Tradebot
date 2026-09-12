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

    CANDIDATE_ACTIONS = CANDIDATE_ACTIONS | {"rh_pons", "rh_pons_paper", "rh_pons_reentry", "rh_pons_manual"}

    @classmethod
    def is_candidate(cls, d: dict) -> bool:
        if d.get("entered") or d.get("scanner_eligible"):
            return True
        a = d.get("classifier_action")
        if a in cls.CANDIDATE_ACTIONS:
            return True
        buyers = int(d.get("unique_buyers") or 0)
        if d.get("chain") == "rh":
            # RH rows are gated by rh_paper (`rh_gate`): pass → candidate; otherwise the same "hot enough to watch"
            # tier as Solana pending rows so the RH tab is never empty while the poller is alive
            return d.get("rh_gate") == "pass" or buyers >= cls.PENDING_MIN_BUYERS
        return a == "pending" and buyers >= cls.PENDING_MIN_BUYERS

    _raw: dict = {}           # launch id → full launch doc that was NOT a candidate yet (so a late qualifier arrives whole)

    def _gate_launch(self, event_type: str, data):
        lid = data.get("id")
        base = self._seen.get(lid) or self._raw.get(lid) or {}
        merged = {**base, **data} if lid else dict(data)
        was = lid in self._seen
        ok = self.is_candidate(merged)
        if not ok and lid and event_type == "launch":
            self._raw[lid] = merged
            if len(self._raw) > 2000:
                for k in list(self._raw)[:500]:
                    self._raw.pop(k, None)
        if ok:
            if lid:
                self._raw.pop(lid, None)
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
