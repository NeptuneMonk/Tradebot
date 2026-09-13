"""ERC-20-quoted RH curves/pools (USDG, tokenized stocks) can go live behind `rh_live_erc20_quotes`, and seasoned
entries are cost-gated by the pool's real round trip (V4Quoter buy → sell) instead of the curve model."""
import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

from eth_abi import decode as abi_decode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cost_gate
import quote_prices as qp
import rh_dex
import rh_discovery as rd
import rh_live
import rh_paper as rp
import rh_wallet
from tests.test_rh_paper import make_state, hot_bucket, TOKEN

USDG, USDG_DEC = rd.quote_of("USDG")
MSFT, _ = rd.quote_of("MSFT")
LOW_TOKEN = "0x0437c6537e5044f6ee84c6cbbccb755ed481a698"     # BABA-quoted pool seen on-chain: meme token is currency0


def _swap_params(calldata_hex: str):
    data = bytes.fromhex(calldata_hex[2:])
    _cmds, inputs, _deadline = abi_decode(["bytes", "bytes[]", "uint256"], data[4:])
    _actions, params = abi_decode(["bytes", "bytes[]"], inputs[0])
    swap = abi_decode([f"({rh_dex.KEY_T},bool,uint128,uint128,bytes)"], params[0])[0]
    settle = abi_decode(["address", "uint256"], params[1])
    take = abi_decode(["address", "uint256"], params[2])
    return swap, settle, take


def test_pool_key_sorts_currencies_and_flags_token_side():
    assert rh_dex.pool_key(TOKEN)[0] == rh_dex.NATIVE and rh_dex.token_is_c0(TOKEN) is False
    key = rh_dex.pool_key(TOKEN, USDG)
    assert int(key[0], 16) < int(key[1], 16) and key[2:] == (rh_dex.FEE, rh_dex.TICK_SPACING, rh_wallet.checksum(rh_dex.HOOK))
    assert rh_dex.token_is_c0(TOKEN, USDG) is False           # 0xa027… > USDG 0x5fc5…
    assert rh_dex.token_is_c0(LOW_TOKEN, rd.quote_of("BABA")[0]) is True
    assert rh_dex.pool_id(TOKEN, USDG) != rh_dex.pool_id(TOKEN)


def test_price_and_swap_decode_follow_the_token_side_and_quote_decimals():
    sqrt = int((2.0 ** 0.5) * rh_dex.Q96)                     # c1/c0 = 2 raw
    assert abs(rh_dex.price_from_sqrt(sqrt) - 0.5) < 1e-9                       # ETH c0: 2 tokens per wei → 0.5 ETH/token
    assert abs(rh_dex.price_from_sqrt(sqrt, True, 18) - 2.0) < 1e-9             # token c0: 2 quote raw per token raw
    assert abs(rh_dex.price_from_sqrt(sqrt, False, 6) - 0.5 * 1e12) < 1e-3      # USDG (6-dec) c0: scale by 1e12
    data = "0x" + (-5_000_000 % 2**256).to_bytes(32, "big").hex() + (3 * 10**18).to_bytes(32, "big").hex() \
        + sqrt.to_bytes(32, "big").hex() + (0).to_bytes(32, "big").hex() + (0).to_bytes(32, "big").hex() + (0).to_bytes(32, "big").hex()
    log = {"data": data, "topics": ["0x", "0x" + "1" * 64, "0x" + "0" * 24 + "ab" * 20], "blockNumber": "0x10"}
    tr = rh_dex.decode_swap(log, False, 6)                     # quote is c0 (paid 5 USDG), token is c1 (got 3)
    assert tr["side"] == "buy" and tr["quote"] == 5.0 and tr["tokens"] == 3.0
    tr2 = rh_dex.decode_swap(log, True, 18)                    # token is c0 (paid 5e-12 tokens), quote c1 received → sell
    assert tr2["side"] == "sell" and tr2["quote"] == 3.0


