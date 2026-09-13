"""Creator-solvency + first-window dump gate — hunt/seasoned/rh_pons only, cached, read-only, no firehose cost."""
import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import creator_solvency as cs
from models import BotConfig
import rh_paper as rp
from tests.test_rh_paper import make_state, hot_bucket, TOKEN


def setup_function():
    cs._cache.clear()
    for k in cs.stats:
        cs.stats[k] = 0


def _fetch(value=None, exc=None):
    calls = []

    async def f(addr):
        calls.append(addr)
        if exc:
            raise exc
        return value
    f.calls = calls
    return f


def test_scope_hunt_seasoned_rh_not_new_band_not_manual():
    cfg = BotConfig()
    assert cs.in_scope(cfg, "greylist_snipe", "pumpfun") and cs.in_scope(cfg, "scanner_momentum", "pumpswap")
    assert cs.in_scope(cfg, "momentum_new", "pumpfun") is False        # scalp tape untouched
    assert cs.in_scope(cfg, "manual", "pumpfun") is False              # operator bypass
    assert cs.in_scope(BotConfig(creator_sol_gate_new_band=True), "momentum_new", "pumpfun") is True
    assert cs.in_scope(BotConfig(creator_solvency_enabled=False), "greylist_snipe", "pumpfun") is False


def test_hunt_low_sol_skips_and_healthy_creator_passes():
    cfg = BotConfig()
    assert cs.gate(cfg, "sol", 0.1, {}) == "pf-creator-sol"
    assert cs.gate(cfg, "sol", 2.0, {}) is None
    assert cs.gate(cfg, "rh", 0.01, {}) == "rh-creator-eth"
    assert cs.gate(cfg, "rh", 0.2, {}) is None


def test_rpc_error_fail_closed_and_not_retried_every_tick():
    cfg = BotConfig()
    f = _fetch(exc=RuntimeError("rpc down"))
    assert asyncio.run(cs.balance("CreatorA", f)) is None
    assert asyncio.run(cs.balance("CreatorA", f)) is None
    assert len(f.calls) == 1 and cs.stats["errors"] == 1 and cs.stats["cache_hits"] == 1
    assert cs.gate(cfg, "sol", None, {}) == "creator-balance-unknown"
    assert cs.gate(BotConfig(creator_balance_fail="open"), "sol", None, {}) is None


def test_cache_two_entries_same_creator_one_call():
    f = _fetch(value=1.5)
    assert asyncio.run(cs.balance("CreatorB", f)) == 1.5
    assert asyncio.run(cs.balance("CreatorB", f)) == 1.5
    assert len(f.calls) == 1
    cs._cache["CreatorB"] = (1.5, time.time() - cs.CACHE_TTL_S - 1)
    asyncio.run(cs.balance("CreatorB", f))
    assert len(f.calls) == 2                                            # refetched only after the 5-min TTL


def test_dump_window_50pct_blocks_10pct_passes_and_window_closes():
    cfg = BotConfig()
    now = time.time()
    b = {"start": now - 5, "creator_start_tokens": 1000.0}
    cs.record_creator_sell(b, 0.4, 500.0, now)
    assert cs.gate(cfg, "sol", 2.0, b) == "creator-dumped" and b["creator_sold_pct"] == 50.0 and b["creator_sold_pct_proxy"] is False
    b2 = {"start": now - 5, "creator_start_tokens": 1000.0}
    cs.record_creator_sell(b2, 0.1, 100.0, now)
    assert cs.gate(cfg, "sol", 2.0, b2) is None and b2["creator_sold_pct"] == 10.0
    b3 = {"start": now - 120, "creator_start_tokens": 1000.0}
    cs.record_creator_sell(b3, 1.0, 900.0, now)                         # after the 60 s window: ignored
    assert cs.gate(cfg, "sol", 2.0, b3) is None
    b4 = {"start": now - 5}                                             # unknown denominator → proxy on quote
    cs.record_creator_sell(b4, 3.0, 0.0, now)
    pct, proxy = cs.creator_sold_pct(b4, 2.0)
    assert proxy is True and abs(pct - 60.0) < 1e-9 and cs.gate(cfg, "sol", 2.0, b4) == "creator-dumped"


def test_rh_entry_skips_broke_deployer_and_manual_bypasses():
    st = make_state(creator_solvency_enabled=True)
    now = time.time()
    b = hot_bucket(st.rh_discovery, now)
    b["creator"] = "0x" + "d" * 40
    with patch.object(rp.rh_wallet, "balance_wei", new=AsyncMock(return_value=int(0.01e18))) as bw:
        asyncio.run(st.rh_paper._enter(TOKEN))
    assert TOKEN not in st.rh_paper.pending_buys and b["gate_reason"] == "rh-creator-eth" and b["creator_eth"] == 0.01
    assert st.rh_paper.stats["skip_reasons"]["curve:rh-creator-eth"] == 1 and bw.await_count == 1
    st.rh_paper._pending_entries.clear()
    with patch.object(rp.rh_wallet, "balance_wei", new=AsyncMock(return_value=int(0.01e18))) as bw:
        asyncio.run(st.rh_paper._enter(TOKEN, manual=True))
    assert TOKEN in st.rh_paper.pending_buys and bw.await_count == 0    # manual bypass: no balance call at all
    st.rh_paper.pending_buys.clear()
    st.rh_paper._pending_entries.clear()
    cs._cache.clear()
    b["creator"] = "0x" + "e" * 40
    with patch.object(rp.rh_wallet, "balance_wei", new=AsyncMock(return_value=int(0.3e18))):
        asyncio.run(st.rh_paper._enter(TOKEN))
    pb = st.rh_paper.pending_buys[TOKEN]
    assert pb["ctx"]["plan"]["creator_eth"] == 0.3                      # stamped on the trade plan


def test_rh_poller_does_not_fetch_balances_inline():
    src = Path(__file__).resolve().parents[1].joinpath("rh_discovery.py").read_text()
    assert "creator_solvency.balance(" not in src                       # only the entry path pays for the lookup
    assert "creator_solvency.record_creator_sell(" in src               # dump window rides the existing trade ingest
