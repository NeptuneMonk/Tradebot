"""One Helius standard websocket.

logsSubscribe with ``mentions`` for each leader (count capped by config max_leaders) and accountSubscribe
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

# How long we wait for every logsSubscribe / accountSubscribe ack before treating
# the silent ones as a failure and subscribing again.
ACK_TIMEOUT_S = 8.0


def ws_url(api_key: str) -> str:
    return f"wss://mainnet.helius-rpc.com/?api-key={api_key}"


def logs_subscribe_params(wallet: str) -> list:
    """Helius requires ``mentions`` to be a list. A bare string is rejected with
    'Invalid params … expected a sequence'."""
    return [{"mentions": [wallet]}, {"commitment": "confirmed"}]


class HeliusWS:
    def __init__(self, api_key: str, on_logs, on_account, meter, on_connect=None):
        self.api_key = api_key
        self.on_logs = on_logs
        self.on_account = on_account
        self.on_connect = on_connect
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
        self._awaiting_acks = False
        self._ack_deadline = 0.0
        self._ack_ok_logs = 0
        self._ack_ok_accounts = 0
        self._ack_errors = 0

    def set_leaders(self, leaders: list[str]) -> None:
        self.leaders = list(leaders)  # cap enforced by config.max_leaders
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
            self._resub = False
            log.info("helius websocket up (%d leaders, %d accounts)", len(self.leaders), len(self.accounts))
            if self.on_connect is not None:
                asyncio.create_task(self._safe(self.on_connect()))
            while not self._stop:
                if self._resub:
                    self._resub = False
                    await self._unsubscribe_all(ws)
                    await self._subscribe_all(ws)
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    self.check_ack_timeout(time.time())
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
        self._pending.clear()
        self._ack_ok_logs = 0
        self._ack_ok_accounts = 0
        self._ack_errors = 0
        sent = 0
        for leader in self.leaders:
            await self._send(ws, "logsSubscribe", logs_subscribe_params(leader), ("logs", leader))
            sent += 1
        for account in sorted(self.accounts):
            await self._send(
                ws, "accountSubscribe",
                [account, {"commitment": "confirmed", "encoding": "base64"}],
                ("account", account),
            )
            sent += 1
        self._awaiting_acks = True
        self._ack_deadline = time.time() + ACK_TIMEOUT_S
        if sent == 0:
            self._finish_acks_if_ready()

    async def _unsubscribe_all(self, ws) -> None:
        for sub_id, (kind, _key) in list(self._subs.items()):
            method = "logsUnsubscribe" if kind == "logs" else "accountUnsubscribe"
            try:
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": self._next, "method": method, "params": [sub_id]}))
                self._next += 1
            except Exception:
                return
        self._subs.clear()

    def check_ack_timeout(self, now: float) -> bool:
        """Missing subscribe acks are a failure: log them and subscribe again."""
        if not self._awaiting_acks or now < self._ack_deadline or not self._pending:
            return False
        log.warning("subscribe acks missing (%d pending); resubscribing", len(self._pending))
        self._awaiting_acks = False
        self._pending.clear()
        self._resub = True
        return True

    def _finish_acks_if_ready(self) -> None:
        if not self._awaiting_acks or self._pending:
            return
        self._awaiting_acks = False
        if self._ack_errors:
            log.warning("subscribe failed (%d errors); resubscribing", self._ack_errors)
            self._resub = True
            return
        log.info("subscriptions confirmed: %d logs, %d accounts", self._ack_ok_logs, self._ack_ok_accounts)

    async def _on_message(self, msg: dict) -> None:
        # JSON-RPC errors have ``error`` and no ``result``. Dropping those replies
        # hid the mentions-must-be-a-list rejection.
        if "id" in msg and "method" not in msg and ("result" in msg or "error" in msg):
            self._on_rpc_response(msg)
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

    def _on_rpc_response(self, msg: dict) -> None:
        tag = self._pending.pop(msg["id"], None)
        err = msg.get("error")
        if err:
            message = err.get("message") if isinstance(err, dict) else str(err)
            log.warning("helius subscribe error: %s", message or err)
            if tag is not None:
                self._ack_errors += 1
                self._finish_acks_if_ready()
            return
        result = msg.get("result")
        if tag is not None and isinstance(result, int):
            self._subs[result] = tag
            if tag[0] == "logs":
                self._ack_ok_logs += 1
            elif tag[0] == "account":
                self._ack_ok_accounts += 1
        if tag is not None:
            self._finish_acks_if_ready()

    async def _safe(self, coro) -> None:
        try:
            await coro
        except Exception:
            log.exception("websocket handler failed")
