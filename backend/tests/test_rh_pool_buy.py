"""v4 pool buy path: ETH→token calldata mirrors the sell, live seasoned entries route to rh_dex.buy, ERC-20 quotes refused."""
import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import rh_dex
import rh_wallet
from eth_abi import decode as abi_decode
from tests.test_rh_paper import make_state, hot_bucket, TOKEN


def test_buy_calldata_is_zero_for_one_settle_eth_take_token():
    data = bytes.fromhex(rh_dex.build_buy_calldata(TOKEN, 10**16, 5 * 10**21, deadline=1_900_000_000)[2:])
    cmds, inputs, deadline = abi_decode(["bytes", "bytes[]", "uint256"], data[4:])
    assert cmds == rh_dex.CMD_V4_SWAP and deadline == 1_900_000_000 and len(inputs) == 1
    actions, params = abi_decode(["bytes", "bytes[]"], inputs[0])
    assert actions == bytes([rh_dex.ACT_SWAP_EXACT_IN_SINGLE, rh_dex.ACT_SETTLE_ALL, rh_dex.ACT_TAKE_ALL])
    key, zero_for_one, amount_in, min_out, hook_data = abi_decode([f"({rh_dex.KEY_T},bool,uint128,uint128,bytes)"], params[0])[0]
    assert zero_for_one is True and amount_in == 10**16 and min_out == 5 * 10**21 and key[0].lower() == rh_dex.NATIVE
    assert key[1].lower() == TOKEN.lower()
    settle_cur, settle_amt = abi_decode(["address", "uint256"], params[1])
    take_cur, take_amt = abi_decode(["address", "uint256"], params[2])
    assert settle_cur.lower() == rh_dex.NATIVE and settle_amt == 10**16
    assert take_cur.lower() == TOKEN.lower() and take_amt == 5 * 10**21
    # sell stays token→ETH (oneForZero) — the two legs are mirrors
    sdata = bytes.fromhex(rh_dex.build_sell_calldata(TOKEN, 7, 3, deadline=1)[2:])
    _, sinputs, _ = abi_decode(["bytes", "bytes[]", "uint256"], sdata[4:])
    _, sparams = abi_decode(["bytes", "bytes[]"], sinputs[0])
    assert abi_decode([f"({rh_dex.KEY_T},bool,uint128,uint128,bytes)"], sparams[0])[0][1] is False


def test_buy_sends_value_and_books_wallet_token_delta():
    calls = {}

    async def fake_rpc(method, params):
        return "0x" + (5 * 10**21).to_bytes(32, "big").hex() + (0).to_bytes(32, "big").hex()

    async def fake_simulate(to, data, value=0, sender=None):
        calls["sim_value"] = value
        return "0x"

    async def fake_send(to, data="0x", value=0, gas_limit=None):
        calls["send"] = (to, value, gas_limit)
        return "0xtx"
    balances = iter([100, 100 + 49 * 10**20])

    async def fake_balance(token, owner=None):
        return next(balances)

    async def fake_receipt(tx):
        return {"ok": True, "gas_cost_wei": 12345, "blockNumber": "0x10"}
    with patch.object(rh_wallet, "rpc", fake_rpc), patch.object(rh_wallet, "simulate", fake_simulate), \
         patch.object(rh_wallet, "send", fake_send), patch.object(rh_wallet, "erc20_balance", fake_balance), \
         patch.object(rh_wallet, "wait_receipt", fake_receipt):
        fill = asyncio.run(rh_dex.buy(TOKEN, 10**16, 2.0))
    assert calls["sim_value"] == 10**16 and calls["send"] == (rh_dex.UNIVERSAL_ROUTER, 10**16, rh_dex.GAS_SWAP)
    assert fill["tokens_raw"] == 49 * 10**20 and fill["quote_wei"] == 10**16 and fill["venue"] == "pool" and fill["tx"] == "0xtx"
    assert fill["fee_wei"] == 10**16 - int(10**16 * 49 * 10**20 / (5 * 10**21)) and fill["gas_cost_wei"] == 12345


def test_live_seasoned_routes_to_pool_buy_and_erc20_quote_is_refused():
    st = make_state()
    st.config.rh_live_trading = True
    now = time.time()
    b = hot_bucket(st.rh_discovery, now)
    b.update(graduated=True, graduated_at=now - 60, pool_live=True, last_pool_swap_ts=now - 3, quote_symbol="ETH")
    assert st.rh_paper._gates(TOKEN, b, now) not in ("rh-seasoned-live-unsupported", "rh-seasoned-quote-not-eth", "rh-grad-no-pool")
    b["quote_symbol"] = "USDG"
    assert st.rh_paper.live_ok(b) is False                      # ERC-20-quoted pools stay paper (no Permit2 buy path)
    b["quote_symbol"] = "ETH"
    import rh_paper as rp
    fill = {"tx": "0xabc", "quote_wei": 10**16, "tokens_raw": 10**21, "fee_wei": 1, "gas_cost_wei": 2, "latency_s": 0.4, "block": 7, "venue": "pool"}
    with patch.object(rp.rh_dex, "buy", new=AsyncMock(return_value=fill)) as pool_buy, \
         patch.object(rp.rh_live, "buy", new=AsyncMock()) as curve_buy, \
         patch.object(rp.rh_wallet, "balance_wei", new=AsyncMock(return_value=10**18)):
        got = asyncio.run(st.rh_paper._live_buy(TOKEN, b, 0.01, 2e-9))
    assert got is fill and pool_buy.await_count == 1 and curve_buy.await_count == 0
    assert pool_buy.await_args.args[0] == TOKEN and pool_buy.await_args.args[1] == 10**16
