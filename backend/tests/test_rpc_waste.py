"""RPC-waste reduction: push-decoded curve state, safety-net poll, canonical pool PDA, batched pool reads,
mint token-program cache, provider-agnostic env, follower stops the auto-tuner."""
import asyncio
import base64
import os
import struct
import sys
import time

import pytest
from solders.pubkey import Pubkey

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot as bot_mod  # noqa: E402
import pumpfun  # noqa: E402
import pumpswap  # noqa: E402
from account_event_bus import account_event_bus as bus  # noqa: E402
from bot import BotState, MONITOR_SAFETY_POLL_S  # noqa: E402


def _curve_bytes(vsr: int, vtr: int, complete: bool = False) -> bytes:
    data = bytearray(90)
    struct.pack_into("<QQQQQ", data, 8, vtr, vsr, 1, 2, 3)
    data[48] = 1 if complete else 0
    return bytes(data)


def test_decode_bonding_curve_roundtrip():
    st = pumpfun.decode_bonding_curve(_curve_bytes(30_000_000_000, 1_073_000_000_000_000))
    assert st["virtual_sol_reserves"] == 30_000_000_000
    assert st["virtual_token_reserves"] == 1_073_000_000_000_000
    assert st["complete"] is False
    assert pumpfun.decode_bonding_curve(b"\x00" * 10) is None


def test_canonical_pool_matches_live_pools():
    cases = [
        ("BjA7m25vvfVJQnFtNF4QjHC9x6inNtCnTsmXWLgupump", "Bbafnfoxo3aiwe2WAxaTbM3gMUHCzxGqs4Et8jB58QDn"),
        ("9FXna2GAnFz7KKLAHDRLxnBrjVMyoPSX1zE2TBHpump", "Cg3wPJwHtXLFatH3mEjBYXus8czr7e1VENkw8Uz5XuTn"),
    ]
    for mint, pool in cases:
        assert str(pumpswap.derive_canonical_pool(Pubkey.from_string(mint))) == pool


def test_env_is_provider_agnostic():
    import solana_client
    assert solana_client.RPC_URL == (os.environ.get("SOLANA_RPC_URL") or os.environ["HELIUS_RPC_URL"])
    assert solana_client.rpc_provider() in ("quicknode", "helius", "triton", "alchemy", "ankr", "shyft") or "." in solana_client.rpc_provider()


@pytest.mark.asyncio
async def test_bus_take_latest_and_is_live():
    acct = "TestAcct1111111111111111111111111111111111111"
    bus.subscribe(acct)
    bus._wss_sub_ids[acct] = 77
    raw = _curve_bytes(31_000_000_000, 1_000_000_000_000_000)
    await bus._handle_message(
        '{"method":"accountNotification","params":{"subscription":77,"result":{"context":{"slot":1},'
        '"value":{"data":["' + base64.b64encode(raw).decode() + '","base64"],"lamports":1}}}}')
    assert bus.take_latest(acct) == raw
    assert bus.take_latest(acct) is None          # consumed once
    bus._connected.set()
    assert bus.is_live(acct)
    bus.unsubscribe(acct)
    assert not bus.is_live(acct)
    bus._connected.clear()


@pytest.mark.asyncio
async def test_monitor_curve_state_prefers_push_then_cache_then_rpc(monkeypatch):
    calls = {"rpc": 0}

    async def fake_fetch(mint):
        calls["rpc"] += 1
        return {"virtual_sol_reserves": 1, "virtual_token_reserves": 1, "complete": False}

    monkeypatch.setattr(pumpfun, "fetch_bonding_curve_state", fake_fetch)
    state = BotState(db=None)
    acct = "WatchAcct11111111111111111111111111111111111"
    bus.subscribe(acct)
    bus._wss_sub_ids[acct] = 5
    bus._connected.set()
    slot: dict = {}
    # 1) push → decoded locally, zero RPC
    bus._latest[acct] = (_curve_bytes(40_000_000_000, 900_000_000_000_000), time.time())
    st = await state._monitor_curve_state("m", slot, acct)
    assert st["virtual_sol_reserves"] == 40_000_000_000 and calls["rpc"] == 0
    # 2) no push, subscription live, cache fresh → reuse, zero RPC
    st2 = await state._monitor_curve_state("m", slot, acct)
    assert st2 is st and calls["rpc"] == 0
    # 3) cache older than the safety-net interval → one RPC
    slot["_curve_read_ts"] = time.time() - MONITOR_SAFETY_POLL_S - 0.1
    await state._monitor_curve_state("m", slot, acct)
    assert calls["rpc"] == 1
    # 4) subscription not live → every tick polls (old behaviour)
    bus._connected.clear()
    await state._monitor_curve_state("m", slot, acct)
    assert calls["rpc"] == 2
    # 5) closed-account push (b"") → None state (graduation path), still no RPC
    bus._latest[acct] = (b"", time.time())
    assert await state._monitor_curve_state("m", slot, acct) is None and calls["rpc"] == 2
    bus.unsubscribe(acct)


