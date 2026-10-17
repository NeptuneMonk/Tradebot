"""PumpSwap `multi_hop_swap`: buy / sell a coin whose quote is another pump coin (depth ≥ 1) in ONE instruction, paying /
receiving native SOL. The route walks the quote chain down to SOL — each hop is the coin's live bonding curve or, once
migrated, its canonical PumpSwap pool (exactly what @pump-fun/pump-sdk `resolveMultiHopRoute` does). Coins quoted by a
*listed* non-pump mint (PUMP, USDC, xStocks …) have no pump venue for SOL → quote and raise `NotPumpRoutable`; the
caller falls back to Jupiter for those."""
from __future__ import annotations

import hashlib
import struct

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey
from solders.system_program import ID as SYSTEM_PROGRAM_ID, TransferParams, transfer
from spl.token.instructions import SyncNativeParams, sync_native

import pumpfun
import pumpswap
from pumpswap import PUMPSWAP_PROGRAM_ID, GLOBAL_CONFIG, EVENT_AUTH, TOKEN_PROGRAM, WSOL, get_associated_token_address, build_create_ata_ix, build_close_wsol_ix
from quote_mints import is_sol_quote

TOKEN_2022 = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
AMM_FEE_CONFIG = Pubkey.from_string("5PHirr8joyTMp9JMm6nW7hNDVyEYdkzDqazxPD7RaTjx")
PUMP_FEE_CONFIG = Pubkey.from_string("8Wf5TiAheLUqBrKXeYg2JtAFFMWtKdG2BSFgqUcPVwTt")
DISC = hashlib.sha256(b"global:multi_hop_swap").digest()[:8]
CU_PER_HOP = 120_000
MAX_HOPS = 2                      # 3 hops + wrap/ATA/tip ixs overflow the 1232-byte packet without a lookup table → Jupiter
_BUYBACK_ATA_OK = False


class NotPumpRoutable(RuntimeError):
    """The quote chain does not reach SOL through pump venues — route through Jupiter instead."""


def canonical_pool_with_quote(mint: Pubkey, quote: Pubkey) -> Pubkey:
    authority, _ = Pubkey.find_program_address([b"pool-authority", bytes(mint)], pumpfun.PUMP_PROGRAM_ID)
    pda, _ = Pubkey.find_program_address([b"pool", (0).to_bytes(2, "little"), bytes(authority), bytes(mint), bytes(quote)], PUMPSWAP_PROGRAM_ID)
    return pda


async def _tp(mint: str) -> Pubkey:
    return TOKEN_PROGRAM if mint == str(WSOL) else await pumpfun.get_mint_token_program(mint)


async def _hop_for(base: str, quote: str) -> dict:
    """Venue for `base` priced in `quote`: its live curve when the curve's quote matches, else its canonical pool."""
    curve = await pumpfun.fetch_bonding_curve_state(base)
    curve_q = str(WSOL) if is_sol_quote((curve or {}).get("quote_mint")) else (curve or {}).get("quote_mint")
    if curve and not curve.get("complete") and curve_q == quote:
        if curve.get("is_mayhem"):
            raise NotPumpRoutable(f"{base[:8]}… is a mayhem-mode curve — the multi-hop builder is not mayhem-ready (direct curve path only)")
        venue = pumpfun.derive_bonding_curve(Pubkey.from_string(base))
        return {"venue": "curve", "base": base, "quote": quote, "addr": venue}
    pool = canonical_pool_with_quote(Pubkey.from_string(base), Pubkey.from_string(quote))
    st = await pumpswap.fetch_pool_state(str(pool))
    if st:
        return {"venue": "pool", "base": base, "quote": quote, "addr": pool, "state": st}
    if curve is None:
        raise NotPumpRoutable(f"{base[:8]}… is not a pump coin (no bonding curve) — no pump venue for {base[:8]}…/{quote[:8]}…")
    raise NotPumpRoutable(f"{base[:8]}…/{quote[:8]}…: curve complete, canonical pool not live yet")


async def resolve_route(mint: str, quote_mint: str) -> list[dict]:
    """Hops from the coin down to SOL: [coin/Q, Q/Q', …, X/SOL]. Sell order; reverse for a buy."""
    hops: list[dict] = []
    base, quote = mint, quote_mint
    while True:
        q = str(WSOL) if is_sol_quote(quote) else quote
        hops.append(await _hop_for(base, q))
        if q == str(WSOL):
            return hops
        if len(hops) >= MAX_HOPS:
            raise NotPumpRoutable(f"route for {mint[:8]}… needs more than {MAX_HOPS} hops")
        qcurve = await pumpfun.fetch_bonding_curve_state(q)
        if qcurve is None:
            raise NotPumpRoutable(f"quote {q[:8]}… is not a pump coin — no pump venue for SOL → {q[:8]}…")
        base, quote = q, qcurve.get("quote_mint")


