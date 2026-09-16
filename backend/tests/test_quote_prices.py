"""Stock/ETF/cbBTC-quoted RH curves must be priced so they don't die as `unpriced-quote`."""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import quote_prices as qp
from tests.test_rh_paper import make_state, hot_bucket, TOKEN


def _fake(prices: dict):
    async def f(client, sym):
        if sym not in prices:
            raise RuntimeError("no such symbol")
        return prices[sym]
    return f


def setup_function():
    qp._cache.clear()


def test_refresh_prices_stocks_and_btc_and_skips_eth_usdg(monkeypatch):
    monkeypatch.setattr(qp, "FEEDS", {}); monkeypatch.setattr(qp, "_feed_dir_ts", time.time())   # web-fallback path under test
    monkeypatch.setattr(qp, "_fetcher", lambda sym: _fake({"MSFT": 495.63, "TSLA": 310.0, "cbBTC": 77000.0}))
    n = asyncio.run(qp.refresh(["ETH", "USDG", "MSFT", "TSLA", "cbBTC", "MSFT", "?", None]))
    assert n == 3
    assert qp.quote_usd("MSFT") == 495.63 and qp.quote_usd("cbBTC") == 77000.0
    assert qp.quote_usd("USDG") == 1.0 and qp.quote_usd("ETH") == 0.0     # ETH is priced by the existing cache
    snap = qp.snapshot()
    assert snap["MSFT"]["usd"] == 495.63 and snap["MSFT"]["source"] == "yahoo" and snap["cbBTC"]["source"] == "coinbase"


def test_cache_ttl_and_miss_backoff(monkeypatch):
    monkeypatch.setattr(qp, "FEEDS", {}); monkeypatch.setattr(qp, "_feed_dir_ts", time.time())
    calls = []
    async def f(client, sym):
        calls.append(sym)
        if sym == "SPCX":
            raise RuntimeError("private company — no public print")
        return 100.0
    monkeypatch.setattr(qp, "_fetcher", lambda sym: f)
    assert asyncio.run(qp.refresh(["AMD", "SPCX"])) == 2
    assert asyncio.run(qp.refresh(["AMD", "SPCX"])) == 0            # both inside TTL / back-off
    assert qp.quote_usd("SPCX") == 0.0 and qp.quote_usd("AMD") == 100.0
    qp._cache["AMD"]["ts"] -= qp.TTL_S + 1
    assert asyncio.run(qp.refresh(["AMD", "SPCX"])) == 1            # AMD stale again, SPCX still backing off
    assert calls == ["AMD", "SPCX", "AMD"] or calls == ["SPCX", "AMD", "AMD"]
    # a later miss keeps the last good print instead of zeroing it
    qp._cache["AMD"]["ts"] -= qp.TTL_S + 1
    async def fail(client, sym):
        raise RuntimeError("down")
    monkeypatch.setattr(qp, "_fetcher", lambda sym: fail)
    asyncio.run(qp.refresh(["AMD"]))
    assert qp.quote_usd("AMD") == 100.0


def test_stock_quoted_curve_clears_unpriced_gate(monkeypatch):
    monkeypatch.setattr(qp, "FEEDS", {}); monkeypatch.setattr(qp, "_feed_dir_ts", time.time())
    st = make_state()
    now = time.time()
    b = hot_bucket(st.rh_discovery, now, quote="MSFT")
    assert st.rh_paper._gates(TOKEN, b, now) == "unpriced-quote"
    monkeypatch.setattr(qp, "_fetcher", lambda sym: _fake({"MSFT": 495.0}))
    asyncio.run(qp.refresh(b["quote_symbol"] for b in st.rh_discovery.tracking.values()))
    assert st.rh_discovery._quote_usd("MSFT") == 495.0
    assert st.rh_paper._gates(TOKEN, b, now) != "unpriced-quote"
    assert "MSFT" in st.rh_discovery.status()["quote_prices"]


