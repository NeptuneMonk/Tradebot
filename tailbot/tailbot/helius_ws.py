"""One Helius standard websocket.

logsSubscribe with ``mentions`` for each leader (at most 3) and accountSubscribe
for the curve or quote vault of an open position. transactionSubscribe is not
used — it is not on the free plan. Reconnects with backoff. ``feed_is_down`` is
true after ``feed_down_s`` without a live socket, which pauses new buys.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time

import websockets

log = logging.getLogger("tailbot.ws")


def ws_url(api_key: str) -> str:
    return f"wss://mainnet.helius-rpc.com/?api-key={api_key}"


class HeliusWS:
    def __init__(self, api_key: str, on_logs, on_account, meter):
        self.api_key = api_key
        self.on_logs = on_logs
        self.on_account = on_account
        self.meter = meter
        self.leaders: list[str] = []
        self.accounts: set[str] = set()
        self.connected = False
        self.down_since: float | None = None
        self._stop = False
        self._resub = False
        self._subs: dict[int, tuple[str, str]] = {}
        self._pending: dict[int, tuple[str, str]] = {}
        self._next = 1

    def set_leaders(self, leaders: list[str]) -> None:
        self.leaders = list(leaders)[:3]
        self._resub = True

    def set_accounts(self, accounts: set[str]) -> None:
        accounts = set(accounts)
        if accounts != self.accounts:
            self.accounts = accounts
            self._resub = True

    def feed_is_down(self, now: float, limit_s: float) -> bool:
        if self.connected or self.down_since is None:
            return False
        return now - self.down_since >= limit_s

    def stop(self) -> None:
        self._stop = True

    async def run(self) -> None:
        self.down_since = time.time()
        if not self.api_key:
            log.error("HELIUS_API_KEY is empty; websocket not started")
            while not self._stop:
                await asyncio.sleep(30)
            return
        backoff = 1.0
        while not self._stop:
            try:
                await self._session()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("helius websocket closed (%s)", type(exc).__name__)
            self.connected = False
            if self.down_since is None:
                self.down_since = time.time()
            await asyncio.sleep(backoff)
            backoff = min(30.0, backoff * 2)

    async def _session(self) -> None:
        async with websockets.connect(ws_url(self.api_key), ping_interval=20, ping_timeout=20, max_size=8_000_000) as ws:
            self.connected = True
            self.down_since = None
            self._subs.clear()
            self._pending.clear()
            await self._subscribe_all(ws)
            log.info("helius websocket up (%d leaders, %d accounts)", len(self.leaders), len(self.accounts))
            while not self._stop:
                if self._resub:
                    self._resub = False
                    await self._unsubscribe_all(ws)
                    await self._subscribe_all(ws)
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    continue
                size = len(raw) if isinstance(raw, (bytes, str)) else 0
                self.meter.add_ws(size)
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                await self._on_message(msg)

    async def _send(self, ws, method: str, params: list, tag: tuple[str, str]) -> None:
        self._next += 1
        req_id = self._next
        self._pending[req_id] = tag
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}))

    async def _subscribe_all(self, ws) -> None:
        for leader in self.leaders:
            await self._send(ws, "logsSubscribe", [{"mentions": leader}, {"commitment": "confirmed"}], ("logs", leader))
        for account in sorted(self.accounts):
            await self._send(
                ws, "accountSubscribe",
                [account, {"commitment": "confirmed", "encoding": "base64"}],
                ("account", account),
            )

    async def _unsubscribe_all(self, ws) -> None:
        for sub_id, (kind, _key) in list(self._subs.items()):
            method = "logsUnsubscribe" if kind == "logs" else "accountUnsubscribe"
            try:
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": self._next, "method": method, "params": [sub_id]}))
                self._next += 1
            except Exception:
                return
        self._subs.clear()

    async def _on_message(self, msg: dict) -> None:
        if "id" in msg and "result" in msg and not isinstance(msg.get("result"), dict):
            tag = self._pending.pop(msg["id"], None)
            if tag is not None and isinstance(msg.get("result"), int):
                self._subs[msg["result"]] = tag
            if msg.get("error"):
                log.warning("helius subscribe error: %s", msg["error"].get("message") or msg["error"])
            return
        params = msg.get("params") or {}
        tag = self._subs.get(params.get("subscription"))
        if not tag:
            return
        result = params.get("result") or {}
        value = result.get("value") or {}
        if msg.get("method") == "logsNotification" and tag[0] == "logs":
            asyncio.create_task(self._safe(self.on_logs(tag[1], value)))
        elif msg.get("method") == "accountNotification" and tag[0] == "account":
            data = value.get("data")
            if isinstance(data, list) and data and data[0]:
                try:
                    raw = base64.b64decode(data[0])
                except Exception:
                    return
                asyncio.create_task(self._safe(self.on_account(tag[1], raw)))

    async def _safe(self, coro) -> None:
        try:
            await coro
        except Exception:
            log.exception("websocket handler failed")
