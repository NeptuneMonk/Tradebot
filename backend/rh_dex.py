"""Robinhood Chain post-graduation DEX path (Uniswap v4 pool behind the PONS hook).

On graduation the PONS curve sweeps tokens + quote into a v4 pool and its sell()
reverts forever. Verified on-chain (chain 4663, `PoolGraduated` tx):
  PoolKey = (currency0, currency1 sorted by address, fee 0, tickSpacing 200, PonsV2MemeHook)
  ETH-quoted pools: currency0 = native ETH. ERC-20-quoted pools (USDG, tokenized stocks): same hook /
  fee / tickSpacing, the meme token may land on either side of the key — every helper takes `quote`.
  Swaps go PoolManager <- Universal Router (execute 0x3593564c, V4_SWAP) <- Permit2 (ERC-20 inputs).
The hook takes ~3% on every swap (quoter vs spot), so fills come from V4Quoter.
"""
from __future__ import annotations

import logging
import time

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_utils import keccak

import rh_wallet

logger = logging.getLogger("rh_dex")

POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
QUOTER = "0x8dc178efb8111bb0973dd9d722ebeff267c98f94"
STATE_VIEW = "0xf3334192d15450cdd385c8b70e03f9a6bd9e673b"
UNIVERSAL_ROUTER = "0x8876789976decbfcbbbe364623c63652db8c0904"
PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
HOOK = "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044"
NATIVE = "0x0000000000000000000000000000000000000000"
FEE, TICK_SPACING = 0, 200
T_SWAP = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
POOL_FEE_BPS = 300           # hook take observed on quotes (paper fallback only)
GAS_SWAP = 260_000
Q96 = 2**96
MAX_UINT160 = 2**160 - 1
MAX_UINT48 = 2**48 - 1

KEY_T = "(address,address,uint24,int24,address)"
CMD_V4_SWAP = bytes([0x10])
ACT_SWAP_EXACT_IN_SINGLE, ACT_SETTLE_ALL, ACT_TAKE_ALL = 0x06, 0x0C, 0x0F


def is_native(quote: str | None) -> bool:
    return not quote or quote.lower() == NATIVE


def token_is_c0(token: str, quote: str = NATIVE) -> bool:
    """v4 sorts currencies by address; native ETH (0x0) is always currency0."""
    return not is_native(quote) and int(token, 16) < int(quote, 16)


def pool_key(token: str, quote: str = NATIVE) -> tuple:
    tok, q = rh_wallet.checksum(token), rh_wallet.checksum(quote or NATIVE)
    c0, c1 = (tok, q) if token_is_c0(token, quote) else (q, tok)
    return (c0, c1, FEE, TICK_SPACING, rh_wallet.checksum(HOOK))


def pool_id(token: str, quote: str = NATIVE) -> bytes:
    return keccak(abi_encode([KEY_T], [pool_key(token, quote)]))


def price_from_sqrt(sqrt_price_x96: int, token_c0: bool = False, quote_decimals: int = 18) -> float:
    """sqrtP² = currency1_raw / currency0_raw. Returns quote per token in human units."""
    if sqrt_price_x96 <= 0:
        return 0.0
    ratio = (sqrt_price_x96 / Q96) ** 2                     # c1 per c0 (raw)
    scale = 10 ** (18 - quote_decimals)                     # token is 18-dec
    return ratio * scale if token_c0 else (1.0 / ratio) * scale


def decode_swap(log: dict, token_c0: bool = False, quote_decimals: int = 18) -> dict:
    """PoolManager.Swap(id, sender; amount0, amount1, sqrtPriceX96, liquidity, tick, fee).
    Deltas are the swapper's: paying the quote (negative quote delta) is a buy of the token."""
    a0, a1, sqrt_p, _liq, _tick, _fee = abi_decode(["int128", "int128", "uint160", "uint128", "int24", "uint24"],
                                                    bytes.fromhex(log["data"][2:]))
    q_raw, t_raw = (a1, a0) if token_c0 else (a0, a1)
    side = "buy" if q_raw < 0 else "sell"
    quote = abs(q_raw) / 10 ** quote_decimals
    return {"side": side, "wallet": "0x" + log["topics"][2][-40:], "quote": quote,
            "q_eff": quote, "tokens": abs(t_raw) / 1e18, "price": price_from_sqrt(sqrt_p, token_c0, quote_decimals),
            "block": int(log["blockNumber"], 16), "venue": "pool"}


