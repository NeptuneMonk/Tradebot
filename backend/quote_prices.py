"""USD prices for the non-ETH quote assets RH curves are denominated in (tokenized stocks/ETFs, cbBTC, USDG).

Source order per symbol:
  1. Chainlink feed ON Robinhood Chain (`latestRoundData()` through the feed proxy) — the price the chain itself
     uses, multiplier-adjusted, reachable from any pod IP. Feed proxies come from Chainlink's reference directory
     (refreshed daily) with a built-in seed so a directory outage never blanks the map.
  2. Yahoo chart meta (stocks) / Coinbase (BTC) — web fallback when the feed is missing or stale.
60 s cache, 5 min back-off after a miss. Sync read, async refresh from the discovery poll."""
import asyncio
import logging
import time

import httpx

logger = logging.getLogger(__name__)

TTL_S = 60.0
MISS_TTL_S = 300.0
FEED_STALE_S = 3 * 86400.0        # stock feeds hold the last print over weekends/holidays — older than this is a broken feed
FEED_DIR_URL = "https://reference-data-directory.vercel.app/feeds-robinhood-mainnet.json"
FEED_DIR_TTL_S = 86400.0
SEL_LATEST_ROUND = "0xfeaf968c"   # latestRoundData()
SEL_DECIMALS = "0x313ce567"       # decimals()
STABLES = {"USDG": 1.0}
_cache: dict[str, dict] = {}       # sym → {"price": float, "ts": float, "source": str}
_UA = {"User-Agent": "Mozilla/5.0"}

# Chainlink feed proxies on Robinhood Chain (seed — the directory refresh above extends/overrides it)
FEEDS: dict[str, str] = {
    "AAPL": "0x6B22A786bAa607d76728168703a39Ea9C99f2cD0", "AMD": "0x943A29E7ae51A4798823ca9eEd2ed533B2A22C72",
    "AMZN": "0xD5a1508ceD74c084eBf3cBe853e2C968fB2a651C", "ASML": "0xB4106147E8cce40b7d46124090d373A71b70f87D",
    "BABA": "0x62Cc8F9b5f56a33c9C8A60c8B92779f523c4E984", "CLSK": "0x810c12D3a554Bc47fd39597Fe3b3AAC4941F50eF",
    "COIN": "0xA3a468A452940B7D6b69991207B508c609a98Ef2", "CRCL": "0x6652eDf64bA3731C4F2D3ce821A0Fb1f1f6b482a",
    "CRWV": "0xe1b3aABCAFAd1c94708dc1367dcfF8Aa4407487C", "DELL": "0x1C6c8cADBe02E19129c39dDB92281cE4c0bf206b",
    "EWY": "0xEFdf54610B62A7753Ec30bDc380847c12D32e1D1", "GME": "0x27C71df6A64fB476468EdF256CF72c038baB5B67",
    "GOOGL": "0xF6f373a037c30F0e5010d854385cA89185AE638b", "INTC": "0x3f390C5C24628Ac7C489515402235FeAD71D1913",
    "IONQ": "0x22EfeC4919baf55F360E0EDee4AbEB26DE4971eb", "META": "0x7C38C00C30BEe9378381E7B6135d7283356D71b1",
    "MSFT": "0x45C3C877C15E6BA2EBB19eA114Ea508d14C1Af2E", "MSTR": "0x396118bdFB181e6240E74D243F266B061c0edc3D",
    "MU": "0x425EEFdCf05ed6526C3cE61Af99429A228a6d596", "NBIS": "0xE1D87B116Ba0fe898998f1D140339D1fA1E09705",
    "NVDA": "0x379EC4f7C378F34a1B47E4F3cbeBCbAC3E8E9F15", "ORCL": "0x0e6a64a2B58A6693a531E6c555f3A5d042eEA844",
    "PLTR": "0x820ABedFF239034956B7A9d2F0a331f9F075eB4c", "QQQ": "0x80901d846d5D7B030F26B480776EE3b29374C2ae",
    "RGTI": "0x2A045cF1C49c61c166C036d2f06FA2D2d984f765", "RKLB": "0x045477BF65Aef6f4F2386ad0164579e48381CC74",
    "SGOV": "0xa0DF4ee0fFf975306345875E3548Fcc519577A11", "SLV": "0x209b73908e92Ae021826eD79609845451Ecba2ce",
    "SNDK": "0xfb133Fa4B7b385802B693a293606682Df47109A3", "SPCX": "0xB265810950ba6c5C0Ff821c9963014a56fD8Bffb",
    "SPY": "0x319724394D3A0e3669269846abE664Cd621f9f6A", "TSLA": "0x4A1166a659A55625345e9515b32adECea5547C38",
    "TSM": "0x874cF94aa8eC88Fd9560094dD065f2fB3E41Fc2F", "USAR": "0xA994d3684e8400A6c8078226925779FdeE682DD9",
    "USO": "0x75a9c76Ef439e2C7c2E5a34Ab105EcFe3766431c",
    "cbBTC": "0x0009cD492adf8167f9eEBf1293556A673530a21a", "BTC": "0xa2c5184bF03d373Dc9dE4876eb4Bce595B460251",
    "ETH": "0x78F3556b67E17Df817D51Ef5a990cDaF09E8d3A9", "USDG": "0x61B7e5650328764B076A108EFF5fa7282a1B9aD2",
}
_feed_decimals: dict[str, int] = {}
_feed_dir_ts = 0.0


