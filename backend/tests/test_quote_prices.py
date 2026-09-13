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
    monkeypatch.setattr(qp, "_fetcher", lambda sym: _fake({"MSFT": 495.63, "TSLA": 310.0, "cbBTC": 77000.0}))
    n = asyncio.run(qp.refresh(["ETH", "USDG", "MSFT", "TSLA", "cbBTC", "MSFT", "?", None]))
    assert n == 3
    assert qp.quote_usd("MSFT") == 495.63 and qp.quote_usd("cbBTC") == 77000.0
    assert qp.quote_usd("USDG") == 1.0 and qp.quote_usd("ETH") == 0.0     # ETH is priced by the existing cache
    snap = qp.snapshot()
    assert snap["MSFT"]["usd"] == 495.63 and snap["MSFT"]["source"] == "yahoo" and snap["cbBTC"]["source"] == "coinbase"


def test_cache_ttl_and_miss_backoff(monkeypatch):
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
    st = make_state()
    now = time.time()
    b = hot_bucket(st.rh_discovery, now, quote="MSFT")
    assert st.rh_paper._gates(TOKEN, b, now) == "unpriced-quote"
    monkeypatch.setattr(qp, "_fetcher", lambda sym: _fake({"MSFT": 495.0}))
    asyncio.run(qp.refresh(b["quote_symbol"] for b in st.rh_discovery.tracking.values()))
    assert st.rh_discovery._quote_usd("MSFT") == 495.0
    assert st.rh_paper._gates(TOKEN, b, now) != "unpriced-quote"
    assert "MSFT" in st.rh_discovery.status()["quote_prices"]