async def spot_price(token: str, quote: str = NATIVE, quote_decimals: int = 18) -> float:
    """Quote per token from StateView.getSlot0; 0.0 when the pool was never initialised (not graduated)."""
    res = await rh_wallet.rpc("eth_call", [{"to": STATE_VIEW, "data": rh_wallet.calldata("getSlot0(bytes32)", ["bytes32"], [pool_id(token, quote)])}, "latest"])
    if not res or res == "0x":
        return 0.0
    sqrt_p = abi_decode(["uint160", "int24", "uint24", "uint24"], bytes.fromhex(res[2:]))[0]
    return price_from_sqrt(sqrt_p, token_is_c0(token, quote), quote_decimals)


async def _quote_exact_in(token: str, quote: str, zero_for_one: bool, amount_in: int) -> int:
    qt = f"({KEY_T},bool,uint128,bytes)"
    data = rh_wallet.calldata(f"quoteExactInputSingle({qt})", [qt], [(pool_key(token, quote), zero_for_one, amount_in, b"")])
    res = await rh_wallet.rpc("eth_call", [{"to": QUOTER, "data": data}, "latest"])
    return abi_decode(["uint256", "uint256"], bytes.fromhex(res[2:]))[0]


async def quote_sell(token: str, tokens_raw: int, quote: str = NATIVE) -> int:
    """Exact quote (raw) out for selling `tokens_raw` via V4Quoter (includes the hook take + impact)."""
    return await _quote_exact_in(token, quote, token_is_c0(token, quote), tokens_raw)


async def quote_buy(token: str, quote_raw: int, quote: str = NATIVE) -> int:
    """Exact tokens (raw) out for `quote_raw` in via V4Quoter (hook take + impact included)."""
    return await _quote_exact_in(token, quote, not token_is_c0(token, quote), quote_raw)


async def round_trip(token: str, quote_raw: int, quote: str = NATIVE) -> dict:
    """What the pool really charges for our size: buy `quote_raw`, then sell every token back. Both legs from the
    V4Quoter, so hook take + our own impact are in the number. cost_pct is the round-trip friction."""
    tokens = await quote_buy(token, quote_raw, quote)
    if tokens <= 0:
        raise RuntimeError("pool buy quote returned 0")
    back = await quote_sell(token, tokens, quote)
    cost_pct = (1.0 - back / quote_raw) * 100.0 if quote_raw > 0 else 100.0
    return {"quote_in": quote_raw, "tokens_raw": tokens, "quote_back": back, "cost_pct": round(max(0.0, cost_pct), 3)}


def _execute(actions: bytes, params: list[bytes], deadline: int | None) -> str:
    v4_input = abi_encode(["bytes", "bytes[]"], [actions, params])
    return rh_wallet.calldata("execute(bytes,bytes[],uint256)", ["bytes", "bytes[]", "uint256"],
                              [CMD_V4_SWAP, [v4_input], deadline or int(time.time()) + 120])


def build_sell_calldata(token: str, tokens_raw: int, min_out: int, deadline: int | None = None, quote: str = NATIVE) -> str:
    """UniversalRouter.execute(V4_SWAP: SWAP_EXACT_IN_SINGLE(token→quote) → SETTLE_ALL(token) → TAKE_ALL(quote))."""
    tok, q = rh_wallet.checksum(token), rh_wallet.checksum(quote or NATIVE)
    swap = abi_encode([f"({KEY_T},bool,uint128,uint128,bytes)"], [(pool_key(token, quote), token_is_c0(token, quote), tokens_raw, min_out, b"")])
    settle = abi_encode(["address", "uint256"], [tok, tokens_raw])
    take = abi_encode(["address", "uint256"], [q, min_out])
    return _execute(bytes([ACT_SWAP_EXACT_IN_SINGLE, ACT_SETTLE_ALL, ACT_TAKE_ALL]), [swap, settle, take], deadline)


