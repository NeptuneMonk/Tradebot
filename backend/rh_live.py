"""Robinhood Chain LIVE execution on PONS bonding curves.

Verified from on-chain traffic: the token contract itself is the curve.
  buy(uint256 amountIn, uint256 minOut, address recipient)   0x59a87bc1  (ETH via msg.value)
  sell(uint256 amountIn, uint256 minOut, address recipient)  0xd04c6983
Trade events (topics from rh_discovery): Buy/Sell data words =
  [quoteIn|tokensIn, tokensOut|quoteOut, fee, _].
ETH-quoted curves only (ERC-20 quotes need approvals → stay paper).
"""
from __future__ import annotations

import logging
import time

import rh_wallet
from rh_discovery import T_BUY, T_SELL

logger = logging.getLogger("rh_live")

BUY_SIG = "buy(uint256,uint256,address)"
SELL_SIG = "sell(uint256,uint256,address)"
GAS_BUY = 160_000
GAS_SELL = 160_000
WEI = 10**18


def _trade_log(receipt: dict, token: str, topic: str) -> tuple[int, int, int] | None:
    for lg in receipt.get("logs") or []:
        if lg.get("address", "").lower() == token.lower() and (lg.get("topics") or [""])[0].lower() == topic.lower():
            d = lg["data"][2:]
            words = [int(d[i:i + 64], 16) for i in range(0, min(len(d), 192), 64)]
            if len(words) >= 2:
                return words[0], words[1], (words[2] if len(words) > 2 else 0)
    return None


async def quote_buy(token: str, quote_wei: int, est_price_quote: float) -> int:
    """Expected raw tokens out. Tries eth_call on buy(minOut=0) for the exact
    return value; falls back to the feed price."""
    try:
        data = rh_wallet.calldata(BUY_SIG, ["uint256", "uint256", "address"], [quote_wei, 0, rh_wallet.address()])
        res = await rh_wallet.simulate(token, data, quote_wei)
        if res and res != "0x":
            v = int(res[2:66], 16)
            if v > 0:
                return v
    except Exception as e:
        logger.debug(f"rh_live quote_buy simulate failed {token[:10]}: {e}")
    return int(quote_wei / est_price_quote) if est_price_quote > 0 else 0


async def buy(token: str, quote_wei: int, est_price_quote: float, slippage_pct: float) -> dict:
    est_out = await quote_buy(token, quote_wei, est_price_quote)
    if est_out <= 0:
        raise RuntimeError("cannot estimate tokens out")
    min_out = int(est_out * (1.0 - slippage_pct / 100.0))
    me = rh_wallet.address()
    data = rh_wallet.calldata(BUY_SIG, ["uint256", "uint256", "address"], [quote_wei, min_out, me])
    await rh_wallet.simulate(token, data, quote_wei)          # revert → RhRpcError before we spend gas
    t0 = time.time()
    tx = await rh_wallet.send(token, data, quote_wei, gas_limit=GAS_BUY)
    rc = await rh_wallet.wait_receipt(tx)
    if not rc["ok"]:
        raise RuntimeError(f"buy reverted on-chain tx={tx}")
    ev = _trade_log(rc, token, T_BUY)
    quote_in, tokens_out, fee = ev if ev else (quote_wei, est_out, 0)
    return {"tx": tx, "quote_wei": quote_in, "tokens_raw": tokens_out, "fee_wei": fee,
            "gas_cost_wei": rc["gas_cost_wei"], "latency_s": round(time.time() - t0, 2),
            "block": int(rc.get("blockNumber", "0x0"), 16)}


async def sell(token: str, tokens_raw: int, est_price_quote: float, slippage_pct: float) -> dict:
    est_quote = int(tokens_raw * est_price_quote) if est_price_quote > 0 else 0
    min_out = int(est_quote * (1.0 - slippage_pct / 100.0))
    me = rh_wallet.address()
    data = rh_wallet.calldata(SELL_SIG, ["uint256", "uint256", "address"], [tokens_raw, min_out, me])
    try:
        await rh_wallet.simulate(token, data, 0)
    except rh_wallet.RhRpcError as e:
        # price moved past slippage — emergency: sell with minOut=0 rather than be stranded
        logger.warning(f"rh_live sell sim failed ({e}); retrying with minOut=0")
        data = rh_wallet.calldata(SELL_SIG, ["uint256", "uint256", "address"], [tokens_raw, 0, me])
        await rh_wallet.simulate(token, data, 0)
    t0 = time.time()
    tx = await rh_wallet.send(token, data, 0, gas_limit=GAS_SELL)
    rc = await rh_wallet.wait_receipt(tx)
    if not rc["ok"]:
        raise RuntimeError(f"sell reverted on-chain tx={tx}")
    ev = _trade_log(rc, token, T_SELL)
    tokens_in, quote_out, fee = ev if ev else (tokens_raw, est_quote, 0)
    return {"tx": tx, "tokens_raw": tokens_in, "quote_wei": quote_out, "fee_wei": fee,
            "gas_cost_wei": rc["gas_cost_wei"], "latency_s": round(time.time() - t0, 2),
            "block": int(rc.get("blockNumber", "0x0"), 16)}
