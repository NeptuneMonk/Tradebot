"""
Pump.fun launch/trade firehose — a logsSubscribe channel on the single shared Solana WSS (account_event_bus).
Detects Create + Trade events for Pump.fun and emits them.
"""
import json
import base64
import os
import struct
import hashlib
import logging

import base58

from pumpfun import PUMP_PROGRAM_ID

logger = logging.getLogger("listener")



def _anchor_event_disc(name: str) -> bytes:
    return hashlib.sha256(f"event:{name}".encode()).digest()[:8]


TRADE_EVENT_DISC = _anchor_event_disc("TradeEvent")
CREATE_EVENT_DISC = _anchor_event_disc("CreateEvent")


# ---------- IDL-driven Anchor event decoding (tolerant: older/shorter events stop at the last field present) ----------
_IDL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pump_idl.json")
try:
    with open(_IDL_PATH) as _f:
        _IDL = json.load(_f)
    _IDL_TYPES = {t["name"]: t for t in _IDL.get("types", [])}
except Exception as _e:   # pragma: no cover
    _IDL, _IDL_TYPES = None, {}
    logger.warning(f"pump_idl.json unavailable ({_e}); falling back to fixed-layout parsers")


def _borsh_decode(fields: list, buf: bytes, off: int) -> tuple[dict, int]:
    out: dict = {}
    for f in fields:
        if off >= len(buf):
            break
        t = f["type"]
        if t == "string":
            n = struct.unpack_from("<I", buf, off)[0]
            off += 4
            out[f["name"]] = buf[off:off + n].decode("utf-8", errors="replace")
            off += n
        elif t == "pubkey":
            if off + 32 > len(buf):
                break
            out[f["name"]] = base58.b58encode(buf[off:off + 32]).decode()
            off += 32
        elif t == "u64":
            out[f["name"]] = struct.unpack_from("<Q", buf, off)[0]
            off += 8
        elif t == "i64":
            out[f["name"]] = struct.unpack_from("<q", buf, off)[0]
            off += 8
        elif t == "u16":
            out[f["name"]] = struct.unpack_from("<H", buf, off)[0]
            off += 2
        elif t == "bool":
            out[f["name"]] = bool(buf[off])
            off += 1
        elif t == "u8":
            out[f["name"]] = buf[off]
            off += 1
        elif isinstance(t, dict) and "vec" in t:
            n = struct.unpack_from("<I", buf, off)[0]
            off += 4
            inner = _IDL_TYPES[t["vec"]["defined"]["name"]]["type"]["fields"]
            items = []
            for _ in range(n):
                item, off = _borsh_decode(inner, buf, off)
                items.append(item)
            out[f["name"]] = items
        else:
            break
    return out, off


def _event_fields(name: str) -> list | None:
    t = _IDL_TYPES.get(name)
    return (t.get("type") or {}).get("fields") if t else None


CREATE_FIELDS = _event_fields("CreateEvent")
TRADE_FIELDS = _event_fields("TradeEvent")


def parse_create_event(raw: bytes) -> dict | None:
    """Anchor CreateEvent → {name, symbol, uri, mint, bonding_curve, creator, quote_mint, virtual_quote_reserves, token_program, is_mayhem_mode…}."""
    try:
        if CREATE_FIELDS:
            ev, _ = _borsh_decode(CREATE_FIELDS, raw, 8)
            mint, bc = ev.get("mint"), ev.get("bonding_curve")
            user = ev.get("user")
            if not mint or not bc or not user:
                return None  # truncated event → would yield mint='' / creator=1111… phantom launches
            ev["creator"] = user          # tx signer = launch creator (CreateEvent.creator is the fee creator, may differ)
            ev["signer_creator"] = user
            return ev
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
        if len(mint) != 32 or len(bonding_curve) != 32 or len(raw) < offset + 32:
            return None
        user = raw[offset : offset + 32]
        return {"name": name, "symbol": symbol, "uri": uri, "mint": base58.b58encode(mint).decode("utf-8"),
                "bonding_curve": base58.b58encode(bonding_curve).decode("utf-8"), "creator": base58.b58encode(user).decode("utf-8")}
    except Exception as e:
        logger.debug(f"parse_create_event failed: {e}")
        return None


def parse_trade_event(raw: bytes) -> dict | None:
    """Anchor TradeEvent → every IDL field present (mint, sol_amount, token_amount, is_buy, user, timestamp, virtual_sol_reserves,
    virtual_token_reserves, real_sol_reserves, …, quote_mint, quote_amount, virtual_quote_reserves, real_quote_reserves).
    Non-SOL-quoted coins report sol_amount / virtual_sol_reserves = 0 — quote_mints.QuoteBook.normalize() fills them in."""
    try:
        if len(raw) < 8 + 105:
            return None
        if TRADE_FIELDS:
            ev, _ = _borsh_decode(TRADE_FIELDS, raw, 8)
            if not ev.get("mint") or "virtual_token_reserves" not in ev:
                return None
            return ev
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
        return {"mint": base58.b58encode(mint).decode("utf-8"), "sol_amount": sol_amount, "token_amount": tok_amount, "is_buy": is_buy,
                "user": base58.b58encode(user).decode("utf-8"), "timestamp": ts, "virtual_sol_reserves": vsr, "virtual_token_reserves": vtr}
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

