"""Robinhood Chain LIVE execution on PONS bonding curves.

Verified from on-chain traffic: buy()/sell() live on the CURVE contract
(`bucket["curve"]`, distinct from the ERC-20 token address).
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
DEFAULT_FEE_PCT = 1.0   # curve protocol fee on the quote side (BotConfig.rh_protocol_fee_pct overrides on sells)


def _trade_log(receipt: dict, contract: str, topic: str) -> tuple[int, int, int] | None:
    for lg in receipt.get("logs") or []:
        if lg.get("address", "").lower() == contract.lower() and (lg.get("topics") or [""])[0].lower() == topic.lower():
            d = lg["data"][2:]
            words = [int(d[i:i + 64], 16) for i in range(0, min(len(d), 192), 64)]
            if len(words) >= 2:
                return words[0], words[1], (words[2] if len(words) > 2 else 0)
    return None


async def quote_buy(curve: str, quote_wei: int, est_price_quote: float, quote_token: str | None = None,
                    quote_decimals: int = 18) -> int:
    """Expected raw tokens out. Tries a read-only eth_call on buy() with a zero floor for the exact
    return value; falls back to the feed price. `quote_wei` is raw quote units (ETH wei or ERC-20 raw)."""
    try:
        data = rh_wallet.calldata(BUY_SIG, ["uint256", "uint256", "address"], [quote_wei, 0, rh_wallet.address()])
        res = await rh_wallet.simulate(curve, data, 0 if quote_token else quote_wei)
        if res and res != "0x":
            v = int(res[2:66], 16)
            if v > 0:
                return v
    except Exception as e:
        logger.debug(f"rh_live quote_buy simulate failed {curve[:10]}: {e}")
    # feed-price fallback is pre-fee → take the curve's ~1% quote-side fee off (the eth_call path above is already post-fee)
    return int(quote_wei * (1.0 - DEFAULT_FEE_PCT / 100.0) / 10 ** quote_decimals / est_price_quote * WEI) if est_price_quote > 0 else 0


async def buy(curve: str, quote_wei: int, est_price_quote: float, slippage_pct: float,
              quote_token: str | None = None, quote_decimals: int = 18) -> dict:
    """buy(amountIn, minOut, recipient). ETH-quoted curves take the input as msg.value; ERC-20-quoted curves pull it
    with transferFrom (verified from live buys: Approval → Transfer to the curve inside the same tx), so approve first."""
    approve_gas = 0
    if quote_token:
        approve_tx = await rh_wallet.ensure_allowance(quote_token, curve, quote_wei)
        if approve_tx:
            approve_gas = (await rh_wallet.wait_receipt(approve_tx))["gas_cost_wei"]
    est_out = await quote_buy(curve, quote_wei, est_price_quote, quote_token, quote_decimals)
    if est_out <= 0:
        raise RuntimeError("cannot estimate tokens out")
    min_out = int(est_out * (1.0 - slippage_pct / 100.0))
    me = rh_wallet.address()
    value = 0 if quote_token else quote_wei
    data = rh_wallet.calldata(BUY_SIG, ["uint256", "uint256", "address"], [quote_wei, min_out, me])
    await rh_wallet.simulate(curve, data, value)          # revert → RhRpcError before we spend gas
    t0 = time.time()
    tx = await rh_wallet.send(curve, data, value, gas_limit=GAS_BUY)
    rc = await rh_wallet.wait_receipt(tx)
    if not rc["ok"]:
        raise RuntimeError(f"buy reverted on-chain tx={tx}")
    ev = _trade_log(rc, curve, T_BUY)
    quote_in, tokens_out, fee = ev if ev else (quote_wei, est_out, 0)
    return {"tx": tx, "quote_wei": quote_in, "tokens_raw": tokens_out, "fee_wei": fee,
            "gas_cost_wei": rc["gas_cost_wei"] + approve_gas, "latency_s": round(time.time() - t0, 2),
            "block": int(rc.get("blockNumber", "0x0"), 16)}


async def quote_sell(curve: str, tokens_raw: int) -> int:
    """Expected raw quote out for `tokens_raw`, read-only eth_call on sell() with a zero floor (nothing is sent).
    Returns 0 when the call reverts or yields nothing."""
    try:
        data = rh_wallet.calldata(SELL_SIG, ["uint256", "uint256", "address"], [tokens_raw, 0, rh_wallet.address()])
        res = await rh_wallet.simulate(curve, data, 0)
        if res and res != "0x":
            return int(res[2:66], 16)
    except Exception as e:
        logger.debug(f"rh_live quote_sell simulate failed {curve[:10]}: {e}")
    return 0


def sell_min_out(est_quote: int, slippage_pct: float, fee_pct: float) -> int:
    """EMERGENT_CHANGES #11 — curve fee comes off the quote side BEFORE our tolerance, so the tolerance is what was configured."""
    return int(est_quote * (1.0 - float(fee_pct) / 100.0) * (1.0 - float(slippage_pct) / 100.0))


