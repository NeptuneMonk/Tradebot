"""
AccountEventBus — THE single persistent Solana WSS connection for this process.

One socket multiplexes every subscription we hold:
  - `logsSubscribe` channels (Pump.fun program firehose → listener.PumpFunListener)
  - `accountSubscribe` per open position (push-based wakes for _monitor_position)

Why one socket: Solana JSON-RPC WSS multiplexes any number of subscriptions per connection, and the
free public node caps connections per IP (HTTP 413 on the handshake) — two sockets per instance
plus a published twin was enough to trip it. One connection also means one reconnect/backoff path,
one quota/handshake failover path (solana_client.wss_router) and one operator-facing health record.

Design:
  - Subscribers call `bus.subscribe(account)` → asyncio.Event fired on every push (unchanged API).
  - `bus.subscribe_logs(mentions, handler)` registers a logs channel; on every (re)connect the bus
    issues `logsSubscribe` for it and routes `logsNotification` frames to `handler(raw)` inline
    (the handler is the hot path and must not block).
  - On disconnect: exponential backoff (cap 30s, 5 min when every provider's quota is gone), then
    every account + logs subscription is re-issued automatically.
  - Helius gate (feed switch OFF) keeps the socket closed; the loop polls the gate every 1s.
  - Health: `connected`, `last_error`, `last_ok_ts`, `last_attempt_ts`, `via` (fallback endpoint label).
"""
import asyncio
import base64
import json
import logging
import time
from typing import Optional

import websockets

logger = logging.getLogger("sol_wss")

from solana_client import wss_router, wss_label

QUOTA_BACKOFF_S = 300.0   # every provider plan exhausted: retry the WSS every 5 min, not 6×/s


def _is_quota_error(raw) -> bool:
    """Provider plan exhausted on the WSS (QuickNode -32003 'request limit reached', Helius 'max usage')."""
    if not isinstance(raw, (str, bytes)) or '"error"' not in (raw.decode(errors="ignore") if isinstance(raw, bytes) else raw):
        return False
    try:
        err = json.loads(raw).get("error") or {}
    except Exception:
        return False
    msg = str(err.get("message") or "").lower()
    return err.get("code") == -32003 or "limit reached" in msg or "max usage" in msg or "quota" in msg


