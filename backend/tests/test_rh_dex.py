"""Post-graduation DEX path: pool key / calldata / swap decoding (offline, verified against a real
PoolGraduated tx on chain 4663) plus rh_paper routing when a held token graduates."""
import asyncio
import os
import sys
import time

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

import rh_dex
import rh_paper as rp
from tests.test_rh_paper import TOKEN, enter, hot_bucket, make_state

GRAD_TOKEN = "0x6ee1d6327800516e6098301b00b68ad19f84391c"
GRAD_POOL_ID = "0x54ecfbd742598c84b2454d77a700af43be0f6fef3ffc6f4922c833ae86199bde"
# PoolManager.Swap emitted inside the graduation tx (router bought 0.08 ETH worth)
SWAP_LOG = {
    "address": rh_dex.POOL_MANAGER,
    "topics": [rh_dex.T_SWAP, GRAD_POOL_ID, "0x000000000000000000000000630d72024e10c3e2160352dd2cdf24736c783e70"],
    "data": "0x" + (2**128 - 80000000000000000).to_bytes(16, "big").rjust(32, b"\xff").hex()
            + (3814609956131985364861718).to_bytes(32, "big").hex()
            + (541953992363893511156524725921528).to_bytes(32, "big").hex()
            + (29277002188455996370971).to_bytes(32, "big").hex()
            + (176620).to_bytes(32, "big").hex()
            + (0).to_bytes(32, "big").hex(),
    "blockNumber": "0x36978e8",
    "logIndex": "0x14",
}


def test_pool_id_matches_onchain_initialize_event():
    assert "0x" + rh_dex.pool_id(GRAD_TOKEN).hex() == GRAD_POOL_ID
    assert rh_dex.pool_key(GRAD_TOKEN)[0] == rh_dex.NATIVE  # ETH is currency0 → zeroForOne=False on sells


def test_price_from_sqrt_inverts_to_eth_per_token():
    sqrt_p = 515061686757469092566193039980925
    p = rh_dex.price_from_sqrt(sqrt_p)
    assert abs(p - 2.366e-8) / 2.366e-8 < 0.01
    assert rh_dex.price_from_sqrt(0) == 0.0


def test_decode_swap_from_graduation_tx():
    tr = rh_dex.decode_swap(SWAP_LOG)
    assert tr["side"] == "buy" and tr["venue"] == "pool"
    assert abs(tr["quote"] - 0.08) < 1e-12
    assert abs(tr["tokens"] - 3814609.956) < 0.01
    assert tr["block"] == 0x36978E8 and tr["price"] > 0
    assert tr["wallet"] == "0x630d72024e10c3e2160352dd2cdf24736c783e70"


def test_sell_calldata_shape():
    data = rh_dex.build_sell_calldata(GRAD_TOKEN, 10**21, 12345, deadline=999)
    raw = bytes.fromhex(data[2:])
    assert raw[:4].hex() == "3593564c"                       # UniversalRouter.execute(bytes,bytes[],uint256)
    assert b"\x10" in raw                                    # V4_SWAP command
    assert bytes([0x06, 0x0C, 0x0F]) in raw                  # SWAP_EXACT_IN_SINGLE → SETTLE_ALL → TAKE_ALL
    assert (10**21).to_bytes(32, "big") in raw and (12345).to_bytes(32, "big") in raw
    assert bytes.fromhex(rh_dex.HOOK[2:]) in raw and bytes.fromhex(GRAD_TOKEN[2:]) in raw


def test_graduation_switches_venue_and_keeps_ladder_running():
    st = make_state(rh_grad_handoff_r_trail=False, stop_loss_pct=12.0, trailing_arm_pct=12.0, trailing_stop_pct=6.0, hold_max_seconds=3500,
                    no_momentum_exit_enabled=False, exit_momentum_gate_enabled=False)
    now = time.time()
    b = hot_bucket(st.rh_discovery, now, price=1e-9, first=5e-10)
    enter(st)
    pos = st.rh_paper.positions[TOKEN]
    t = pos["trade"]
    b["graduated"] = True
    b["last_price_quote"] = 1.05e-9

    async def tick(price, expect_reason):
        b["last_price_quote"] = price
        r = st.rh_paper._decide_exit(pos, b, time.time())
        await asyncio.sleep(0)
        assert r == expect_reason, (price, r)

    asyncio.run(tick(1.05e-9, None))                    # +5%: no forced "graduated" exit any more
    assert t["venue"] == "pool" and t["graduated_during_hold"] is True
    assert abs(t["graduated_at_pnl_pct"] - 5.0) < 0.01
    assert st.rh_paper.stats["graduated_holds"] == 1
    asyncio.run(tick(1.20e-9, None))                    # arms the trail
    asyncio.run(tick(1.10e-9, "trailing_stop"))         # 8.3% off peak on the pool


def test_paper_pool_exit_uses_quoter_and_marks_exit_venue(monkeypatch):
    st = make_state(no_momentum_exit_enabled=False)
    now = time.time()
    b = hot_bucket(st.rh_discovery, now, price=1e-9, first=5e-10)
    enter(st)
    pos = st.rh_paper.positions[TOKEN]
    t = pos["trade"]
    b["graduated"] = True
    st.rh_paper._decide_exit(pos, b, now)
    assert t["venue"] == "pool"
    tokens = float(t["entry_tokens"])

    async def fake_quote(token, raw, quote=rh_dex.NATIVE):
        assert token == TOKEN and raw == int(tokens * 1e18) and quote == rh_dex.NATIVE
        return int(tokens * 2e-9 * 0.97 * 1e18)      # quoter says 2x price minus a 3% hook take

    monkeypatch.setattr(rh_dex, "quote_sell", fake_quote)
    asyncio.run(st.rh_paper.exit(TOKEN, "take_profit"))
    doc = st.db.trades.docs[t["id"]]
    assert doc["status"] == "closed" and doc["exit_venue"] == "pool"
    assert doc["pnl_pct"] > 80                        # ~+94% after the take and paper gas


