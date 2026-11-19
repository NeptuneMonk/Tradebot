"""Pump.fun TradeEvent and PumpSwap Buy/Sell event parsers.

Extracted from ``backend/listener.py`` (IDL-tolerant borsh walk of TradeEvent) and
``backend/pumpswap_feed.py`` (BuyEvent / SellEvent layout). Kept here so tailbot does
not import ``pumpfun.py`` / ``pumpswap.py``, which read program ids from the
environment and build signed transactions.
"""
from __future__ import annotations

import base64
import hashlib
import struct

from .b58 import b58encode, pubkey_bytes

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMPSWAP_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
SOL_MINT_ALIASES = {WSOL, "11111111111111111111111111111111", "", None}
STABLES = {USDC, USDT}

# Programs whose appearance in logs means the tx might be a swap. Anything else
# (a plain SOL transfer that merely mentions the leader) is ignored with no RPC.
INTERESTING_MARKERS = (
    PUMP_PROGRAM,
    PUMPSWAP_PROGRAM,
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium AMM v4
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",  # Orca Whirlpool
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",  # Raydium CLMM
)


def event_disc(name: str) -> bytes:
    return hashlib.sha256(f"event:{name}".encode()).digest()[:8]


TRADE_DISC = event_disc("TradeEvent")
BUY_DISC = event_disc("BuyEvent")
SELL_DISC = event_disc("SellEvent")

# TradeEvent field order from backend/pump_idl.json. The walk stops at the last
# field that fits, matching listener._borsh_decode.
TRADE_FIELDS: list[tuple[str, str]] = [
    ("mint", "pubkey"),
    ("sol_amount", "u64"),
    ("token_amount", "u64"),
    ("is_buy", "bool"),
    ("user", "pubkey"),
    ("timestamp", "i64"),
    ("virtual_sol_reserves", "u64"),
    ("virtual_token_reserves", "u64"),
    ("real_sol_reserves", "u64"),
    ("real_token_reserves", "u64"),
    ("fee_recipient", "pubkey"),
    ("fee_basis_points", "u64"),
    ("fee", "u64"),
    ("creator", "pubkey"),
    ("creator_fee_basis_points", "u64"),
    ("creator_fee", "u64"),
    ("track_volume", "bool"),
    ("total_unclaimed_tokens", "u64"),
    ("total_claimed_tokens", "u64"),
    ("current_sol_volume", "u64"),
    ("last_update_timestamp", "i64"),
    ("ix_name", "string"),
    ("mayhem_mode", "bool"),
    ("cashback_fee_basis_points", "u64"),
    ("cashback", "u64"),
    ("buyback_fee_basis_points", "u64"),
    ("buyback_fee", "u64"),
    ("shareholders", "shareholders"),
    ("quote_mint", "pubkey"),
    ("quote_amount", "u64"),
    ("virtual_quote_reserves", "u64"),
    ("real_quote_reserves", "u64"),
    ("holder_rewards_bps", "u64"),
    ("holder_rewards", "u64"),
    ("creator_fee_unclaimed", "u64"),
]


def _borsh_decode(fields: list[tuple[str, str]], buf: bytes, off: int) -> tuple[dict, int]:
    out: dict = {}
    for name, typ in fields:
        if off >= len(buf):
            break
        if typ == "pubkey":
            if off + 32 > len(buf):
                break
            out[name] = b58encode(buf[off : off + 32])
            off += 32
        elif typ == "u64":
            if off + 8 > len(buf):
                break
            out[name] = struct.unpack_from("<Q", buf, off)[0]
            off += 8
        elif typ == "i64":
            if off + 8 > len(buf):
                break
            out[name] = struct.unpack_from("<q", buf, off)[0]
            off += 8
        elif typ == "u16":
            if off + 2 > len(buf):
                break
            out[name] = struct.unpack_from("<H", buf, off)[0]
            off += 2
        elif typ == "bool":
            out[name] = bool(buf[off])
            off += 1
        elif typ == "string":
            if off + 4 > len(buf):
                break
            n = struct.unpack_from("<I", buf, off)[0]
            off += 4
            if off + n > len(buf):
                break
            out[name] = buf[off : off + n].decode("utf-8", errors="replace")
            off += n
        elif typ == "shareholders":
            if off + 4 > len(buf):
                break
            n = struct.unpack_from("<I", buf, off)[0]
            off += 4
            items = []
            for _ in range(n):
                if off + 34 > len(buf):
                    return out, off
                items.append({"address": b58encode(buf[off : off + 32]), "share_bps": struct.unpack_from("<H", buf, off + 32)[0]})
                off += 34
            out[name] = items
        else:
            break
    return out, off


