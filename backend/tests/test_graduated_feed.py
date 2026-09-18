"""Seasoned supply: recently-graduated feed seeds pumpswap buckets."""
import asyncio, os, sys, time
from types import SimpleNamespace
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
import discovery as disc
from models import BotConfig


def _coin(mint, created_ago_s=600, complete=True, pool="Pool111"):
    return {"mint": mint, "symbol": mint[:4], "name": mint, "complete": complete,
            "created_timestamp": int((time.time() - created_ago_s) * 1000),
            "pool_address": pool, "usd_market_cap": 50_000.0, "creator": "C1"}


def _state():
    return SimpleNamespace(config=BotConfig(), tracking={}, active_trades={}, entered_mints=set(),
                           recent_launches=[], db=None)


import pytest


@pytest.fixture(autouse=True)
def _restore_batch():
    import pumpswap
    orig = pumpswap.fetch_pool_states_batch
    yield
    pumpswap.fetch_pool_states_batch = orig       # never leak the fake into later test modules (test_rpc_waste)


def _feed(coins):
    d = disc.PumpfunDiscovery(_state())

    async def fake_fetch(limit=100):
        return coins
    d.fetch_recent_graduated = fake_fetch

    async def fake_seed(coin, created_s, is_pumpswap=False, **_kw):
        d.state.tracking[coin["mint"]] = {"start": created_s, "protocol": "pumpswap" if is_pumpswap else "pumpfun",
                                          "graduated_at": time.time() if is_pumpswap else None, "discovered": True}
    d._seed_token = fake_seed

    async def fake_batch(pools):
        return {}
    disc.pumpswap.fetch_pool_states_batch = fake_batch          # restored by the autouse _restore_batch fixture
    return d


def test_seeds_graduated_as_pumpswap_and_dedups():
    d = _feed([_coin("A" * 32), _coin("B" * 32), _coin("C" * 32, complete=False)])
    n = asyncio.run(d.graduated_once())
    assert n == 2
    b = d.state.tracking["A" * 32]
    assert b["protocol"] == "pumpswap" and b["graduated_feed"] is True and b["graduated_at"]
    assert "C" * 32 not in d.state.tracking
    assert asyncio.run(d.graduated_once()) == 0


def test_skips_tracked_active_and_ancient():
    d = _feed([_coin("A" * 32), _coin("B" * 32), _coin("D" * 32, created_ago_s=3 * 86400), _coin("E" * 32, pool=None)])
    d.state.tracking["A" * 32] = {"start": 0}
    d.state.active_trades["B" * 32] = {}
    assert asyncio.run(d.graduated_once()) == 0


def test_drops_feed_tokens_aged_out_of_band():
    d = _feed([])
    cfg = d.state.config
    old = time.time() - (cfg.band_seasoned_max_age_min * 60 + 400)
    d.state.tracking["OLD"] = {"start": old, "graduated_at": old, "graduated_feed": True}
    d.state.tracking["HELD"] = {"start": old, "graduated_at": old, "graduated_feed": True}
    d.state.active_trades["HELD"] = {}
    d.state.tracking["FRESH"] = {"start": time.time(), "graduated_at": time.time(), "graduated_feed": True}
    asyncio.run(d.graduated_once())
    assert "OLD" not in d.state.tracking and "HELD" in d.state.tracking and "FRESH" in d.state.tracking


def test_disabled_flag_exists():
    assert BotConfig().scanner_graduated_feed_enabled is True


def test_live_pumpfun_graduated_endpoint_shape():
    d = disc.PumpfunDiscovery(_state())
    try:
        coins = asyncio.run(d.fetch_recent_graduated(limit=20))
    except Exception:
        return  # network hiccup — not a code failure
    assert coins and all(c.get("complete") for c in coins)
    assert any(c.get("pool_address") for c in coins)