async def _hop_accounts(h: dict) -> list[AccountMeta]:
    base, quote = Pubkey.from_string(h["base"]), Pubkey.from_string(h["quote"])
    base_tp, quote_tp = await _tp(h["base"]), await _tp(h["quote"])
    if h["venue"] == "pool":
        st = h["state"]
        vaults = (Pubkey.from_string(st["pool_base_token_account"]), Pubkey.from_string(st["pool_quote_token_account"]))
    else:
        vaults = (get_associated_token_address(h["addr"], base, base_tp), get_associated_token_address(h["addr"], quote, quote_tp))
    return [AccountMeta(base, False, False), AccountMeta(quote, False, False), AccountMeta(h["addr"], False, True),
            AccountMeta(vaults[0], False, True), AccountMeta(vaults[1], False, True)]


def build_multi_hop_ix(user: Pubkey, user_in: Pubkey, user_out: Pubkey, hop_metas: list[list[AccountMeta]], amount_in: int, min_out: int) -> Instruction:
    buyback_wsol = get_associated_token_address(pumpfun.BREAKING_FEE_RECIPIENTS[0], WSOL, TOKEN_PROGRAM)
    fixed = [
        AccountMeta(user, True, True), AccountMeta(user_in, False, True), AccountMeta(user_out, False, True),
        AccountMeta(GLOBAL_CONFIG, False, False), AccountMeta(AMM_FEE_CONFIG, False, False),
        AccountMeta(pumpswap.derive_user_volume_accumulator(user), False, True), AccountMeta(buyback_wsol, False, True),
        AccountMeta(TOKEN_PROGRAM, False, False), AccountMeta(TOKEN_2022, False, False), AccountMeta(SYSTEM_PROGRAM_ID, False, False),
        AccountMeta(EVENT_AUTH, False, False), AccountMeta(PUMPSWAP_PROGRAM_ID, False, False),
        AccountMeta(pumpfun.PUMP_PROGRAM_ID, False, False), AccountMeta(pumpfun.GLOBAL_PDA, False, False),
        AccountMeta(PUMP_FEE_CONFIG, False, False), AccountMeta(pumpfun.EVENT_AUTHORITY, False, False),
    ]
    accounts = fixed + [m for hop in hop_metas for m in hop]
    return Instruction(program_id=PUMPSWAP_PROGRAM_ID, data=DISC + struct.pack("<QQ", int(amount_in), max(1, int(min_out))), accounts=accounts)


async def _buyback_ata_ixs(user: Pubkey) -> list[Instruction]:
    """Idempotent create of the buyback wallet's WSOL ATA — skipped (saves ~45 tx bytes) once seen on chain."""
    global _BUYBACK_ATA_OK
    if _BUYBACK_ATA_OK:
        return []
    ata = get_associated_token_address(pumpfun.BREAKING_FEE_RECIPIENTS[0], WSOL, TOKEN_PROGRAM)
    try:
        from solana_client import rpc_call
        res = await rpc_call("getAccountInfo", [str(ata), {"encoding": "base64", "commitment": "confirmed"}])
        if (res.get("result") or {}).get("value"):
            _BUYBACK_ATA_OK = True
            return []
    except Exception:
        pass
    return [build_create_ata_ix(user, pumpfun.BREAKING_FEE_RECIPIENTS[0], WSOL, TOKEN_PROGRAM)]


LAST_UNITS_CONSUMED: dict[str, int] = {}   # mint → unitsConsumed of the last successful route simulation (EMERGENT_CHANGES #3)


async def simulate_out(user: Pubkey, ixs: list[Instruction], cu: int, side: str, mint: str) -> int:
    """Exact route output from an unsigned simulateTransaction: the user's lamport delta (sell, base fee added back)
    or the coin ATA's token delta (buy). pump-amm's return data is not set on every route, so balances are the truth."""
    import asyncio as _aio
    import base64
    from solders.compute_budget import set_compute_unit_limit
    from solders.hash import Hash
    from solders.message import MessageV0
    from solders.signature import Signature
    from solders.transaction import VersionedTransaction
    from solana_client import rpc_call
    watch = user if side == "sell" else get_associated_token_address(user, Pubkey.from_string(mint), await _tp(mint))

    async def pre_read() -> int:
        if side == "sell":
            res = await rpc_call("getBalance", [str(user), {"commitment": "processed"}])
            return int((res.get("result") or {}).get("value") or 0)
        res = await rpc_call("getTokenAccountBalance", [str(watch), {"commitment": "processed"}])
        return int((((res.get("result") or {}).get("value") or {}).get("amount")) or 0)

    msg = MessageV0.try_compile(payer=user, instructions=[set_compute_unit_limit(cu), *ixs], address_lookup_table_accounts=[],
                                recent_blockhash=Hash.default())
    tx = VersionedTransaction.populate(msg, [Signature.default()])
    pre, res = await _aio.gather(pre_read(), rpc_call("simulateTransaction", [
        base64.b64encode(bytes(tx)).decode(),
        {"encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True, "commitment": "processed",
         "accounts": {"addresses": [str(watch)], "encoding": "jsonParsed"}}]))
    v = (res.get("result") or {}).get("value") or {}
    if v.get("err") is not None:
        tail = " | ".join(line for line in (v.get("logs") or [])[-4:])
        raise RuntimeError(f"route simulation failed: {v['err']} {tail}")
    try:
        LAST_UNITS_CONSUMED[mint] = int(v.get("unitsConsumed") or 0)
    except (TypeError, ValueError):
        pass
    acc = ((v.get("accounts") or [None])[0]) or {}
    if side == "sell":
        post = int(acc.get("lamports") or 0)
        out = post - pre + 5_000
    else:
        post = int(((((acc.get("data") or {}).get("parsed") or {}).get("info") or {}).get("tokenAmount") or {}).get("amount") or 0)
        out = post - pre
    if out <= 0:
        raise RuntimeError(f"route simulation produced no {'SOL' if side == 'sell' else 'tokens'} (pre={pre} post={post})")
    return out