@pytest.mark.asyncio
async def test_monitor_pool_state_watches_vault_and_reuses_cache(monkeypatch):
    calls = {"rpc": 0}
    pool = "Pool1111111111111111111111111111111111111111"
    vault = "Vault111111111111111111111111111111111111111"
    pumpswap._POOL_STATIC[pool] = {"pool": pool, "pool_quote_token_account": vault, "pool_base_token_account": "B"}

    async def fake_fetch(p):
        calls["rpc"] += 1
        return {"pool": p, "base_reserves": 10, "quote_reserves": 20}

    monkeypatch.setattr(pumpswap, "fetch_pool_state", fake_fetch)
    state = BotState(db=None)
    slot = {"watch_account": pool}
    bus._connected.set()
    st = await state._monitor_pool_state(slot, pool)
    assert st["quote_reserves"] == 20 and calls["rpc"] == 1
    assert slot["watch_account"] == vault and vault in bus._events      # moved from pool → WSOL vault
    bus._wss_sub_ids[vault] = 9
    assert (await state._monitor_pool_state(slot, pool)) is st and calls["rpc"] == 1   # live + fresh → reuse
    bus._latest[vault] = (b"\x00" * 165, time.time())                                # a swap landed → one re-read
    await state._monitor_pool_state(slot, pool)
    assert calls["rpc"] == 2
    bus.unsubscribe(vault)
    bus._connected.clear()
    pumpswap._POOL_STATIC.pop(pool, None)


@pytest.mark.asyncio
async def test_fetch_pool_states_batch_uses_two_calls_for_many_pools(monkeypatch):
    calls: list[str] = []
    n = 40
    pools = [str(Pubkey.new_unique()) for _ in range(n)]
    statics = {}
    for p in pools:
        raw = bytearray(250)
        raw[43:75] = bytes(Pubkey.new_unique()); raw[75:107] = bytes(pumpswap.WSOL)
        raw[139:171] = bytes(Pubkey.new_unique()); raw[171:203] = bytes(Pubkey.new_unique()); raw[211:243] = bytes(Pubkey.new_unique())
        statics[p] = bytes(raw)

    async def fake_rpc(method, params, **kw):
        calls.append(method)
        keys = params[0]
        if params[1]["encoding"] == "base64":
            return {"result": {"value": [{"data": [base64.b64encode(statics[k]).decode(), "base64"]} for k in keys]}}
        return {"result": {"value": [{"data": {"parsed": {"info": {"tokenAmount": {"amount": "5", "decimals": 6}}}}} for _ in keys]}}

    monkeypatch.setattr(pumpswap, "rpc_call", fake_rpc)
    out = await pumpswap.fetch_pool_states_batch(pools)
    assert len(out) == n and all(v["quote_reserves"] == 5 for v in out.values())
    assert calls == ["getMultipleAccounts", "getMultipleAccounts"]       # statics + vaults
    calls.clear()
    await pumpswap.fetch_pool_states_batch(pools)
    assert calls == ["getMultipleAccounts"]                              # statics cached → vaults only
    for p in pools:
        pumpswap._POOL_STATIC.pop(p, None)


@pytest.mark.asyncio
async def test_find_pool_canonical_only_and_negative_cache(monkeypatch):
    calls = {"state": 0, "gpa": 0}

    async def fake_state(p):
        calls["state"] += 1
        return None

    async def fake_gpa(m):
        calls["gpa"] += 1
        return None

    monkeypatch.setattr(pumpswap, "fetch_pool_state", fake_state)
    monkeypatch.setattr(pumpswap, "_find_pool_gpa", fake_gpa)
    mint = str(Pubkey.new_unique())
    assert await pumpswap.find_pool_for_mint(mint, canonical_only=True) is None
    assert calls == {"state": 1, "gpa": 0}
    assert await pumpswap.find_pool_for_mint(mint) is None               # inside the miss TTL → no RPC at all
    assert calls == {"state": 1, "gpa": 0}
    pumpswap._POOL_MISS.pop(mint, None)
    assert await pumpswap.find_pool_for_mint(mint) is None               # full path: canonical miss → gPA fallback
    assert calls == {"state": 2, "gpa": 1}
    pumpswap._POOL_MISS.pop(mint, None)