def _borsh_encode(fields: list[tuple[str, str]], values: dict) -> bytes:
    buf = bytearray()
    for name, typ in fields:
        if name not in values:
            break
        val = values[name]
        if typ == "pubkey":
            buf += pubkey_bytes(val)
        elif typ == "u64":
            buf += struct.pack("<Q", int(val))
        elif typ == "i64":
            buf += struct.pack("<q", int(val))
        elif typ == "bool":
            buf += bytes([1 if val else 0])
        elif typ == "string":
            raw = str(val).encode()
            buf += struct.pack("<I", len(raw)) + raw
        elif typ == "shareholders":
            items = list(val or [])
            buf += struct.pack("<I", len(items))
            for item in items:
                buf += pubkey_bytes(item["address"])
                buf += struct.pack("<H", int(item["share_bps"]))
        else:
            break
    return bytes(buf)


def parse_trade_event(raw: bytes) -> dict | None:
    """Anchor TradeEvent. Non-SOL pairs leave sol_amount at 0 and set quote_*."""
    if len(raw) < 8 + 32 + 16 + 1 + 32 or raw[:8] != TRADE_DISC:
        return None
    ev, _ = _borsh_decode(TRADE_FIELDS, raw, 8)
    if not ev.get("mint") or not ev.get("user") or "token_amount" not in ev:
        return None
    ev["kind"] = "pumpfun"
    ev["is_buy"] = bool(ev.get("is_buy"))
    return ev


def encode_trade_event(values: dict) -> bytes:
    """Test helper: discriminator + the TradeEvent prefix described by ``values``."""
    return TRADE_DISC + _borsh_encode(TRADE_FIELDS, values)


# pump_amm BuyEvent / SellEvent: i64 timestamp + 13 × u64, then pool, user.
# buy: field 1 = base out, field 13 = user quote in. Same layout pumpswap_feed.parse_amm_event reads.
_AMM_NUM = struct.Struct("<q13Q")
_AMM_MIN = 8 + _AMM_NUM.size + 64


def parse_amm_event(raw: bytes) -> dict | None:
    if len(raw) < _AMM_MIN:
        return None
    disc = raw[:8]
    if disc == BUY_DISC:
        is_buy = True
    elif disc == SELL_DISC:
        is_buy = False
    else:
        return None
    nums = _AMM_NUM.unpack_from(raw, 8)
    off = 8 + _AMM_NUM.size
    pool = b58encode(raw[off : off + 32])
    user = b58encode(raw[off + 32 : off + 64])
    return {
        "kind": "pumpswap",
        "is_buy": is_buy,
        "pool": pool,
        "user": user,
        "base_amount": int(nums[1]),
        "quote_amount": int(nums[13]),
        "pool_base_reserves": int(nums[5]),
        "pool_quote_reserves": int(nums[6]),
        "timestamp": int(nums[0]),
    }


def iter_program_data(logs: list[str]) -> list[bytes]:
    out = []
    for line in logs or []:
        if not line.startswith("Program data: "):
            continue
        try:
            out.append(base64.b64decode(line[len("Program data: ") :]))
        except Exception:
            continue
    return out


def leader_events(logs: list[str], leader: str) -> list[dict]:
    found = []
    for raw in iter_program_data(logs):
        ev = parse_trade_event(raw)
        if ev is None:
            ev = parse_amm_event(raw)
        if ev and ev.get("user") == leader:
            found.append(ev)
    return found


def logs_are_interesting(logs: list[str]) -> bool:
    blob = "\n".join(logs or [])
    return any(marker in blob for marker in INTERESTING_MARKERS)


def is_sol_quote(mint: str | None) -> bool:
    return mint in SOL_MINT_ALIASES


def decode_bonding_curve(data: bytes) -> dict | None:
    """Bonding-curve account. Layout copied from ``backend/pumpfun.decode_bonding_curve``."""
    if len(data) < 49:
        return None
    vtr, vsr, rtr, rsr, supply = struct.unpack_from("<QQQQQ", data, 8)
    complete = bool(data[48])
    quote_mint = None
    if len(data) >= 115:
        quote_mint = b58encode(data[83:115])
        if is_sol_quote(quote_mint):
            quote_mint = None
    return {
        "virtual_token_reserves": vtr,
        "virtual_sol_reserves": vsr,
        "real_token_reserves": rtr,
        "real_sol_reserves": rsr,
        "token_total_supply": supply,
        "complete": complete,
        "quote_mint": quote_mint,
        "virtual_quote_reserves": vsr,
        "real_quote_reserves": rsr,
    }


def decode_pool(data: bytes) -> dict | None:
    """PumpSwap pool header. Layout copied from the comment in ``backend/pumpswap.py``."""
    need = 8 + 1 + 2 + 32 * 6
    if len(data) < need:
        return None
    off = 8
    off += 1  # bump
    off += 2  # index
    def pk() -> str:
        nonlocal off
        p = b58encode(data[off : off + 32])
        off += 32
        return p
    creator, base, quote, lp, base_vault, quote_vault = (pk() for _ in range(6))
    return {
        "creator": creator,
        "base_mint": base,
        "quote_mint": quote,
        "lp_mint": lp,
        "base_vault": base_vault,
        "quote_vault": quote_vault,
    }


def token_account_amount(data: bytes) -> int | None:
    """SPL token account amount at offset 64."""
    if len(data) < 72:
        return None
    return struct.unpack_from("<Q", data, 64)[0]