class AccountEventBus:
    """Singleton-style bus. Use `account_event_bus` global below."""

    def __init__(self):
        # account_pubkey_str -> asyncio.Event (fires on every push)
        self._events: dict[str, asyncio.Event] = {}
        # account_pubkey_str -> Helius subscription id (returned by accountSubscribe)
        self._wss_sub_ids: dict[str, int] = {}
        # rpc_request_id -> account_pubkey_str (resolves the async subscribe ACK)
        self._pending_acks: dict[int, str] = {}
        self._latest: dict[str, tuple[bytes, float]] = {}
        # logs channels: mentions_pubkey -> async handler(raw); sub ids + pending acks tracked separately
        self._log_handlers: dict[str, object] = {}
        self._log_sub_ids: dict[str, int] = {}
        self._pending_log_acks: dict[int, str] = {}
        self._task: Optional[asyncio.Task] = None
        self._ws = None
        self._next_id = 10_000
        self._stop = False
        self._kick = False                      # skip the remaining backoff and reconnect now
        # Set when the WSS handshake completes and is ready to accept subscribes
        self._connected = asyncio.Event()
        # operator-facing health (surfaced as the SOL FEED status)
        self.last_error: str | None = None
        self.last_ok_ts: float = 0.0            # last successful connect + (re)subscribe
        self.last_attempt_ts: float = 0.0
        self.via: str | None = None             # fallback endpoint label when the primary is exhausted / rejecting
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

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def kick(self):
        """Reconnect now (feed toggled ON / bot started) instead of waiting out the backoff."""
        self._kick = True
        self.start()

    async def disconnect(self):
        """Close the live socket now (gate OFF) — `connected` flips false immediately, the loop idles on the gate."""
        self._connected.clear()
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    def health(self) -> dict:
        return {"connected": self.connected, "last_error": self.last_error, "last_ok_ts": self.last_ok_ts or None,
                "last_attempt_ts": self.last_attempt_ts or None, "task_alive": bool(self._task and not self._task.done()),
                "via": self.via, "logs_channels": list(self._log_handlers.keys()), "accounts": len(self._events)}

    def subscribe_logs(self, mentions: str, handler):
        """Register a `logsSubscribe` channel for txs mentioning `mentions`; `handler(raw)` gets every notification.
        Idempotent per `mentions` (the newest handler wins). Sent now if connected, else on the next connect."""
        fresh = mentions not in self._log_handlers
        self._log_handlers[mentions] = handler
        if fresh:
            asyncio.create_task(self._send_logs_subscribe(mentions))

    def unsubscribe_logs(self, mentions: str):
        self._log_handlers.pop(mentions, None)
        sub_id = self._log_sub_ids.pop(mentions, None)
        if sub_id is not None and self._ws is not None and self._connected.is_set():
            req_id = self._next_id
            self._next_id += 1
            asyncio.create_task(self._ws_send_safe({"jsonrpc": "2.0", "id": req_id, "method": "logsUnsubscribe", "params": [sub_id]}))

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
        self._latest.pop(account, None)
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

    async def _send_logs_subscribe(self, mentions: str):
        if not self._connected.is_set() or mentions not in self._log_handlers:
            return
        req_id = self._next_id
        self._next_id += 1
        self._pending_log_acks[req_id] = mentions
        await self._ws_send_safe({
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "logsSubscribe",
            "params": [{"mentions": [mentions]}, {"commitment": "processed"}],
        })

    async def _resubscribe_all(self):
        """Re-issue every logs channel + accountSubscribe after a (re)connect. Clears stale subscription ids since
        they're owned by the old (now-dead) WSS session. Logs first: the launch firehose is the hot path."""
        self._wss_sub_ids.clear()
        self._log_sub_ids.clear()
        self._pending_acks.clear()
        self._pending_log_acks.clear()
        for mentions in list(self._log_handlers.keys()):
            await self._send_logs_subscribe(mentions)
        for account in list(self._events.keys()):
            await self._send_subscribe(account)

    async def _sleep_gated(self, seconds: int) -> None:
        """Sleep in 1 s steps, waking early on a kick / stop or when the operator flips the feed OFF."""
        for _ in range(max(1, int(seconds))):
            await asyncio.sleep(1)
            if self._kick or self._stop:
                break
            try:
                from helius_gate import is_helius_paused
                if is_helius_paused():
                    break
            except Exception:
                pass

    async def _run(self):
        backoff = 1
        while not self._stop:
            # Feed switch OFF (or auto-paused) → socket stays closed; poll the gate every 1s so ON bites fast.
            try:
                from helius_gate import is_helius_paused, snapshot as gate_snapshot
                if is_helius_paused():
                    g = gate_snapshot()
                    if self._connected.is_set():
                        logger.info("Solana WSS paused by feed switch — socket idle")
                    self._connected.clear()
                    self._ws = None
                    self.last_error = "paused: operator switch OFF" if g["manual"] else f"paused: {g['auto_reason'] or 'auto'}"
                    await asyncio.sleep(1)
                    continue
            except Exception:
                pass
            url = wss_router.current()
            self._kick = False
            self.last_attempt_ts = time.time()
            try:
                logger.info(f"Solana WSS connecting ({wss_label(url)}) — {len(self._log_handlers)} logs channel(s), {len(self._events)} account(s)…")
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=4 * 1024 * 1024,
                ) as ws:
                    self._ws = ws
                    self._connected.set()
                    self.via = wss_label(url) if wss_router.on_fallback() else None
                    self.stats["connected_since"] = time.time()
                    backoff = 1
                    await self._resubscribe_all()
                    wss_router.mark_connected(url)
                    self.last_ok_ts = time.time()
                    self.last_error = None
                    logger.info(f"Solana WSS up ({wss_label(url)}): subscribed logs={list(self._log_handlers)} accounts={len(self._events)}")
                    async for raw in ws:
                        if self._stop:
                            break
                        # Re-check the gate inside the loop so a mid-stream toggle drops the connection immediately.
                        try:
                            from helius_gate import is_helius_paused
                            if is_helius_paused():
                                logger.info("Solana WSS: feed switch flipped OFF mid-stream — closing")
                                break
                        except Exception:
                            pass
                        if _is_quota_error(raw):
                            # provider plan exhausted — skip it until the feed is toggled OFF→ON, carry on the next endpoint
                            self._connected.clear()
                            if wss_router.mark_exhausted(url):
                                nxt = wss_router.current()
                                self.last_error = f"{wss_label(url)} quota exhausted — switching to {wss_label(nxt)}"
                                logger.error(f"Solana WSS quota exhausted ({wss_label(url)}): {raw[:120]} — switching to {wss_label(nxt)}")
                                self._kick = True
                            else:
                                self.last_error = "RPC plan quota exhausted on every WSS — feed idle, retrying every 5 min"
                                logger.error(f"Solana WSS quota exhausted on all endpoints ({raw[:120]}) — idle {QUOTA_BACKOFF_S:.0f}s")
                                await self._sleep_gated(int(QUOTA_BACKOFF_S))
                            break
                        await self._handle_message(raw)
                # clean close (server hung up) — back off like an error instead of reconnecting instantly
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {str(e)[:100]}" if str(e) else type(e).__name__
                logger.warning(f"Solana WSS error: {type(e).__name__}: {e}; reconnecting in {backoff}s")
                self.stats["reconnects"] += 1
                if "rejected WebSocket connection" in str(e):
                    nxt = wss_router.mark_rejected(url)
                    if nxt:
                        self.last_error = f"{wss_label(url)} rejected the handshake ({str(e).rsplit(':', 1)[-1].strip()}) — switching to {wss_label(nxt)}"
                        logger.error(self.last_error)
                        self._kick = True
            finally:
                self._ws = None
                self._connected.clear()
            if self._stop:
                break
            if self._kick:
                backoff = 1
                continue
            await self._sleep_gated(backoff)
            backoff = 1 if self._kick else min(backoff * 2, 30)
        self._connected.clear()

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
            mentions = self._pending_log_acks.pop(msg["id"], None)
            if mentions is not None:
                self._log_sub_ids[mentions] = msg["result"]
            return
        method = msg.get("method")
        if method == "logsNotification":
            sub_id = (msg.get("params") or {}).get("subscription")
            for mentions, sid in self._log_sub_ids.items():
                if sid == sub_id:
                    handler = self._log_handlers.get(mentions)
                    if handler is not None:
                        try:
                            await handler(msg)
                        except Exception as e:
                            logger.exception(f"logs handler failed: {e}")
                    return
            if len(self._log_handlers) == 1 and not self._log_sub_ids:   # ACK not seen yet (or lost): single channel → route anyway
                handler = next(iter(self._log_handlers.values()))
                try:
                    await handler(msg)
                except Exception as e:
                    logger.exception(f"logs handler failed: {e}")
            return
        # Notification shape:
        # {"method": "accountNotification",
        #  "params": {"subscription": <int>, "result": {"context": {...},
        #                                                "value": {...}}}}
        if method != "accountNotification":
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
        # Keep the pushed account bytes: the monitor decodes them locally instead of re-reading the account
        # over HTTP (one getAccountInfo saved per trade that lands on a watched curve).
        try:
            data = ((params.get("result") or {}).get("value") or {}).get("data")
            self._latest[account] = (base64.b64decode(data[0]) if data and data[0] else b"", time.time())
        except Exception:
            self._latest.pop(account, None)
        event.set()

    # ---------------- Helpers for callers ----------------

    def take_latest(self, account: str) -> bytes | None:
        """Account bytes from the most recent push, consumed once (None = nothing new since the last take).
        b"" means the account was closed."""
        hit = self._latest.pop(account, None)
        return hit[0] if hit else None

    def is_live(self, account: str) -> bool:
        """True when the WSS is up and this account's subscription has been ACKed — i.e. 'no push' really means
        'no on-chain change', so callers may stretch their safety-net poll."""
        return self._connected.is_set() and account in self._wss_sub_ids

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