def test_erc20_buy_calldata_settles_the_quote_and_buy_goes_through_permit2_with_zero_value():
    cd = rh_dex.build_buy_calldata(TOKEN, 7 * 10**6, 10**20, deadline=1, quote=USDG)
    swap, settle, take = _swap_params(cd)
    assert swap[1] is True and swap[2] == 7 * 10**6          # USDG is currency0 → zeroForOne buy
    assert settle[0].lower() == USDG and settle[1] == 7 * 10**6 and take[0].lower() == TOKEN
    scd = rh_dex.build_sell_calldata(TOKEN, 10**20, 5 * 10**6, deadline=1, quote=USDG)
    sswap, ssettle, stake = _swap_params(scd)
    assert sswap[1] is False and stake[0].lower() == USDG and ssettle[0].lower() == TOKEN
    # token on the low side: directions flip
    bswap, _, _ = _swap_params(rh_dex.build_buy_calldata(LOW_TOKEN, 10**18, 1, deadline=1, quote=rd.quote_of("BABA")[0]))
    assert bswap[1] is False

    calls = {}

    async def fake_quote(token, amt, quote=rh_dex.NATIVE):
        return 10**20

    async def fake_permit2(token, amt):
        calls["permit2"] = (token, amt)
        return 777

    async def fake_simulate(to, data, value=0, sender=None):
        calls["sim_value"] = value
        return "0x"

    async def fake_send(to, data="0x", value=0, gas_limit=None):
        calls["send_value"] = value
        return "0xtx"
    balances = iter([0, 10**20])

    async def fake_balance(token, owner=None):
        return next(balances)

    async def fake_receipt(tx):
        return {"ok": True, "gas_cost_wei": 100, "blockNumber": "0x10"}
    with patch.object(rh_dex, "quote_buy", fake_quote), patch.object(rh_dex, "ensure_permit2", fake_permit2), \
         patch.object(rh_wallet, "simulate", fake_simulate), patch.object(rh_wallet, "send", fake_send), \
         patch.object(rh_wallet, "erc20_balance", fake_balance), patch.object(rh_wallet, "wait_receipt", fake_receipt):
        fill = asyncio.run(rh_dex.buy(TOKEN, 7 * 10**6, 2.0, quote=USDG))
    assert calls["permit2"] == (USDG, 7 * 10**6) and calls["sim_value"] == 0 and calls["send_value"] == 0
    assert fill["quote_wei"] == 7 * 10**6 and fill["tokens_raw"] == 10**20 and fill["gas_cost_wei"] == 877


def test_curve_buy_with_erc20_quote_approves_the_curve_and_sends_zero_value():
    calls = {}
    curve = "0x" + "c" * 40

    async def fake_allowance(token, spender, amount):
        calls["approve"] = (token, spender, amount)
        return "0xapprove"

    async def fake_receipt(tx):
        return {"ok": True, "gas_cost_wei": 50, "blockNumber": "0x10", "logs": []}

    async def fake_simulate(to, data, value=0, sender=None):
        calls.setdefault("sim_values", []).append(value)
        return "0x" + (10**21).to_bytes(32, "big").hex()

    async def fake_send(to, data="0x", value=0, gas_limit=None):
        calls["send"] = (to, value)
        return "0xtx"
    with patch.object(rh_wallet, "ensure_allowance", fake_allowance), patch.object(rh_wallet, "wait_receipt", fake_receipt), \
         patch.object(rh_wallet, "simulate", fake_simulate), patch.object(rh_wallet, "send", fake_send):
        fill = asyncio.run(rh_live.buy(curve, 12 * 10**6, 1e-6, 5.0, quote_token=USDG, quote_decimals=6))
    assert calls["approve"] == (USDG, curve, 12 * 10**6) and calls["send"] == (curve, 0) and set(calls["sim_values"]) == {0}
    assert fill["quote_wei"] == 12 * 10**6 and fill["tokens_raw"] == 10**21 and fill["gas_cost_wei"] == 100
    # fallback estimate when the simulation is unavailable: 12 USDG at 2 USDG/token → 6 tokens (18-dec)
    assert asyncio.run(rh_live.quote_buy(curve, 12 * 10**6, 2.0, USDG, 6)) == 6 * 10**18


