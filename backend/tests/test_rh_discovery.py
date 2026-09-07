"""Robinhood Chain (PONS V2) watch-only feed — decoding + isolation tests."""
import asyncio
import os
import sys
import time
from collections import deque
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
os.environ.setdefault("RH_RPC_URL", "https://rpc.mainnet.chain.robinhood.com")

import rh_discovery as rh  # noqa: E402
from models import BotConfig, Launch  # noqa: E402


def _pad_addr(a: str) -> str:
    return "0x" + a[2:].lower().rjust(64, "0")


def _w(n: int) -> str:
    return hex(n)[2:].rjust(64, "0")


TOKEN = "0xa027116861ce778bbf5ba8b14d06ebfdbfd4f8db"
CURVE = "0x99ed793d8c38ce449d778f76ed61bbe2225eb911"
DEPLOYER = "0x92222a481f48b0aa2a07f2cd01b1826dfd80c50a"
BUYER = "0x1111111111111111111111111111111111111111"


def launched_log(pair="0x" + "0" * 40, threshold=4_200_000_000_000_000_000, block=100):
    return {
        "address": rh.FACTORY,
        "topics": [rh.T_LAUNCHED, _pad_addr(TOKEN), _pad_addr(CURVE), _pad_addr(DEPLOYER)],
        "data": "0x" + _pad_addr(pair)[2:] + _w(7) + _w(threshold),
        "blockNumber": hex(block),
        "logIndex": "0x0",
    }


def buy_log(quote_in: int, tokens_out: int, block=101, wallet=BUYER, fee: int = 0, tax: int = 0):
    return {
        "address": CURVE,
        "topics": [rh.T_BUY, _pad_addr(wallet), _pad_addr(wallet)],
        "data": "0x" + _w(quote_in) + _w(tokens_out) + _w(fee) + _w(tax),
        "blockNumber": hex(block),
        "logIndex": "0x1",
    }


def sell_log(tokens_in: int, quote_out: int, block=102):
    return {
        "address": CURVE,
        "topics": [rh.T_SELL, _pad_addr(BUYER), _pad_addr(BUYER)],
        "data": "0x" + _w(tokens_in) + _w(quote_out) + _w(0) + _w(0),
        "blockNumber": hex(block),
        "logIndex": "0x2",
    }


class FakeCol:
    def __init__(self):
        self.ops = []

    async def update_one(self, *a, **k):
        self.ops.append(("update_one", a, k))

    async def delete_many(self, *a, **k):
        self.ops.append(("delete_many", a, k))


def make_state(**cfg):
    return SimpleNamespace(
        config=BotConfig(**cfg),
        db=SimpleNamespace(launches=FakeCol()),
        tracking={},
        active_trades={},
        entered_mints=set(),
    )


def test_topic_hashes_match_verified_keccak():
    from Crypto.Hash import keccak

    def k(sig):
        h = keccak.new(digest_bits=256)
        h.update(sig.encode())
        return "0x" + h.hexdigest()

    assert k("TokenLaunched(address,address,address,address,uint256,uint256)") == rh.T_LAUNCHED
    assert k("CurveBuy(address,address,uint256,uint256,uint256,uint256)") == rh.T_BUY
    assert k("CurveSell(address,address,uint256,uint256,uint256,uint256)") == rh.T_SELL
    assert k("LaunchSwept(address,uint256,uint256)") == rh.T_SWEPT
    assert k("PoolGraduated(address,uint256,uint256,uint256)") == rh.T_GRADUATED


def test_decode_launch_eth_quote():
    d = rh.decode_launch(launched_log())
    assert d["token"] == TOKEN and d["curve"] == CURVE and d["deployer"] == DEPLOYER
    assert d["quote_symbol"] == "ETH" and d["quote_decimals"] == 18
    assert abs(d["graduation_threshold"] - 4.2) < 1e-9


def test_decode_launch_usdg_and_stock_quote():
    usdg = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
    d = rh.decode_launch(launched_log(pair=usdg, threshold=8_090_000_000))
    assert d["quote_symbol"] == "USDG" and abs(d["graduation_threshold"] - 8090.0) < 1e-6
    rddt = "0x05b37fb53a299a1b874a619e1c4c404d52c36f4c"
    d = rh.decode_launch(launched_log(pair=rddt, threshold=42 * 10**18))
    assert d["quote_symbol"] == "RDDT" and d["quote_decimals"] == 18


def test_decode_trade_price():
    tr = rh.decode_trade(buy_log(10**16, 5_799_502_899_751_450_000_000_000), 18)
    assert tr["side"] == "buy" and tr["wallet"] == BUYER
    assert abs(tr["quote"] - 0.01) < 1e-12
    assert abs(tr["price"] - 1.7242857e-9) < 1e-14
    ts = rh.decode_trade(sell_log(10**24, 10**15), 18)
    assert ts["side"] == "sell" and abs(ts["quote"] - 0.001) < 1e-12
    # fee + tax are stripped from the buy price (protocol take ≠ curve price)
    tf = rh.decode_trade(buy_log(10**16, 10**24, fee=10**14, tax=10**14), 18)
    assert abs(tf["price"] - 9.8e-3 * 1e-6 / 1e0) < 1e-15 or abs(tf["price"] - (0.0098 / 1e6)) < 1e-15


