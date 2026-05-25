"""
AccountEventBus — single persistent Helius WSS connection multiplexing many
`accountSubscribe` calls.

Purpose: replace the per-position 0.4-0.8s polling loop in
`BotState._monitor_position` with push-based wakes. When a buy/sell lands on
a tracked bonding curve / PumpSwap pool, Helius pushes the new account state
to us within ~50-150ms (vs the 400-800ms polling worst case). Reduces RPC
credit burn by ~10x and tightens SL/TP latency to roughly one network RTT.

Design:
  - One persistent WSS to wss://mainnet.helius-rpc.com.
  - Subscribers call `bus.subscribe(account_pubkey_str)` and receive an
    `asyncio.Event`. The Event fires every time Helius pushes an update
    for that account.
  - Subscribers use the standard `event.clear()` / `asyncio.wait_for(event.wait(), timeout)`
    pattern — exactly the same shape as `asyncio.sleep(0.8)` was before,
    so the calling loop body stays identical.
  - On disconnect: reconnect with exponential backoff, then re-issue all
    active subscriptions automatically. The waiting tasks just see a longer
    silence and fall through their safety-net timeout — no data loss.
  - One `accountUnsubscribe` is sent best-effort when a subscriber calls
    `unsubscribe(account)`, but a dead subscriber is harmless: at worst we
    keep getting pushes for an account no one waits on.

Operator notes:
  - Helius docs require pings or activity within 10 min. We let
    `websockets.connect(ping_interval=20)` handle this.
  - `accountSubscribe` does NOT require Developer plan (standard Solana
    method) so this works on every Helius tier.
  - Reconnection logic is essential — we follow Helius's recommended
    exponential-backoff pattern, capped at 30s.
"""
import asyncio
import json
import logging
import os
import time
from typing import Optional

import websockets

logger = logging.getLogger("account_event_bus")

WSS_URL = os.environ["HELIUS_WSS_URL"]