def with_min_out(ixs: list[Instruction], min_out: int) -> list[Instruction]:
    """Same route, new `min_amount_out` on the multi_hop_swap instruction."""
    out = []
    for ix in ixs:
        if ix.program_id == PUMPSWAP_PROGRAM_ID and bytes(ix.data)[:8] == DISC:
            amount_in = struct.unpack_from("<Q", bytes(ix.data), 8)[0]
            ix = Instruction(program_id=PUMPSWAP_PROGRAM_ID, data=DISC + struct.pack("<QQ", amount_in, max(1, int(min_out))), accounts=ix.accounts)
        out.append(ix)
    return out


def cu_for(hops: list[dict]) -> int:
    return 80_000 + CU_PER_HOP * len(hops)


async def buy_ixs(user: Pubkey, mint: str, quote_mint: str, lamports_in: int, min_tokens_out: int) -> tuple[list[Instruction], int]:
    """SOL → … → coin. SOL is wrapped only when the SOL-side hop is a pool; a SOL curve takes lamports from the wallet
    but still wants the (empty) WSOL ATA passed. Returns (ixs, compute units)."""
    route = list(reversed(await resolve_route(mint, quote_mint)))      # SOL side first
    base_mint = Pubkey.from_string(mint)
    base_tp = await _tp(mint)
    wsol_ata = get_associated_token_address(user, WSOL, TOKEN_PROGRAM)
    user_out = get_associated_token_address(user, base_mint, base_tp)
    wrap = [transfer(TransferParams(from_pubkey=user, to_pubkey=wsol_ata, lamports=int(lamports_in))),
            sync_native(SyncNativeParams(program_id=TOKEN_PROGRAM, account=wsol_ata))] if route[0]["venue"] == "pool" else []
    metas = [await _hop_accounts(h) for h in route]
    return [
        build_create_ata_ix(user, user, WSOL, TOKEN_PROGRAM),
        *wrap,
        build_create_ata_ix(user, user, base_mint, base_tp),
        *(await _buyback_ata_ixs(user)),
        build_multi_hop_ix(user, wsol_ata, user_out, metas, lamports_in, min_tokens_out),
        build_close_wsol_ix(user, wsol_ata),
    ], cu_for(route)


async def sell_ixs(user: Pubkey, mint: str, quote_mint: str, tokens_in: int, min_lamports_out: int) -> tuple[list[Instruction], int]:
    """coin → … → SOL. Output lands as wrapped SOL (pool) or lamports (curve); the WSOL ATA is closed either way."""
    route = await resolve_route(mint, quote_mint)                        # coin side first
    base_mint = Pubkey.from_string(mint)
    base_tp = await _tp(mint)
    wsol_ata = get_associated_token_address(user, WSOL, TOKEN_PROGRAM)
    user_in = get_associated_token_address(user, base_mint, base_tp)
    metas = [await _hop_accounts(h) for h in route]
    return [
        build_create_ata_ix(user, user, WSOL, TOKEN_PROGRAM),
        *(await _buyback_ata_ixs(user)),
        build_multi_hop_ix(user, user_in, wsol_ata, metas, tokens_in, min_lamports_out),
        build_close_wsol_ix(user, wsol_ata),
    ], cu_for(route)


def cu_from_simulation(mint: str, mult: float, fallback: int = 300_000) -> int:
    """CU limit for a routed leg: last simulated unitsConsumed × mult (min 80k); `fallback` when no simulation is known."""
    units = int(LAST_UNITS_CONSUMED.get(mint) or 0)
    if units <= 0:
        return int(fallback)
    return max(80_000, int(units * float(mult)))