def test_ingest_builds_bucket_curve_pct_and_mc():
    st = make_state()
    disc = rh.RHDiscovery(st)
    rh._eth_usd_cache.update(price=3000.0, ts=time.time())
    now = time.time()
    new = asyncio.run(disc._ingest_factory_logs([launched_log()], head=105, now=now))
    assert new == [TOKEN]
    # on-curve trade: (0 + 1.68)·1e9 = (1.68 + 1.68)·5e8 → 1.68 ETH buys exactly 500M tokens
    disc._ingest_trade_logs([buy_log(1_680_000_000_000_000_000, 500_000_000 * 10**18)], now)
    b = disc.tracking[TOKEN]
    assert b["chain"] == "rh" and b["protocol"] == "pons"
    assert abs(b["curve_fill_pct"] - 40.0) < 1e-6           # 1.68 / 4.2
    assert b["buy_count"] == 1 and len(b["buyers"]) == 1
    # exact curve state recovered from the trade itself; price is the marginal (spot) price, not the trade average
    assert abs(b["curve_a"] - 3.36) < 1e-9
    spot = 3.36 ** 2 / 1.68e9
    assert abs(b["last_price_quote"] - spot) < 1e-18 and spot > 1.68 / 5e8   # spot 6.72e-9 > average 3.36e-9
    assert abs(b["usd_market_cap"] - spot * 1e9 * 3000) < 1e-3
    # sell 100M tokens: x 5e8 → 6e8, a 3.36 → 2.8, gross quote out 0.56 ETH
    disc._ingest_trade_logs([sell_log(100_000_000 * 10**18, 560_000_000_000_000_000)], now)
    assert abs(b["curve_a"] - 2.8) < 1e-9
    assert abs(b["curve_fill_pct"] - (1.68 - 0.56) / 4.2 * 100) < 1e-6
    assert b["sell_count"] == 1


def test_graduation_marks_bucket():
    st = make_state()
    disc = rh.RHDiscovery(st)
    now = time.time()
    asyncio.run(disc._ingest_factory_logs([launched_log()], head=105, now=now))
    grad = {
        "address": rh.FACTORY,
        "topics": [rh.T_GRADUATED, _pad_addr(TOKEN)],
        "data": "0x" + _w(1) + _w(2) + _w(3),
        "blockNumber": hex(120),
        "logIndex": "0x0",
    }
    asyncio.run(disc._ingest_factory_logs([grad], head=125, now=now))
    b = disc.tracking[TOKEN]
    assert b["graduated"] is True and b["curve_fill_pct"] == 100.0


def test_rh_tokens_never_touch_bot_tracking():
    st = make_state()
    disc = rh.RHDiscovery(st)
    now = time.time()
    asyncio.run(disc._ingest_factory_logs([launched_log()], head=105, now=now))
    disc._ingest_trade_logs([buy_log(10**16, 10**24)], now)
    assert TOKEN in disc.tracking
    assert TOKEN not in st.tracking, "RH tokens must be isolated from the entry path"


def test_candidates_snapshot_band_and_watch_only():
    st = make_state(band_new_min_age_min=0.0, band_new_max_age_min=15.0,
                    scanner_min_growth_pct_new=50.0, scanner_min_new_buyers_new=2)
    disc = rh.RHDiscovery(st)
    rh._eth_usd_cache.update(price=3000.0, ts=time.time())
    now = time.time()
    asyncio.run(disc._ingest_factory_logs([launched_log()], head=105, now=now))
    b = disc.tracking[TOKEN]
    b["start"] = now - 120
    disc.apply_trade(b, {"side": "buy", "wallet": BUYER, "quote": 0.01, "tokens": 1e7, "price": 1e-9, "block": 1}, now - 30)
    b["last_price_sample_ts"] = 0
    disc.apply_trade(b, {"side": "buy", "wallet": "0x2222222222222222222222222222222222222222", "quote": 0.02, "tokens": 1e7, "price": 2e-9, "block": 2}, now)
    snap = disc.candidates_snapshot()
    assert len(snap) == 1
    c = snap[0]
    assert c["band"] == "rh_new" and c["chain"] == "rh" and c["watch_only"] is True
    assert c["unique_buyers_total"] == 2 and c["new_buyers_recent"] == 2
    assert abs(c["growth_pct"] - 100.0) < 1e-6
    assert c["passes"] is True
    # Outside the New age window → dropped
    b["start"] = now - 3600
    assert disc.candidates_snapshot() == []


def test_launch_model_defaults_chain_sol():
    l = Launch(mint="m", creator="c", bonding_curve="b")
    assert l.chain == "sol" and l.protocol is None
    assert BotConfig().rh_feed_enabled is True


def test_launch_fields_payload():
    st = make_state()
    disc = rh.RHDiscovery(st)
    now = time.time()
    asyncio.run(disc._ingest_factory_logs([launched_log()], head=105, now=now))
    b = disc.tracking[TOKEN]
    f = disc._launch_fields(b)
    assert set(f) == {"unique_buyers", "buy_count", "curve_fill_pct", "quote_inflow",
                      "quote_symbol", "price_quote", "usd_market_cap", "graduated"}


def test_gc_drops_stale_and_caps():
    st = make_state()
    disc = rh.RHDiscovery(st)
    now = time.time()
    for i in range(5):
        tok = f"0x{i:040x}"
        disc.tracking[tok] = disc._new_bucket({
            "token": tok, "curve": f"0x{i + 100:040x}", "deployer": DEPLOYER, "pair_token": "0x" + "0" * 40,
            "quote_symbol": "ETH", "quote_decimals": 18, "graduation_threshold": 4.2, "block": 1,
        }, start=now - (rh.TRACK_MAX_AGE_S + 10 if i < 2 else 10))
        disc._curve_to_token[f"0x{i + 100:040x}"] = tok
    disc._gc(now)
    assert len(disc.tracking) == 3 and len(disc._curve_to_token) == 3
