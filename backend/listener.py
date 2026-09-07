"""
Pump.fun mempool listener via Helius WSS logsSubscribe.
Detects Create + Trade events for Pump.fun and emits them.
"""
import os
import json
import asyncio
import base64
import struct
import hashlib
import logging
import websockets

from pumpfun import PUMP_PROGRAM_ID, CREATE_DISCRIMINATOR

logger = logging.getLogger("listener")

WSS_URL = os.environ["HELIUS_WSS_URL"]


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
    def __init__(self, on_launch, on_trade=None):
        self.on_launch = on_launch  # async callable(launch_dict)
        self.on_trade = on_trade  # async callable(trade_dict) — optional
        self._task: asyncio.Task | None = None
        self._stop = False
        self.connected = False

    def start(self):
        if self._task and not self._task.done():
            return
        self._stop = False
        self._task = asyncio.create_task(self._run())

    def stop(self):
        self._stop = True
        if self._task:
            self._task.cancel()
        self.connected = False

    async def _run(self):
        backoff = 1
        # Poll interval for the helius_gate when paused — we don't want to
        # busy-wait, but we also want toggling the switch ON to take effect
        # within a few seconds (not minutes).
        gate_check_interval_s = 5
        while not self._stop:
            # Helius kill switch — when the user toggles tracker OFF, we
            # disconnect (or never connect) and idle here. Polling every
            # 5s gets us back online quickly when the switch flips ON.
            try:
                from helius_gate import is_helius_paused
                if is_helius_paused():
                    if self.connected:
                        logger.info(
                            "Helius tracker disabled by user — keeping listener idle"
                        )
                    self.connected = False
                    await asyncio.sleep(gate_check_interval_s)
                    continue
            except Exception:
                pass
            try:
                logger.info("Connecting to Helius WSS for logsSubscribe...")
                async with websockets.connect(
                    WSS_URL, ping_interval=20, ping_timeout=20, max_size=4 * 1024 * 1024
                ) as ws:
                    self.connected = True
                    backoff = 1
                    sub_req = {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "logsSubscribe",
                        "params": [
                            {"mentions": [str(PUMP_PROGRAM_ID)]},
                            {"commitment": "processed"},
                        ],
                    }
                    await ws.send(json.dumps(sub_req))
                    logger.info("Subscribed to Pump.fun logs.")
                    async for raw in ws:
                        if self._stop:
                            break
                        # Re-check the gate inside the message loop too —
                        # the user could toggle OFF mid-stream and we want
                        # to drop the connection immediately rather than
                        # keep consuming credits until the next reconnect.
                        try:
                            from helius_gate import is_helius_paused
                            if is_helius_paused():
                                logger.info("Helius gate flipped OFF mid-stream — closing WSS")
                                break
                        except Exception:
                            pass
                        await self._handle_message(raw)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.connected = False
                logger.warning(f"WSS connection error: {e}; retrying in {backoff}s")
                # Chunked sleep — poll the helius gate every 1s during the
                # reconnect backoff. Without this, an OFF toggle issued
                # while the listener is in a 30s backoff would take up to
                # 30s to take effect; with chunking it bites within ~1s.
                for _ in range(backoff):
                    await asyncio.sleep(1)
                    try:
                        from helius_gate import is_helius_paused
                        if is_helius_paused():
                            break
                    except Exception:
                        pass
                backoff = min(backoff * 2, 30)
        self.connected = False

    async def _handle_message(self, raw):
        # Track Helius credit consumption per inbound bytes
        try:
            from helius_budget import record_ws_message
            record_ws_message(len(raw) if isinstance(raw, (str, bytes)) else 0)
        except Exception:
            pass
        try:
            msg = json.loads(raw)
        except Exception:
            return
        params = msg.get("params")
        if not params:
            return
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
                if parsed:
                    parsed["signature"] = signature
                    try:
                        await self.on_trade(parsed)
                    except Exception as e:
                        logger.exception(f"on_trade failed: {e}")