def test_live_ok_gates_erc20_quotes_behind_the_flag_and_a_price():
    qp._cache.clear()
    st = make_state(rh_live_trading=True)
    b = hot_bucket(st.rh_discovery, time.time(), quote="USDG")
    b["pair_token"], b["quote_decimals"] = USDG, USDG_DEC
    assert st.rh_paper.live_ok(b) is False                    # flag off → paper
    st.config.rh_live_erc20_quotes = True
    assert st.rh_paper.live_ok(b) is True                     # USDG priced at 1
    b.update(quote_symbol="MSFT", pair_token=MSFT)
    assert st.rh_paper.live_ok(b) is False                    # no USD print yet
    qp._cache["MSFT"] = {"price": 500.0, "ts": time.time(), "source": "test"}
    assert st.rh_paper.live_ok(b) is True
    b.update(quote_symbol="?", pair_token="0x" + "e" * 40)
    assert st.rh_paper.live_ok(b) is False                    # unknown quote never goes live
    b.update(quote_symbol="ETH", pair_token=rh_dex.NATIVE)
    st.config.rh_live_erc20_quotes = False
    assert st.rh_paper.live_ok(b) is True


def test_live_buy_erc20_needs_the_wallet_to_hold_the_quote_and_uses_its_decimals():
    st = make_state(rh_live_trading=True, rh_live_erc20_quotes=True)
    now = time.time()
    b = hot_bucket(st.rh_discovery, now, quote="USDG")
    b["pair_token"], b["quote_decimals"] = USDG, USDG_DEC
    with patch.object(rp.rh_wallet, "balance_wei", new=AsyncMock(return_value=10**18)), \
         patch.object(rp.rh_wallet, "erc20_balance", new=AsyncMock(return_value=3 * 10**6)), \
         patch.object(rp.rh_live, "buy", new=AsyncMock()) as curve_buy:
        assert asyncio.run(st.rh_paper._live_buy(TOKEN, b, 5.0, 2e-9)) is None       # holds 3 USDG < 5 stake
    assert curve_buy.await_count == 0 and st.rh_paper.stats["quote_balance_skips"] == 1 and "3.0000 USDG" in st.rh_paper.last_live_error
    fill = {"tx": "0xabc", "quote_wei": 5 * 10**6, "tokens_raw": 10**21, "fee_wei": 0, "gas_cost_wei": 2, "latency_s": 0.4, "block": 7}
    with patch.object(rp.rh_wallet, "balance_wei", new=AsyncMock(return_value=10**18)), \
         patch.object(rp.rh_wallet, "erc20_balance", new=AsyncMock(return_value=50 * 10**6)), \
         patch.object(rp.rh_live, "buy", new=AsyncMock(return_value=fill)) as curve_buy:
        got = asyncio.run(st.rh_paper._live_buy(TOKEN, b, 5.0, 2e-9))
    assert got is fill and curve_buy.await_args.args[1] == 5 * 10**6           # 6-dec raw stake, not 1e18
    assert curve_buy.await_args.kwargs == {"quote_token": USDG, "quote_decimals": 6}
    b.update(graduated=True, pool_live=True)
    with patch.object(rp.rh_wallet, "balance_wei", new=AsyncMock(return_value=10**18)), \
         patch.object(rp.rh_wallet, "erc20_balance", new=AsyncMock(return_value=50 * 10**6)), \
         patch.object(rp.rh_dex, "buy", new=AsyncMock(return_value={**fill, "venue": "pool"})) as pool_buy:
        asyncio.run(st.rh_paper._live_buy(TOKEN, b, 5.0, 2e-9))
    assert pool_buy.await_args.args[1] == 5 * 10**6 and pool_buy.await_args.kwargs == {"quote": USDG}


def test_cost_gate_measured_round_trip_replaces_the_model():
    kw = dict(size_usd=10.0, r_usd=2.0, first_target_r=1.0, protocol="rh", entry_slip_bps=800, exit_slip_bps=800,
              fee_usd_round_trip=0.2, ladder=False, depth_usd=5000.0)
    model = cost_gate.quote(**kw)
    meas = cost_gate.quote(**kw, measured_round_trip_pct=6.4)
    assert model["cost_breakdown"]["measured"] is False and meas["cost_breakdown"]["measured"] is True
    assert meas["cost_breakdown"]["protocol_pct"] == 0 and abs(meas["cost_breakdown"]["slip_pct"] - 6.4) < 1e-6
    assert abs(meas["expected_cost_pct"] - (6.4 + 2.0 + cost_gate.TOKEN_SHAVE_PCT)) < 1e-6      # + gas 2% + shave
    assert meas["cost_gate_pass"] is True                     # 20% first target ≥ 2 × 8.9%, under the 12% measured ceiling
    assert cost_gate.quote(**kw, measured_round_trip_pct=11.7)["cost_gate_pass"] is False   # 14.2% > 12% ceiling
    assert cost_gate.quote(**{**kw, "first_target_r": 3.0}, measured_round_trip_pct=9.0)["cost_gate_pass"] is True
    assert cost_gate.quote(**kw, measured_round_trip_pct=9.0)["cost_gate_pass"] is False    # 20% < 2 × 11.5%