def build_buy_calldata(token: str, quote_raw: int, min_tokens_raw: int, deadline: int | None = None, quote: str = NATIVE) -> str:
    """UniversalRouter.execute(V4_SWAP: SWAP_EXACT_IN_SINGLE(quote→token) → SETTLE_ALL(quote) → TAKE_ALL(token)).
    Native ETH input rides on msg.value; an ERC-20 quote is pulled through Permit2 (approve first)."""
    tok, q = rh_wallet.checksum(token), rh_wallet.checksum(quote or NATIVE)
    swap = abi_encode([f"({KEY_T},bool,uint128,uint128,bytes)"], [(pool_key(token, quote), not token_is_c0(token, quote), quote_raw, min_tokens_raw, b"")])
    settle = abi_encode(["address", "uint256"], [q, quote_raw])
    take = abi_encode(["address", "uint256"], [tok, min_tokens_raw])
    return _execute(bytes([ACT_SWAP_EXACT_IN_SINGLE, ACT_SETTLE_ALL, ACT_TAKE_ALL]), [swap, settle, take], deadline)


async def buy(token: str, quote_raw: int, slippage_pct: float, quote: str = NATIVE) -> dict:
    """Buy the graduated token on its v4 pool with `quote_raw` of the quote asset (native ETH or an ERC-20 the wallet
    already holds). Same fill shape as rh_live.buy; tokens_raw is the wallet's ERC-20 delta (truth)."""
    est_out = await quote_buy(token, quote_raw, quote)
    if est_out <= 0:
        raise RuntimeError("pool buy quote returned 0")
    min_out = int(est_out * (1.0 - slippage_pct / 100.0))
    native = is_native(quote)
    approve_gas = 0 if native else await ensure_permit2(quote, quote_raw)
    data = build_buy_calldata(token, quote_raw, min_out, quote=quote)
    value = quote_raw if native else 0
    await rh_wallet.simulate(UNIVERSAL_ROUTER, data, value)
    pre = await rh_wallet.erc20_balance(token)
    t0 = time.time()
    tx = await rh_wallet.send(UNIVERSAL_ROUTER, data, value, gas_limit=GAS_SWAP)
    rc = await rh_wallet.wait_receipt(tx)
    if not rc["ok"]:
        raise RuntimeError(f"pool buy reverted on-chain tx={tx}")
    post = await rh_wallet.erc20_balance(token)
    got = max(0, post - pre) or est_out
    impl_raw = int(quote_raw * got / est_out) if est_out else quote_raw
    return {"tx": tx, "quote_wei": quote_raw, "tokens_raw": got, "fee_wei": max(0, quote_raw - impl_raw),
            "gas_cost_wei": rc["gas_cost_wei"] + approve_gas, "latency_s": round(time.time() - t0, 2),
            "block": int(rc.get("blockNumber", "0x0"), 16), "venue": "pool"}


async def permit2_allowance(token: str, owner: str | None = None) -> tuple[int, int]:
    data = rh_wallet.calldata("allowance(address,address,address)", ["address", "address", "address"],
                              [rh_wallet.checksum(owner or rh_wallet.address()), rh_wallet.checksum(token), rh_wallet.checksum(UNIVERSAL_ROUTER)])
    res = await rh_wallet.rpc("eth_call", [{"to": PERMIT2, "data": data}, "latest"])
    amount, expiration, _nonce = abi_decode(["uint160", "uint48", "uint48"], bytes.fromhex(res[2:]))
    return amount, expiration


async def ensure_permit2(token: str, tokens_raw: int) -> int:
    """token.approve(Permit2) once, then Permit2.approve(token, UniversalRouter). Returns gas spent (wei)."""
    gas = 0
    tx = await rh_wallet.ensure_allowance(token, PERMIT2, tokens_raw)
    if tx:
        gas += (await rh_wallet.wait_receipt(tx))["gas_cost_wei"]
    amount, expiration = await permit2_allowance(token)
    if amount >= tokens_raw and expiration > time.time() + 60:
        return gas
    data = rh_wallet.calldata("approve(address,address,uint160,uint48)", ["address", "address", "uint160", "uint48"],
                              [rh_wallet.checksum(token), rh_wallet.checksum(UNIVERSAL_ROUTER), MAX_UINT160, MAX_UINT48])
    tx = await rh_wallet.send(PERMIT2, data, 0, gas_limit=80_000)
    rc = await rh_wallet.wait_receipt(tx)
    if not rc["ok"]:
        raise rh_wallet.RhRpcError(f"permit2 approve reverted tx={tx}")
    logger.warning(f"rh_dex PERMIT2 {token[:10]} → UniversalRouter tx={tx[:12]}")
    return gas + rc["gas_cost_wei"]