@pytest.mark.asyncio
async def test_mint_token_program_cached(monkeypatch):
    calls = {"n": 0}

    async def fake_read(m):
        calls["n"] += 1
        return pumpfun.TOKEN_2022_PROGRAM

    monkeypatch.setattr(pumpfun, "_read_mint_token_program", fake_read)
    mint = str(Pubkey.new_unique())
    assert await pumpfun.get_mint_token_program(mint) == pumpfun.TOKEN_2022_PROGRAM
    assert await pumpfun.get_mint_token_program(mint) == pumpfun.TOKEN_2022_PROGRAM
    assert calls["n"] == 1
    pumpfun._MINT_TP_CACHE.pop(mint, None)


@pytest.mark.asyncio
async def test_stop_loops_stops_auto_tuner():
    from speed_modes import auto_tuner
    state = BotState(db=None)
    auto_tuner._task = asyncio.create_task(asyncio.sleep(30))
    await state.stop_loops("test")
    await asyncio.sleep(0)
    assert auto_tuner._task.cancelled() or auto_tuner._task.done()


@pytest.mark.asyncio
async def test_get_multiple_shrinks_batch_on_plan_cap(monkeypatch):
    seen: list[int] = []

    async def fake_rpc(method, params, **kw):
        keys = params[0]
        seen.append(len(keys))
        if len(keys) > 5:
            return {"jsonrpc": "2.0", "id": 1, "error": {"code": -32615, "message": "getMultipleAccounts is limited to a 5 range, upgrade"}}
        return {"result": {"value": [{"k": k} for k in keys]}}

    monkeypatch.setattr(pumpswap, "rpc_call", fake_rpc)
    old = pumpswap._BATCH
    pumpswap._BATCH = 100
    try:
        out = await pumpswap._get_multiple([str(i) for i in range(12)], "base64")
        assert len(out) == 12 and out[11] == {"k": "11"}
        assert seen == [12, 5, 5, 2] and pumpswap._BATCH == 5
    finally:
        pumpswap._BATCH = old


@pytest.mark.asyncio
async def test_rpc_pacer_spaces_calls(monkeypatch):
    import solana_client as sc
    monkeypatch.setattr(sc, "RPC_MAX_RPS", 50.0)
    monkeypatch.setattr(sc, "_pace_next", 0.0)
    t0 = time.monotonic()
    for _ in range(6):
        await sc._pace()
    assert time.monotonic() - t0 >= 5 * (1 / 50.0) * 0.8       # 5 gaps of 20 ms


@pytest.mark.asyncio
async def test_rpc_quota_429_switches_to_fallback(monkeypatch):
    import solana_client as sc

    class _Resp:
        def __init__(self, url):
            self.status_code = 429 if url == "https://primary" else 200
            self.text = "daily request limit reached" if self.status_code == 429 else ""
            self.headers = {}
            self.request = None

        def raise_for_status(self):
            pass

        def json(self):
            return {"result": "fallback-ok"}

    hits: list[str] = []

    class _Client:
        is_closed = False

        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, timeout=None):
            hits.append(url)
            return _Resp(url)

    monkeypatch.setattr(sc.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(sc, "_http", None)
    monkeypatch.setattr(sc, "_cool_until", {})
    monkeypatch.setattr(sc, "RPC_URL", "https://primary")
    monkeypatch.setattr(sc, "RPC_FALLBACK_URL", "https://fallback")
    monkeypatch.setattr(sc, "RPC_MAX_RPS", 0.0)
    monkeypatch.setattr(sc, "_primary_dead_until", 0.0)
    assert (await sc.rpc_call("getSlot", []))["result"] == "fallback-ok"
    assert hits == ["https://primary", "https://fallback"]        # quota 429 → straight to fallback, no retry storm
    hits.clear()
    assert (await sc.rpc_call("getSlot", []))["result"] == "fallback-ok"
    assert hits == ["https://fallback"]                            # primary skipped while marked dead


def test_budget_counts_by_method_and_weights_gpa():
    import helius_budget as hb
    before = hb._counts["estimated_credits"]
    hb.record_rpc_call("getProgramAccounts")
    hb.record_rpc_call("getAccountInfo")
    assert hb._counts["estimated_credits"] - before == 11
    assert hb.by_method()["getProgramAccounts"] >= 1
    assert "by_method" in hb.snapshot() and "rpc_provider" in hb.snapshot()