def test_seasoned_entry_uses_the_pool_round_trip_and_blocks_an_expensive_pool():
    st = make_state()
    now = time.time()
    b = hot_bucket(st.rh_discovery, now)
    b.update(graduated=True, graduated_at=now - 60, pool_live=True, last_pool_swap_ts=now - 3)

    async def cheap(token, raw, quote=rh_dex.NATIVE):
        assert token == TOKEN and quote == rh_dex.NATIVE and raw > 0
        return {"quote_in": raw, "tokens_raw": 10**21, "quote_back": int(raw * 0.94), "cost_pct": 6.0}
    with patch.object(rp.rh_dex, "round_trip", cheap):
        asyncio.run(st.rh_paper._enter(TOKEN))
    pb = st.rh_paper.pending_buys.get(TOKEN)
    assert pb and pb["ctx"]["plan"]["pool_round_trip_pct"] == 6.0 and b["pool_round_trip_pct"] == 6.0
    assert st.rh_paper.stats["pool_quotes"] == 1
    st.rh_paper.pending_buys.clear()
    st.rh_paper._pending_entries.clear()

    async def pricey(token, raw, quote=rh_dex.NATIVE):
        return {"quote_in": raw, "tokens_raw": 10**21, "quote_back": int(raw * 0.88), "cost_pct": 11.7}
    with patch.object(rp.rh_dex, "round_trip", pricey):
        asyncio.run(st.rh_paper._enter(TOKEN))
    assert TOKEN not in st.rh_paper.pending_buys and TOKEN not in st.rh_paper.positions
    assert st.rh_paper.stats["cost_gate_skips"] == 1 and TOKEN not in st.rh_paper._pending_entries

    async def broken(token, raw, quote=rh_dex.NATIVE):
        raise RuntimeError("quoter down")
    with patch.object(rp.rh_dex, "round_trip", broken):
        assert asyncio.run(st.rh_paper._pool_round_trip_pct(TOKEN, b, 0.004)) == 2 * rp.POOL_FEE_FRACTION * 100 + cost_gate.ADVERSE_FILL_PCT
    assert st.rh_paper.stats["pool_quote_failures"] == 1


def test_quote_lookup_and_trade_doc_carry_the_quote_asset():
    assert rd.quote_of("USDG") == ("0x5fc5360d0400a0fd4f2af552add042d716f1d168", 6)
    assert rd.quote_of("LMT")[0] == "0x329fcaceb9ad6f9580dd5f643fed0646900d043c" and rd.quote_of("BABA")[1] == 18
    assert rd.quote_of(None) == (rh_dex.NATIVE, 18) and rd.QUOTES[rd.quote_of("LMT")[0]] == ("LMT", 18)
    st = make_state()
    assert rp.RHPaperTrader._quote_asset({"quote_symbol": "USDG"}) == (USDG, 6)
    assert rp.RHPaperTrader._quote_asset({"pair_token": MSFT, "quote_decimals": 18}) == (MSFT, 18)
    assert rp.RHPaperTrader._qscale({"quote_symbol": "USDG"}) == 1e6
    now = time.time()
    b = hot_bucket(st.rh_discovery, now, quote="USDG")
    b["pair_token"], b["quote_decimals"] = USDG, USDG_DEC
    asyncio.run(st.rh_paper._open_position(TOKEN, b, 2e-9, 5.0, 1.0, now, None, {"plan": {}}))
    doc = st.rh_paper.positions[TOKEN]["trade"]
    assert doc["pair_token"] == USDG and doc["quote_decimals"] == 6 and doc["quote_symbol"] == "USDG"
