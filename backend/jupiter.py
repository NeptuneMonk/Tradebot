"""Jupiter route for coins whose quote is a listed non-pump mint (PUMP, USDC, xStocks …): Jupiter routes
SOL → quote (Orca / Raydium / …) → coin (Pump.fun curve or PumpSwap pool) in one v0 transaction. We take the
instructions (not the signed tx) so our own compute budget, fee ladder and Helius Sender path still apply."""
from __future__ import annotations

import base64
import logging

import httpx
from solders.address_lookup_table_account import AddressLookupTableAccount
from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey

from solana_client import rpc_call

logger = logging.getLogger("jupiter")

LITE = "https://lite-api.jup.ag/swap/v1"
WSOL = "So11111111111111111111111111111111111111112"
CU_DEFAULT = 600_000
CU_CAP = 900_000
_ALT_CACHE: dict[str, AddressLookupTableAccount] = {}


MAX_ROUTE_ACCOUNTS = (48, 64)    # tight first (fits with our compute-budget + Sender tip ixs), then Jupiter's default


class JupiterError(RuntimeError):
    pass


async def quote(input_mint: str, output_mint: str, amount: int, slippage_bps: int) -> dict:
    last = "no route"
    for max_acc in MAX_ROUTE_ACCOUNTS:
        params = {"inputMint": input_mint, "outputMint": output_mint, "amount": str(int(amount)), "slippageBps": int(slippage_bps),
                  "maxAccounts": max_acc}
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{LITE}/quote", params=params)
        j = r.json() if r.content else {}
        if r.status_code == 200 and "routePlan" in j:
            return j
        last = j.get("error") or j.get("message") or r.status_code
        if j.get("errorCode") != "NO_ROUTES_FOUND":
            break
    raise JupiterError(f"quote {input_mint[:6]}→{output_mint[:6]}: {last}")


def _ix(d: dict) -> Instruction:
    metas = [AccountMeta(Pubkey.from_string(a["pubkey"]), bool(a["isSigner"]), bool(a["isWritable"])) for a in d["accounts"]]
    return Instruction(Pubkey.from_string(d["programId"]), base64.b64decode(d["data"]), metas)


async def _load_alts(addrs: list[str]) -> list[AddressLookupTableAccount]:
    missing = [a for a in addrs if a not in _ALT_CACHE]
    if missing:
        res = await rpc_call("getMultipleAccounts", [missing, {"encoding": "base64", "commitment": "confirmed"}])
        for addr, val in zip(missing, (res.get("result") or {}).get("value") or []):
            if not val:
                raise JupiterError(f"lookup table {addr[:8]}… not found")
            raw = base64.b64decode(val["data"][0])
            keys = [Pubkey.from_bytes(raw[i:i + 32]) for i in range(56, len(raw) - 31, 32)]
            _ALT_CACHE[addr] = AddressLookupTableAccount(key=Pubkey.from_string(addr), addresses=keys)
    return [_ALT_CACHE[a] for a in addrs]


async def swap_instructions(q: dict, user: Pubkey) -> tuple[list[Instruction], int, list[AddressLookupTableAccount]]:
    """(instructions without compute-budget ixs, compute unit limit, lookup tables) for a Jupiter quote."""
    body = {"quoteResponse": q, "userPublicKey": str(user), "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True, "prioritizationFeeLamports": 0}
    async with httpx.AsyncClient(timeout=6.0) as client:
        r = await client.post(f"{LITE}/swap-instructions", json=body)
    j = r.json() if r.content else {}
    if r.status_code != 200 or not j.get("swapInstruction"):
        raise JupiterError(f"swap-instructions: {j.get('error') or j.get('message') or r.status_code}")
    ixs = [_ix(d) for d in (j.get("setupInstructions") or [])] + [_ix(j["swapInstruction"])]
    if j.get("cleanupInstruction"):
        ixs.append(_ix(j["cleanupInstruction"]))
    ixs += [_ix(d) for d in (j.get("otherInstructions") or [])]
    cu = int(j.get("computeUnitLimit") or 0)
    cu = CU_DEFAULT if cu <= 0 or cu >= 1_400_000 else min(CU_CAP, int(cu * 1.15))
    alts = await _load_alts(j.get("addressLookupTableAddresses") or [])
    return ixs, cu, alts


async def buy_ixs(user: Pubkey, mint: str, lamports_in: int, slippage_bps: int) -> tuple[list[Instruction], int, list, dict]:
    q = await quote(WSOL, mint, lamports_in, slippage_bps)
    ixs, cu, alts = await swap_instructions(q, user)
    return ixs, cu, alts, q


async def sell_ixs(user: Pubkey, mint: str, tokens_in: int, slippage_bps: int) -> tuple[list[Instruction], int, list, dict]:
    q = await quote(mint, WSOL, tokens_in, slippage_bps)
    ixs, cu, alts = await swap_instructions(q, user)
    return ixs, cu, alts, q