async def sell(curve: str, tokens_raw: int, est_price_quote: float, slippage_pct: float, token: str | None = None,
               cfg=None) -> dict:
    fee_pct = float(getattr(cfg, "rh_protocol_fee_pct", 1.0) if cfg is not None else 1.0)
    fallback_pct = float(getattr(cfg, "rh_sell_fallback_slippage_pct", 20.0) if cfg is not None else 20.0)
    est_quote = int(tokens_raw * est_price_quote) if est_price_quote > 0 else 0
    min_out = sell_min_out(est_quote, slippage_pct, fee_pct)
    me = rh_wallet.address()
    approve_gas = 0
    if token:
        # sell() does token.transferFrom(seller → curve); without an allowance it reverts every time
        approve_tx = await rh_wallet.ensure_allowance(token, curve, tokens_raw)
        if approve_tx:
            approve_gas = (await rh_wallet.wait_receipt(approve_tx))["gas_cost_wei"]
    data = rh_wallet.calldata(SELL_SIG, ["uint256", "uint256", "address"], [tokens_raw, max(1, min_out), me])
    try:
        await rh_wallet.simulate(curve, data, 0)
    except rh_wallet.RhRpcError as e:
        # price moved past slippage — re-quote from the curve, then ONE retry at a capped wider slippage. Never a zero floor.
        wide = max(float(slippage_pct), fallback_pct)
        fresh_quote = await quote_sell(curve, tokens_raw)
        if fresh_quote > 0:
            min_out = int(fresh_quote * (1.0 - wide / 100.0))        # exact post-fee output from the curve itself
        else:
            min_out = sell_min_out(est_quote, wide, fee_pct)
        logger.warning(f"rh_live sell sim failed ({e}); ONE retry at {wide:.0f}% slippage min_out={min_out} (requote={'curve' if fresh_quote else 'feed'})")
        data = rh_wallet.calldata(SELL_SIG, ["uint256", "uint256", "address"], [tokens_raw, max(1, min_out), me])
        await rh_wallet.simulate(curve, data, 0)   # raises → caller retries next tick / parks
    t0 = time.time()
    tx = await rh_wallet.send(curve, data, 0, gas_limit=GAS_SELL)
    rc = await rh_wallet.wait_receipt(tx)
    if not rc["ok"]:
        raise RuntimeError(f"sell reverted on-chain tx={tx}")
    ev = _trade_log(rc, curve, T_SELL)
    tokens_in, quote_out, fee = ev if ev else (tokens_raw, est_quote, 0)
    return {"tx": tx, "tokens_raw": tokens_in, "quote_wei": quote_out, "fee_wei": fee,
            "gas_cost_wei": rc["gas_cost_wei"] + approve_gas, "latency_s": round(time.time() - t0, 2),
            "block": int(rc.get("blockNumber", "0x0"), 16)}


async def recover_sell(curve: str, from_block: int) -> dict | None:
    """Find a sell() from OUR wallet on this curve since `from_block` (e.g. the tx landed
    but the process restarted before it was booked). Returns a fill dict or None."""
    me = rh_wallet.address().lower()
    head = int(await rh_wallet.rpc("eth_blockNumber", []), 16)
    logs = await rh_wallet.rpc("eth_getLogs", [{"fromBlock": hex(max(0, from_block)), "toBlock": hex(head),
                                                "address": rh_wallet.checksum(curve), "topics": [T_SELL]}])
    for lg in reversed(logs or []):
        rc = await rh_wallet.rpc("eth_getTransactionReceipt", [lg["transactionHash"]])
        if not rc or (rc.get("from") or "").lower() != me or rc.get("status") != "0x1":
            continue
        rc["ok"] = True
        rc["gas_cost_wei"] = int(rc.get("gasUsed", "0x0"), 16) * int(rc.get("effectiveGasPrice", "0x0"), 16)
        ev = _trade_log(rc, curve, T_SELL)
        if not ev:
            continue
        tokens_in, quote_out, fee = ev
        logger.warning(f"rh_live recovered unbooked SELL on {curve[:10]} tx={lg['transactionHash'][:12]}")
        return {"tx": lg["transactionHash"], "tokens_raw": tokens_in, "quote_wei": quote_out, "fee_wei": fee,
                "gas_cost_wei": rc["gas_cost_wei"], "latency_s": 0.0,
                "block": int(rc.get("blockNumber", "0x0"), 16), "recovered": True}
    return None