class AccountEventBus:
    """Singleton-style bus. Use `account_event_bus` global below."""

    def __init__(self):
        # account_pubkey_str -> asyncio.Event (fires on every push)
        self._events: dict[str, asyncio.Event] = {}
        # account_pubkey_str -> Helius subscription id (returned by accountSubscribe)
        self._wss_sub_ids: dict[str, int] = {}
        # rpc_request_id -> account_pubkey_str (resolves the async subscribe ACK)
        self._pending_acks: dict[int, str] = {}
        self._task: Optional[asyncio.Task] = None
        self._ws = None
        self._next_id = 10_000
        self._stop = False
        # Set when the WSS handshake completes and is ready to accept subscribes
        self._connected = asyncio.Event()
        # Diagnostic counters (exposed via /api/diagnostics/account-bus)
        self.stats = {
            "events_received": 0,
            "subscribes_sent": 0,
            "reconnects": 0,
            "last_event_ts": 0.0,
            "connected_since": 0.0,
        }

    # ---------------- Public API ----------------

    def start(self):
        if self._task is None or self._task.done():
            self._stop = False
            self._task = asyncio.create_task(self._run())

    def stop(self):
        self._stop = True
        if self._task:
            self._task.cancel()
        self._connected.clear()

    def subscribe(self, account: str) -> asyncio.Event:
        """Return an Event that fires on every push for `account`.

        Idempotent — calling twice returns the same Event so multiple
        subscribers can share one WSS subscription. The Event starts
        cleared; the caller decides when to clear after consuming."""
        if account in self._events:
            return self._events[account]
        event = asyncio.Event()
        self._events[account] = event
        # Fire-and-forget the subscribe send; if WSS isn't up yet,
        # `_resubscribe_all` will retry on reconnect.
        asyncio.create_task(self._send_subscribe(account))
        return event

    def unsubscribe(self, account: str):
        """Drop the Event + best-effort unsubscribe on the wire. Safe to call
        even if not currently subscribed. We DON'T drop the wss_sub_id
        eagerly because the ACK may be in flight — let reconnect cleanup
        handle drift if it happens."""
        self._events.pop(account, None)
        sub_id = self._wss_sub_ids.pop(account, None)
        if sub_id is not None and self._ws is not None and self._connected.is_set():
            req_id = self._next_id
            self._next_id += 1
            asyncio.create_task(self._ws_send_safe({
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "accountUnsubscribe",
                "params": [sub_id],
            }))

    # ---------------- Internal ----------------

    async def _ws_send_safe(self, payload: dict):
        try:
            if self._ws is not None:
                await self._ws.send(json.dumps(payload))
        except Exception as e:
            logger.debug(f"ws send failed (will retry on reconnect): {e}")

    async def _send_subscribe(self, account: str):
        """Send accountSubscribe for `account` IF we're connected. Otherwise
        it'll be sent on reconnect via `_resubscribe_all`."""
        if not self._connected.is_set():
            return
        req_id = self._next_id
        self._next_id += 1
        self._pending_acks[req_id] = account
        self.stats["subscribes_sent"] += 1
        try:
            from helius_budget import record_ws_subscribe
            record_ws_subscribe()
        except Exception:
            pass
        await self._ws_send_safe({
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "accountSubscribe",
            "params": [
                account,
                {"encoding": "base64", "commitment": "confirmed"},
            ],
        })

    async def _resubscribe_all(self):
        """Re-issue accountSubscribe for every tracked account after a
        reconnect. Clears stale subscription ids since they're owned by
        the old (now-dead) WSS session."""
        self._wss_sub_ids.clear()
        for account in list(self._events.keys()):
            await self._send_subscribe(account)

    async def _run(self):
        backoff = 1
        while not self._stop:
            try:
                logger.info("AccountEventBus connecting to Helius WSS…")
                async with websockets.connect(
                    WSS_URL,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=4 * 1024 * 1024,
                ) as ws:
                    self._ws = ws
                    self._connected.set()
                    self.stats["connected_since"] = time.time()
                    backoff = 1
                    await self._resubscribe_all()
                    async for raw in ws:
                        if self._stop:
                            break
                        await self._handle_message(raw)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(
                    f"AccountEventBus WSS error: {type(e).__name__}: {e}; "
                    f"reconnecting in {backoff}s"
                )
                self.stats["reconnects"] += 1
            finally:
                self._ws = None
                self._connected.clear()
            if self._stop:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def _handle_message(self, raw):
        # Bill the inbound message before any parsing — even if it's something
        # we ignore, Helius charged us for the bytes.
        try:
            from helius_budget import record_ws_message
            record_ws_message(len(raw) if isinstance(raw, (str, bytes)) else 0)
        except Exception:
            pass
        try:
            msg = json.loads(raw)
        except Exception:
            return
        # Subscription ACK shape: {"id": req_id, "result": <int sub_id>}
        if "id" in msg and "result" in msg and isinstance(msg.get("result"), int):
            account = self._pending_acks.pop(msg["id"], None)
            if account is not None:
                self._wss_sub_ids[account] = msg["result"]
            return
        # Notification shape:
        # {"method": "accountNotification",
        #  "params": {"subscription": <int>, "result": {"context": {...},
        #                                                "value": {...}}}}
        if msg.get("method") != "accountNotification":
            return
        params = msg.get("params") or {}
        sub_id = params.get("subscription")
        if sub_id is None:
            return
        # Reverse-lookup account by sub_id
        account = None
        for acc, sid in self._wss_sub_ids.items():
            if sid == sub_id:
                account = acc
                break
        if not account:
            return
        event = self._events.get(account)
        if event is None:
            return
        self.stats["events_received"] += 1
        self.stats["last_event_ts"] = time.time()
        event.set()

    # ---------------- Helpers for callers ----------------

    async def wait_for_change(self, account: str, timeout: float) -> bool:
        """Convenience: wait for the next push on `account` OR `timeout`
        seconds, whichever comes first. Returns True if a push arrived,
        False if we timed out. The Event is auto-cleared after consumption
        so the next call will wait for the NEXT push.

        Drop-in replacement for `await asyncio.sleep(timeout)` — if the
        bus is down or no subscription exists, the caller still wakes up
        after `timeout`."""
        event = self._events.get(account)
        if event is None:
            await asyncio.sleep(timeout)
            return False
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            event.clear()
            return True
        except asyncio.TimeoutError:
            return False


# Global singleton — `BotState` calls `.start()` from its lifespan hook
account_event_bus = AccountEventBus()