def quote_usd(sym: str) -> float:
    if sym in STABLES:
        return STABLES[sym]
    return float(_cache.get(sym, {}).get("price") or 0.0)


def snapshot() -> dict:
    now = time.time()
    return {s: {"usd": round(c["price"], 4), "age_s": int(now - c["ts"]), "source": c["source"]}
            for s, c in _cache.items() if c.get("price")}


def has_feed(sym: str) -> bool:
    return sym in FEEDS


def _parse_feed_name(name: str) -> str | None:
    """'Robinhood NVDA / USD' → NVDA · 'Robinhood DELL-USD' → DELL · 'CBBTC / USD' → cbBTC · others → None."""
    n = name.replace("Robinhood ", "").replace("-USD", " / USD").strip()
    if not n.endswith("/ USD"):
        return None
    base = n[: -len("/ USD")].strip()
    if not base or " " in base:
        return None
    return "cbBTC" if base.upper() == "CBBTC" else base


async def refresh_feed_directory(client: httpx.AsyncClient) -> int:
    """Pull Chainlink's Robinhood Chain feed list (once a day); the seed map stays as the floor."""
    global _feed_dir_ts
    if time.time() - _feed_dir_ts < FEED_DIR_TTL_S:
        return 0
    _feed_dir_ts = time.time()
    try:
        r = await client.get(FEED_DIR_URL, headers=_UA)
        n = 0
        for f in r.json():
            sym = _parse_feed_name(str(f.get("name") or ""))
            proxy = f.get("proxyAddress")
            if sym and proxy and FEEDS.get(sym) != proxy:
                FEEDS[sym] = proxy
                _feed_decimals.pop(sym, None)
                n += 1
        return n
    except Exception as e:
        logger.debug(f"chainlink feed directory refresh failed: {e}")
        return 0


async def _chainlink(sym: str) -> float:
    import rh_wallet
    proxy = FEEDS[sym]
    if sym not in _feed_decimals:
        raw = await rh_wallet.rpc("eth_call", [{"to": proxy, "data": SEL_DECIMALS}, "latest"])
        _feed_decimals[sym] = int(raw, 16) if raw and raw != "0x" else 8
    raw = await rh_wallet.rpc("eth_call", [{"to": proxy, "data": SEL_LATEST_ROUND}, "latest"])
    if not raw or len(raw) < 2 + 64 * 5:
        return 0.0
    words = [int(raw[2 + i * 64: 2 + (i + 1) * 64], 16) for i in range(5)]
    answer = words[1] - (1 << 256) if words[1] >= (1 << 255) else words[1]
    updated_at = words[3]
    if answer <= 0 or updated_at <= 0 or time.time() - updated_at > FEED_STALE_S:
        return 0.0
    return answer / 10 ** _feed_decimals[sym]


async def _yahoo(client: httpx.AsyncClient, sym: str) -> float:
    r = await client.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                         params={"range": "1d", "interval": "1d", "includePrePost": "true"}, headers=_UA)
    m = r.json()["chart"]["result"][0]["meta"]
    return float(m.get("fulldayPrice") or m.get("postMarketPrice") or m.get("regularMarketPrice") or 0)


async def _coinbase_btc(client: httpx.AsyncClient, _sym: str) -> float:
    r = await client.get("https://api.coinbase.com/v2/exchange-rates", params={"currency": "BTC"})
    return float(r.json()["data"]["rates"]["USD"])


def _fetcher(sym: str):
    return _coinbase_btc if sym in ("cbBTC", "BTC") else _yahoo


async def _fetch_one(client: httpx.AsyncClient, sym: str, now: float):
    price, source = 0.0, "miss"
    if sym in FEEDS:
        try:
            price = await _chainlink(sym)
            source = "chainlink"
        except Exception as e:
            logger.debug(f"chainlink feed {sym} failed: {e}")
    if price <= 0:
        try:
            price = await _fetcher(sym)(client, sym)
            source = "coinbase" if sym in ("cbBTC", "BTC") else "yahoo"
        except Exception as e:
            price = 0.0
            logger.debug(f"quote price {sym} failed: {e}")
    if price > 0:
        _cache[sym] = {"price": price, "ts": now, "source": source}
    else:
        prev = _cache.get(sym)
        # keep the last good print (marked stale) but don't hammer the sources for 5 min
        _cache[sym] = {"price": float(prev["price"]) if prev else 0.0, "ts": now - TTL_S + MISS_TTL_S,
                       "source": (prev or {}).get("source", "miss")}


async def refresh(symbols) -> int:
    """Refresh every symbol whose cached print is older than TTL. Returns how many were fetched."""
    now = time.time()
    stale = [s for s in set(symbols) if s and s not in STABLES and s != "ETH" and s != "?"
             and now - float(_cache.get(s, {}).get("ts") or 0) >= TTL_S]
    async with httpx.AsyncClient(timeout=6.0) as client:
        await refresh_feed_directory(client)
        if not stale:
            return 0
        await asyncio.gather(*(_fetch_one(client, s, now) for s in stale))
    return len(stale)