async def sell(token: str, tokens_raw: int, slippage_pct: float, quote: str = NATIVE) -> dict:
    """Sell `tokens_raw` for the pool's quote asset. Same fill shape as rh_live.sell; quote_wei is raw quote units."""
    est_out = await quote_sell(token, tokens_raw, quote)
    if est_out <= 0:
        raise RuntimeError("pool quote returned 0")
    min_out = int(est_out * (1.0 - slippage_pct / 100.0))
    approve_gas = await ensure_permit2(token, tokens_raw)
    data = build_sell_calldata(token, tokens_raw, min_out, quote=quote)
    try:
        await rh_wallet.simulate(UNIVERSAL_ROUTER, data, 0)
    except rh_wallet.RhRpcError as e:
        logger.warning(f"rh_dex sell sim failed ({e}); retrying with minOut=0")
        data = build_sell_calldata(token, tokens_raw, 0, quote=quote)
        await rh_wallet.simulate(UNIVERSAL_ROUTER, data, 0)
    me = rh_wallet.address()
    native = is_native(quote)
    pre = await (rh_wallet.balance_wei(me) if native else rh_wallet.erc20_balance(quote, me))
    t0 = time.time()
    tx = await rh_wallet.send(UNIVERSAL_ROUTER, data, 0, gas_limit=GAS_SWAP)
    rc = await rh_wallet.wait_receipt(tx)
    if not rc["ok"]:
        raise RuntimeError(f"pool sell reverted on-chain tx={tx}")
    post = await (rh_wallet.balance_wei(me) if native else rh_wallet.erc20_balance(quote, me))
    received = max(0, post - pre + (rc["gas_cost_wei"] if native else 0))   # wallet delta is the truth (hook take already applied)
    return {"tx": tx, "tokens_raw": tokens_raw, "quote_wei": received or est_out, "fee_wei": max(0, est_out - received) if received else 0,
            "gas_cost_wei": rc["gas_cost_wei"] + approve_gas, "latency_s": round(time.time() - t0, 2),
            "block": int(rc.get("blockNumber", "0x0"), 16), "venue": "pool"}


async def recover_sell(token: str, from_block: int, quote: str = NATIVE, quote_decimals: int = 18) -> dict | None:
    """Find a pool sell from OUR wallet since `from_block` (tx landed, process restarted before booking)."""
    me = rh_wallet.address().lower()
    head = int(await rh_wallet.rpc("eth_blockNumber", []), 16)
    logs = await rh_wallet.rpc("eth_getLogs", [{"fromBlock": hex(max(0, from_block)), "toBlock": hex(head),
                                                "address": POOL_MANAGER, "topics": [T_SWAP, "0x" + pool_id(token, quote).hex()]}])
    c0 = token_is_c0(token, quote)
    for lg in reversed(logs or []):
        sw = decode_swap(lg, c0, quote_decimals)
        if sw["side"] != "sell":
            continue
        rc = await rh_wallet.rpc("eth_getTransactionReceipt", [lg["transactionHash"]])
        if not rc or (rc.get("from") or "").lower() != me or rc.get("status") != "0x1":
            continue
        gas = int(rc.get("gasUsed", "0x0"), 16) * int(rc.get("effectiveGasPrice", "0x0"), 16)
        logger.warning(f"rh_dex recovered unbooked POOL SELL {token[:10]} tx={lg['transactionHash'][:12]}")
        return {"tx": lg["transactionHash"], "tokens_raw": int(sw["tokens"] * 1e18), "quote_wei": int(sw["quote"] * 10 ** quote_decimals), "fee_wei": 0,
                "gas_cost_wei": gas, "latency_s": 0.0, "block": sw["block"], "recovered": True, "venue": "pool"}
    return None