# ---------------- Chainlink-on-chain first, web second; unknown pair tokens learned on-chain ----------------
import sys as _sys, types as _types, asyncio as _asyncio, time as _time


def _round(answer: int, updated_at: int) -> str:
    return "0x" + "".join(f"{v:064x}" for v in (1, answer, updated_at, updated_at, 1))


def test_feed_name_parsing():
    assert qp._parse_feed_name("Robinhood NVDA / USD") == "NVDA"
    assert qp._parse_feed_name("Robinhood DELL-USD") == "DELL"
    assert qp._parse_feed_name("CBBTC / USD") == "cbBTC"
    assert qp._parse_feed_name("USDG / USD") == "USDG"
    assert qp._parse_feed_name("SYRUPUSDC / USDC Exchange Rate") is None


def test_chainlink_first_then_web_fallback(monkeypatch):
    async def fake_rpc(method, params, timeout=20.0):
        if params[0]["data"] == qp.SEL_DECIMALS:
            return "0x" + f"{8:064x}"
        return _round(21303500813, int(_time.time()) - 60)
    monkeypatch.setitem(_sys.modules, "rh_wallet", _types.SimpleNamespace(rpc=fake_rpc))
    qp._cache.clear(); qp._feed_decimals.clear()

    async def no_web(client, sym):
        raise AssertionError("web fallback must not run when the feed answers")
    monkeypatch.setattr(qp, "_yahoo", no_web)
    _asyncio.run(qp._fetch_one(None, "NVDA", _time.time()))
    assert abs(qp.quote_usd("NVDA") - 213.03500813) < 1e-6 and qp._cache["NVDA"]["source"] == "chainlink"

    async def stale_rpc(method, params, timeout=20.0):   # feed older than 3 days → web fallback
        return _round(21303500813, int(_time.time()) - 4 * 86400) if params[0]["data"] == qp.SEL_LATEST_ROUND else "0x" + f"{8:064x}"
    monkeypatch.setitem(_sys.modules, "rh_wallet", _types.SimpleNamespace(rpc=stale_rpc))

    async def web(client, sym):
        return 200.5
    monkeypatch.setattr(qp, "_yahoo", web)
    _asyncio.run(qp._fetch_one(None, "NVDA", _time.time() + 100))
    assert qp.quote_usd("NVDA") == 200.5 and qp._cache["NVDA"]["source"] == "yahoo"
    qp._cache.clear()


def test_unknown_pair_token_is_learned_from_chain():
    import rh_discovery as rd
    pair = "0x" + "ab" * 20
    rd.QUOTES.pop(pair, None)
    st = _types.SimpleNamespace(config=_types.SimpleNamespace(), rh_paper=None)
    disc = rd.RHDiscovery(st)
    log = {"topics": ["0x" + "0" * 64, "0x" + "0" * 24 + "11" * 20, "0x" + "0" * 24 + "22" * 20, "0x" + "0" * 24 + "33" * 20],
           "data": "0x" + "0" * 24 + "ab" * 20 + f"{1:064x}" + f"{5_000_000_000:064x}", "blockNumber": "0x10"}
    d = rd.decode_launch(log)
    assert d["quote_symbol"] == "?" and d["pair_token"] == pair
    tok = "0x" + "11" * 20
    disc.tracking[tok] = disc._new_bucket(d, start=_time.time())

    def enc_str(s):
        b = s.encode()
        return "0x" + f"{32:064x}" + f"{len(b):064x}" + b.hex().ljust(64, "0")

    async def fake_rpc(calls):
        return [enc_str("ORCL"), "0x" + f"{18:064x}"]
    disc._rpc = fake_rpc
    _asyncio.run(disc._resolve_pair_token(pair))
    assert rd.QUOTES[pair] == ("ORCL", 18) and disc.tracking[tok]["quote_symbol"] == "ORCL" and tok in disc._dirty
    assert qp.has_feed("ORCL")
    rd.QUOTES.pop(pair, None); rd.QUOTE_BY_SYMBOL.pop("ORCL", None)
