"""Seasoned card freshness: a token we HOLD keeps refreshing (MC / last trade / velocity) using the monitor's
per-tick pool read instead of being dropped from the refresh loop."""
import asyncio
import os
import sys
import time
from collections import deque
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discovery as disc  # noqa: E402
import pumpswap  # noqa: E402


@pytest.mark.asyncio
async def test_held_pumpswap_token_refreshes_from_slot_cache(monkeypatch):
    mint, pool = "HeldMint111111111111111111111111111111111111", "HeldPool111111111111111111111111111111111111"
    bucket = {"discovered": True, "protocol": "pumpswap", "pumpswap_pool": pool, "usd_market_cap": 50_000.0,
              "mc_samples": deque(maxlen=12), "price_samples": deque(maxlen=12), "last_trade_ms": 0,
              "symbol": "HELD", "first_price": 1e-9, "start": time.time() - 600}
    slot = {"_pool_cache": {"pool": pool, "base_reserves": 100_000_000_000_000, "quote_reserves": 200_000_000_000, "base_decimals": 6}}
    st = SimpleNamespace(tracking={mint: bucket}, active_trades={mint: slot}, config=SimpleNamespace(scanner_min_mc_usd_seasoned=17_000.0),
                         db=SimpleNamespace(launches=SimpleNamespace(update_one=None)))
    rpc = {"batch": 0, "single": 0}

    async def fake_batch(pools):
        rpc["batch"] += 1
        return {}

    async def fake_single(p):
        rpc["single"] += 1
        return None

    monkeypatch.setattr(pumpswap, "fetch_pool_states_batch", fake_batch)
    monkeypatch.setattr(pumpswap, "fetch_pool_state", fake_single)

    async def fake_sol_price():
        return 200.0

    import solana_client
    monkeypatch.setattr(solana_client, "get_sol_usd_price", fake_sol_price)

    class _Resp:
        status_code = 200
        content = b""

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(disc.httpx, "AsyncClient", _Client)
    d = disc.PumpfunDiscovery(st)
    before = time.time()
    await d._refresh_once()
    # MC recomputed from the slot pool cache with the existing quote*1e6/base formula: 2000 SOL * $200
    assert abs(bucket["usd_market_cap"] - 400_000.0) < 1.0
    assert bucket["last_trade_ms"] >= int(before * 1000)
    assert len(bucket["mc_samples"]) == 1
    assert rpc == {"batch": 0, "single": 0}      # zero extra RPC for the held token
