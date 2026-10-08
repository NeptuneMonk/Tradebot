"""Probe: decode full Pump TradeEvent / CreateEvent per the current IDL and print non-SOL-quoted coins seen live."""
import asyncio, json, os, struct, sys, base64, hashlib, time
import base58, websockets
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

idl = json.load(open("/app/backend/pump_idl.json"))
TYPES = {t["name"]: t for t in idl["types"]}
PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
WSOL = "So11111111111111111111111111111111111111112"
DEFAULT = "11111111111111111111111111111111"

def disc(name):
    return hashlib.sha256(f"event:{name}".encode()).digest()[:8]

def decode(fields, buf, off):
    out = {}
    for f in fields:
        t = f["type"]
        if off >= len(buf):
            break                                   # older event version: trailing fields absent
        if t == "string":
            n = struct.unpack_from("<I", buf, off)[0]; off += 4; out[f["name"]] = buf[off:off+n].decode("utf-8", "replace"); off += n
        elif t == "pubkey":
            out[f["name"]] = base58.b58encode(buf[off:off+32]).decode(); off += 32
        elif t == "u64":
            out[f["name"]] = struct.unpack_from("<Q", buf, off)[0]; off += 8
        elif t == "i64":
            out[f["name"]] = struct.unpack_from("<q", buf, off)[0]; off += 8
        elif t == "bool":
            out[f["name"]] = bool(buf[off]); off += 1
        elif t == "u8":
            out[f["name"]] = buf[off]; off += 1
        elif t == "u16":
            out[f["name"]] = struct.unpack_from("<H", buf, off)[0]; off += 2
        elif t == "i128":
            out[f["name"]] = int.from_bytes(buf[off:off+16], "little", signed=True); off += 16
        elif isinstance(t, dict) and "vec" in t:
            n = struct.unpack_from("<I", buf, off)[0]; off += 4
            inner = TYPES[t["vec"]["defined"]["name"]]["type"]["fields"]
            items = []
            for _ in range(n):
                item, off = decode(inner, buf, off); items.append(item)
            out[f["name"]] = items
        else:
            raise ValueError(f"unsupported {t}")
    return out, off

EVENTS = {disc(n): (n, TYPES[n]["type"]["fields"]) for n in ("TradeEvent", "CreateEvent")}

async def main(duration=75):
    url = os.environ["HELIUS_WSS_URL"]
    seen = {"trades": 0, "creates": 0, "nonsol_trades": 0, "nonsol_creates": 0}
    quotes = {}
    async with websockets.connect(url, max_size=2**24) as ws:
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe", "params": [{"mentions": [PUMP]}, {"commitment": "processed"}]}))
        t0 = time.time()
        while time.time() - t0 < duration:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                continue
            d = json.loads(msg)
            logs = (d.get("params", {}).get("result", {}).get("value", {}) or {}).get("logs") or []
            for line in logs:
                if not line.startswith("Program data: "):
                    continue
                try:
                    raw = base64.b64decode(line[14:])
                except Exception:
                    continue
                ev = EVENTS.get(raw[:8])
                if not ev:
                    continue
                name, fields = ev
                try:
                    obj, _ = decode(fields, raw, 8)
                except Exception as e:
                    print("decode fail", name, e); continue
                q = obj.get("quote_mint")
                if name == "TradeEvent":
                    seen["trades"] += 1
                    if q not in (WSOL, DEFAULT):
                        seen["nonsol_trades"] += 1
                        quotes[q] = quotes.get(q, 0) + 1
                        if seen["nonsol_trades"] <= 4:
                            print("NONSOL TRADE", {k: obj.get(k) for k in ("mint", "quote_mint", "sol_amount", "quote_amount", "virtual_sol_reserves", "virtual_quote_reserves", "real_sol_reserves", "real_quote_reserves", "is_buy", "ix_name")})
                    elif seen["trades"] <= 1:
                        print("SOL TRADE", {k: obj.get(k) for k in ("mint", "quote_mint", "sol_amount", "quote_amount", "virtual_sol_reserves", "virtual_quote_reserves", "ix_name")})
                else:
                    seen["creates"] += 1
                    if q not in (WSOL, DEFAULT):
                        seen["nonsol_creates"] += 1
                        if seen["nonsol_creates"] <= 3:
                            print("NONSOL CREATE", {k: obj.get(k) for k in ("mint", "symbol", "quote_mint", "virtual_sol_reserves", "virtual_quote_reserves", "token_program", "is_mayhem_mode", "depth")})
                    elif seen["creates"] <= 1:
                        print("SOL CREATE", {k: obj.get(k) for k in ("mint", "symbol", "quote_mint", "virtual_sol_reserves", "virtual_quote_reserves", "depth")})
    print("SUMMARY", seen, "quotes:", quotes)

asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 75))
