"""
Pump.fun mempool listener via Helius WSS logsSubscribe.
Detects new token Create instructions and emits launch events.
"""
import os
import json
import asyncio
import base64
import struct
import logging
import websockets

from pumpfun import PUMP_PROGRAM_ID, CREATE_DISCRIMINATOR

logger = logging.getLogger("listener")

WSS_URL = os.environ["HELIUS_WSS_URL"]


def parse_create_event(data_b64: str) -> dict | None:
    """
    Parse the Anchor 'Program data:' payload emitted by Pump.fun create.
    Anchor event layout for CreateEvent (best-effort, varies by version):
      8  discriminator
      ?  name (string: u32 len + bytes)
      ?  symbol (string)
      ?  uri (string)
      32 mint
      32 bonding_curve
      32 user (creator)
    We parse defensively; on any failure return None.
    """
    try:
        raw = base64.b64decode(data_b64)
        # Skip the 8-byte event discriminator
        offset = 8

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
        # Optional fields — some versions have additional fields
        user = raw[offset : offset + 32] if len(raw) >= offset + 32 else b"\x00" * 32

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


class PumpFunListener:
    def __init__(self, on_launch):
        self.on_launch = on_launch  # async callable(launch_dict)
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
        while not self._stop:
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
                        await self._handle_message(raw)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.connected = False
                logger.warning(f"WSS connection error: {e}; retrying in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
        self.connected = False

    async def _handle_message(self, raw):
        try:
            msg = json.loads(raw)
        except Exception:
            return
        params = msg.get("params")
        if not params:
            return
        result = params.get("result", {})
        value = result.get("value", {})
        logs = value.get("logs", []) or []
        signature = value.get("signature")
        err = value.get("err")
        if err is not None:
            return

        # Look for create instruction signature
        has_create = any("Instruction: Create" in l for l in logs)
        if not has_create:
            return

        # Find Program data payload (next line after the Create)
        launch_data = None
        for i, line in enumerate(logs):
            if line.startswith("Program data: "):
                payload = line[len("Program data: ") :]
                parsed = parse_create_event(payload)
                if parsed:
                    launch_data = parsed
                    break

        if not launch_data:
            return

        launch_data["signature"] = signature
        try:
            await self.on_launch(launch_data)
        except Exception as e:
            logger.exception(f"on_launch handler failed: {e}")