def test_live_sell_routes_to_pool_when_graduated(monkeypatch):
    st = make_state()
    now = time.time()
    b = hot_bucket(st.rh_discovery, now, price=1e-9, first=5e-10)
    b["graduated"] = True
    calls = []

    async def fake_balance(token, owner=None):
        return 10**21

    async def fake_dex_sell(token, raw, slip, quote=rp.rh_dex.NATIVE):
        calls.append(("pool", token, raw, slip))
        return {"tx": "0xabc", "tokens_raw": raw, "quote_wei": 10**15, "fee_wei": 0, "gas_cost_wei": 10**12,
                "latency_s": 0.3, "block": 5, "venue": "pool"}

    async def fake_curve_sell(*a, **k):
        raise AssertionError("curve sell must not be used after graduation")

    monkeypatch.setattr(rp.rh_wallet, "erc20_balance", fake_balance)
    monkeypatch.setattr(rp.rh_dex, "sell", fake_dex_sell)
    monkeypatch.setattr(rp.rh_live, "sell", fake_curve_sell)
    t = {"mint": TOKEN, "symbol": "TST", "entry_tokens": 1000.0, "entry_tokens_raw": str(10**21), "curve": b["curve"], "mode": "live"}
    fill = asyncio.run(st.rh_paper._live_sell(TOKEN, t, 1e-9))
    assert fill and fill["venue"] == "pool"
    assert calls == [("pool", TOKEN, 10**21, st.config.rh_live_slippage_pct)]


def test_live_sell_falls_back_to_pool_when_curve_closed(monkeypatch):
    st = make_state()
    now = time.time()
    b = hot_bucket(st.rh_discovery, now, price=1e-9, first=5e-10)
    calls = []

    async def fake_balance(token, owner=None):
        return 10**21

    async def fake_curve_sell(*a, **k):
        calls.append("curve")
        raise RuntimeError("execution reverted: CurveClosed()")

    async def fake_spot(token, quote=rp.rh_dex.NATIVE, dec=18):
        return 2e-9                                    # pool initialised ⇒ graduated

    async def fake_dex_sell(token, raw, slip, quote=rp.rh_dex.NATIVE):
        calls.append("pool")
        return {"tx": "0xdef", "tokens_raw": raw, "quote_wei": 10**15, "fee_wei": 0, "gas_cost_wei": 10**12,
                "latency_s": 0.3, "block": 6, "venue": "pool"}

    async def no_sleep(_):
        return None

    monkeypatch.setattr(rp.rh_wallet, "erc20_balance", fake_balance)
    monkeypatch.setattr(rp.rh_live, "sell", fake_curve_sell)
    monkeypatch.setattr(rp.rh_dex, "spot_price", fake_spot)
    monkeypatch.setattr(rp.rh_dex, "sell", fake_dex_sell)
    monkeypatch.setattr(rp.asyncio, "sleep", no_sleep)
    t = {"mint": TOKEN, "symbol": "TST", "entry_tokens": 1000.0, "entry_tokens_raw": str(10**21), "curve": b["curve"], "mode": "live"}
    fill = asyncio.run(st.rh_paper._live_sell(TOKEN, t, 1e-9))
    assert fill and fill["tx"] == "0xdef"
    assert calls == ["curve", "pool"]
    assert b["graduated"] is True and t["venue"] == "pool"


def test_pool_swaps_feed_price_and_event_stops():
    st = make_state(stop_loss_pct=10.0, no_momentum_exit_enabled=False, exit_momentum_gate_enabled=False,
                    paper_exit_latency_ms=600)
    now = time.time()
    b = hot_bucket(st.rh_discovery, now, price=1e-9, first=5e-10)
    enter(st)
    pos = st.rh_paper.positions[TOKEN]
    b["graduated"] = True
    st.rh_paper._decide_exit(pos, b, now)
    assert st.rh_discovery._pool_watch_tokens() == [TOKEN]
    # a pool dump: sqrtP for 0.8e-9 ETH/token → tokens/ETH = 1.25e9 → sqrt = sqrt(1.25e9) * 2^96
    sqrt_p = int((1.25e9) ** 0.5 * 2**96)
    log = {"address": rh_dex.POOL_MANAGER,
           "topics": [rh_dex.T_SWAP, "0x" + rh_dex.pool_id(TOKEN).hex(), "0x" + "9" * 64],
           "data": "0x" + (10**17).to_bytes(32, "big").hex()                                   # +ETH to swapper → sell
                   + (2**256 - 10**23).to_bytes(32, "big").hex()
                   + sqrt_p.to_bytes(32, "big").hex() + (1).to_bytes(32, "big").hex()
                   + (0).to_bytes(32, "big").hex() + (0).to_bytes(32, "big").hex(),
           "blockNumber": hex(1000), "logIndex": "0x1"}
    st.rh_discovery._ingest_pool_swaps([log], now)
    assert abs(b["last_price_quote"] - 0.8e-9) / 0.8e-9 < 1e-6
    assert b["sell_count"] == 1 and st.rh_discovery.stats["pool_swaps_seen"] == 1
    trig = pos.get("exit_trigger")
    assert trig and trig["reason"] == "stop_loss" and trig["block"] == 1000 and trig["fill_block"] == 1006
