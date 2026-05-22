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

    async def broadcast(self, event_type: str, data):
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
