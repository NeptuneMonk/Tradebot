"""SOL window discovery: alive = DexScreener buy inflow over the scanner inflow window ≥ scanner_min_recent_inflow_sol;
quiet discovered curve tokens are evicted so the tracker only holds live tokens."""
import asyncio
import time

import discovery as disc_mod
from discovery import PumpfunDiscovery
from models import BotConfig


class _State:
    def __init__(self):
        self.config = BotConfig(scanner_min_recent_inflow_sol=3.0, scanner_recent_inflow_window_s=300,
                                scanner_window_hours=4, scanner_min_age_minutes=180, scanner_discovery_max_idle_minutes=5)
        self.tracking = {}
        self.active_trades = {}
        self.entered_mints = set()
        self.db = None


def _coin(mint, age_h=3.5, idle_s=60, complete=False):
    now_ms = time.time() * 1000
    return {"mint": mint, "created_timestamp": now_ms - age_h * 3600 * 1000, "last_trade_timestamp": now_ms - idle_s * 1000,
            "complete": complete, "symbol": mint[:4], "name": mint, "usd_market_cap": 20000, "virtual_sol_reserves": 40 * 10**9,
            "virtual_token_reserves": 800_000_000 * 10**6, "bonding_curve": "C" * 44, "creator": "D" * 44}


def _pair(mint, vol_m5, buys, sells):
    return {"chainId": "solana", "dexId": "pumpfun", "baseToken": {"address": mint},
            "txns": {"m5": {"buys": buys, "sells": sells}, "h1": {"buys": 0, "sells": 0}}, "volume": {"m5": vol_m5, "h1": 0}}


def _wire(monkeypatch, d, pairs, coins):
    calls = []

    class _R:
        def __init__(self, body): self._b = body
        def raise_for_status(self): pass
        def json(self): return self._b

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None, headers=None):
            calls.append(url)
            if "dexscreener" in url:
                want = url.rsplit("/", 1)[-1].split(",")
                return _R([p for p in pairs if p["baseToken"]["address"] in want])
            return _R([])
    monkeypatch.setattr(disc_mod.httpx, "AsyncClient", _Client)

    async def sol_price():
        return 100.0
    import solana_client
    monkeypatch.setattr(solana_client, "get_sol_usd_price", sol_price)

    async def fetch(lo, hi):
        return coins
    monkeypatch.setattr(d, "_fetch_aged_coins", fetch)
    seeded = []

    async def seed(coin, created_s, is_pumpswap=False, pool_state=None):
        seeded.append(coin["mint"])
        d.state.tracking[coin["mint"]] = {"discovered": True, "start": created_s, "last_trade_ms": coin["last_trade_timestamp"], "protocol": "pumpfun"}
    monkeypatch.setattr(d, "_seed_token", seed)

    async def nobc(*a, **k):
        return None
    monkeypatch.setattr(disc_mod.hub, "broadcast", nobc)
    return calls, seeded


def test_alive_by_inflow_seeds_only_tokens_clearing_the_floor(monkeypatch):
    st = _State()
    d = PumpfunDiscovery(st)
    hot, warm, dead, unknown = "H" * 44, "W" * 44, "Z" * 44, "U" * 44
    coins = [_coin(hot), _coin(warm), _coin(dead), _coin(unknown), _coin("I" * 44, idle_s=900)]      # last one: idle > 5 min
    # hot: $600 m5 vol, 75% buys → $450 = 4.5 SOL ≥ 3 ✓ · warm: $400 × 50% = 2 SOL ✗ · dead: no txns ✗ · unknown: no pair ✗
    pairs = [_pair(hot, 600, 3, 1), _pair(warm, 400, 2, 2), _pair(dead, 0, 0, 0)]
    calls, seeded = _wire(monkeypatch, d, pairs, coins)
    n = asyncio.run(d.run_once())
    assert n == 1 and seeded == [hot]
    assert st.tracking[hot]["alive_inflow_sol"] == 4.5
    s = d.last_stats
    assert s["candidates"] == 4 and s["alive"] == 1 and s["below_floor"] == 2 and s["no_pair"] == 1 and s["skipped_idle"] == 1
    assert sum("dexscreener" in u for u in calls) == 1                     # 4 mints → one batched call


def test_dexscreener_outage_fails_open(monkeypatch):
    st = _State()
    d = PumpfunDiscovery(st)
    coins = [_coin("A" * 44), _coin("B" * 44)]
    calls, seeded = _wire(monkeypatch, d, [], coins)

    class _Boom:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None, headers=None):
            raise RuntimeError("dex down")
    monkeypatch.setattr(disc_mod.httpx, "AsyncClient", _Boom)
    assert asyncio.run(d.run_once()) == 2 and d.last_stats["dex_errors"] == 1


def test_floor_zero_keeps_legacy_freshness_only(monkeypatch):
    st = _State()
    st.config.scanner_min_recent_inflow_sol = 0.0
    d = PumpfunDiscovery(st)
    coins = [_coin("A" * 44), _coin("B" * 44)]
    calls, seeded = _wire(monkeypatch, d, [], coins)
    assert asyncio.run(d.run_once()) == 2 and not any("dexscreener" in u for u in calls)


def test_quiet_discovered_curve_tokens_are_evicted():
    st = _State()
    d = PumpfunDiscovery(st)
    now = time.time()
    st.tracking["quiet"] = {"discovered": True, "start": now - 4 * 3600, "last_trade_ms": (now - 2 * 3600) * 1000, "protocol": "pumpfun"}
    st.tracking["alive"] = {"discovered": True, "start": now - 4 * 3600, "last_trade_ms": (now - 120) * 1000, "protocol": "pumpfun"}
    st.tracking["pool"] = {"discovered": True, "start": now - 4 * 3600, "last_trade_ms": (now - 2 * 3600) * 1000, "protocol": "pumpswap"}
    st.tracking["held"] = {"discovered": True, "start": now - 4 * 3600, "last_trade_ms": (now - 2 * 3600) * 1000, "protocol": "pumpfun"}
    st.active_trades["held"] = {}
    st.tracking["organic"] = {"start": now - 4 * 3600, "last_trade_ms": 0, "protocol": "pumpfun"}      # mempool-tracked: not ours to evict
    assert d._evict_quiet(now) == 1
    assert set(st.tracking) == {"alive", "pool", "held", "organic"} and d.evicted_quiet == 1
