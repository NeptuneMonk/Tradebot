"""
Pump.fun launch/trade firehose — a logsSubscribe channel on the single shared Solana WSS (account_event_bus).
Detects Create + Trade events for Pump.fun and emits them.
"""
import os
import json
import asyncio
import time
import base64
import struct
import hashlib
import logging

from pumpfun import PUMP_PROGRAM_ID, CREATE_DISCRIMINATOR

logger = logging.getLogger("listener")



def _anchor_event_disc(name: str) -> bytes:
    return hashlib.sha256(f"event:{name}".encode()).digest()[:8]


TRADE_EVENT_DISC = _anchor_event_disc("TradeEvent")
CREATE_EVENT_DISC = _anchor_event_disc("CreateEvent")


def parse_create_event(raw: bytes) -> dict | None:
    """Anchor CreateEvent layout (best-effort)."""
    try:
        offset = 8  # event discriminator

        def read_str(buf, off):
            length = struct.unpack_from("<I", buf, off)[0]
            off += 4
            s = buf[off : off + length].decode("utf-8", errors="replace")
            return s, off + length

        name, offset = read_str(raw, offset)
        symbol, offset = read_str(raw, offset)
        uri, offset = read_str(raw, offset)
        mint = raw[offset : offset + 32]
        offset += 32
        bonding_curve = raw[offset : offset + 32]
        offset += 32
        if len(mint) != 32 or len(bonding_curve) != 32 or len(raw) < offset + 32:
            return None  # truncated event → would yield mint='' / creator=1111… phantom launches
        user = raw[offset : offset + 32]

        import base58
        return {
            "name": name,
            "symbol": symbol,
            "uri": uri,
            "mint": base58.b58encode(mint).decode("utf-8"),
            "bonding_curve": base58.b58encode(bonding_curve).decode("utf-8"),
            "creator": base58.b58encode(user).decode("utf-8"),
        }
    except Exception as e:
        logger.debug(f"parse_create_event failed: {e}")
        return None


def parse_trade_event(raw: bytes) -> dict | None:
    """Anchor TradeEvent layout (after 8-byte disc): mint(32) sol(u64) tok(u64) isBuy(1) user(32) ts(i64) vsr(u64) vtr(u64)."""
    try:
        if len(raw) < 8 + 105:
            return None
        offset = 8
        mint = raw[offset : offset + 32]
        offset += 32
        sol_amount, tok_amount = struct.unpack_from("<QQ", raw, offset)
        offset += 16
        is_buy = bool(raw[offset])
        offset += 1
        user = raw[offset : offset + 32]
        offset += 32
        ts, vsr, vtr = struct.unpack_from("<qQQ", raw, offset)
        import base58
        return {
            "mint": base58.b58encode(mint).decode("utf-8"),
            "sol_amount": sol_amount,
            "token_amount": tok_amount,
            "is_buy": is_buy,
            "user": base58.b58encode(user).decode("utf-8"),
            "timestamp": ts,
            "virtual_sol_reserves": vsr,
            "virtual_token_reserves": vtr,
        }
    except Exception as e:
        logger.debug(f"parse_trade_event failed: {e}")
        return None


class PumpFunListener:
    """Pump.fun program firehose: a `logsSubscribe` channel on the ONE shared Solana WSS (account_event_bus).
    Parses Create / Trade events out of each notification and emits them. Health / kick / disconnect delegate to
    the shared socket, so the SOL FEED status reflects the single real connection."""

    def __init__(self, on_launch, on_trade=None):
        self.on_launch = on_launch  # async callable(launch_dict)
        self.on_trade = on_trade  # async callable(trade_dict) — optional
        self.mentions = str(PUMP_PROGRAM_ID)

    @property
    def _bus(self):
        from account_event_bus import account_event_bus
        return account_event_bus

    def start(self):
        self._bus.subscribe_logs(self.mentions, self._handle_message)
        self._bus.start()

    def stop(self):
        self._bus.unsubscribe_logs(self.mentions)

    def kick(self):
        """Reconnect immediately (feed toggled ON / bot started) instead of waiting out the backoff."""
        self._bus.subscribe_logs(self.mentions, self._handle_message)
        self._bus.kick()

    async def disconnect(self):
        """Close the shared socket now (gate OFF) — `connected` flips false immediately, the loop idles on the gate."""
        await self._bus.disconnect()

    def health(self) -> dict:
        return self._bus.health()

    @property
    def connected(self) -> bool:
        return self._bus.connected

    @property
    def last_error(self) -> str | None:
        return self._bus.last_error

    @property
    def last_ok_ts(self) -> float:
        return self._bus.last_ok_ts

    @property
    def last_attempt_ts(self) -> float:
        return self._bus.last_attempt_ts

    @property
    def via(self) -> str | None:
        return self._bus.via

    async def _handle_message(self, raw):
        """`raw` is the parsed notification dict from the shared socket (a JSON string is accepted for tests)."""
        if isinstance(raw, dict):
            msg = raw
        else:
            try:
                msg = json.loads(raw)
            except Exception:
                return
        params = msg.get("params")
        if not params:
            return
        slot = (params.get("result", {}).get("context") or {}).get("slot")
        value = params.get("result", {}).get("value", {})
        logs = value.get("logs", []) or []
        signature = value.get("signature")
        if value.get("err") is not None:
            return

        has_create = any("Instruction: Create" in log for log in logs)

        # Walk all Program data payloads; classify by discriminator
        for line in logs:
            if not line.startswith("Program data: "):
                continue
            payload_b64 = line[len("Program data: ") :]
            try:
                raw_bytes = base64.b64decode(payload_b64)
            except Exception:
                continue
            if len(raw_bytes) < 8:
                continue
            disc = raw_bytes[:8]

            if disc == CREATE_EVENT_DISC or has_create:
                parsed = parse_create_event(raw_bytes)
                if parsed is not None and slot is not None:
                    parsed["creation_slot"] = slot
                if parsed:
                    parsed["signature"] = signature
                    try:
                        await self.on_launch(parsed)
                    except Exception as e:
                        logger.exception(f"on_launch failed: {e}")
                    # only one create per tx
                    has_create = False
                    continue

            if disc == TRADE_EVENT_DISC and self.on_trade:
                parsed = parse_trade_event(raw_bytes)
                if parsed is not None and slot is not None:
                    parsed["slot"] = slot
                if parsed:
                    parsed["signature"] = signature
                    try:
                        await self.on_trade(parsed)
                    except Exception as e:
                        logger.exception(f"on_trade failed: {e}")

