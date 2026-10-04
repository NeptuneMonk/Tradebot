"""Solana RPC 429 breaker: a per-second 429 parks the endpoint briefly and the call moves to the other endpoint —
no inline sleeps, no retry storm; when every endpoint is cooling the call fails fast. Pooled client is reused."""
import asyncio
import time

import pytest

import solana_client as sc


class _Resp:
    def __init__(self, code, text="", result="ok"):
        self.status_code, self.text, self._result = code, text, result
        self.headers, self.request = {}, None

    def raise_for_status(self):
        pass

    def json(self):
        return {"result": self._result}


def _wire(monkeypatch, plan):
    """plan: url -> list of responses popped per call (last one repeats)."""
    hits: list[str] = []
    made = []

    class _Client:
        is_closed = False

        def __init__(self, *a, **kw):
            made.append(1)

        async def post(self, url, json=None, timeout=None):
            hits.append(url)
            q = plan[url]
            return q.pop(0) if len(q) > 1 else q[0]

    monkeypatch.setattr(sc.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(sc, "_http", None)
    monkeypatch.setattr(sc, "_inflight", None)
    monkeypatch.setattr(sc, "_cool_until", {})
    monkeypatch.setattr(sc, "_primary_dead_until", 0.0)
    monkeypatch.setattr(sc, "RPC_URL", "https://primary")
    monkeypatch.setattr(sc, "RPC_FALLBACK_URL", "https://fallback")
    monkeypatch.setattr(sc, "RPC_MAX_RPS", 0.0)
    return hits, made


@pytest.mark.asyncio
async def test_burst_429_moves_to_fallback_without_sleeping(monkeypatch):
    hits, made = _wire(monkeypatch, {"https://primary": [_Resp(429, "50/second request limit reached")],
                                     "https://fallback": [_Resp(200, result="fb")]})
    t0 = time.time()
    assert (await sc.rpc_call("getSlot", []))["result"] == "fb"
    assert time.time() - t0 < 0.5                          # no inline 429 back-off
    assert hits == ["https://primary", "https://fallback"]
    assert sc._primary_dead_until == 0.0                   # per-second burst is NOT a plan-quota death
    assert sc._cool_until["https://primary"] > time.time()  # primary parked for RATE_COOL_S
    hits.clear()
    assert (await sc.rpc_call("getSlot", []))["result"] == "fb"
    assert hits == ["https://fallback"]                    # while cooling, calls skip the primary outright
    assert len(made) == 1                                  # one pooled client, not one per call


@pytest.mark.asyncio
async def test_all_endpoints_cooling_fails_fast(monkeypatch):
    hits, _ = _wire(monkeypatch, {"https://primary": [_Resp(429, "rate limit exceeded")],
                                  "https://fallback": [_Resp(429, "Too Many Requests")]})
    t0 = time.time()
    with pytest.raises(Exception) as ei:
        await sc.rpc_call("getSlot", [], max_retries=3)
    assert "429" in str(ei.value)
    assert time.time() - t0 < 1.0                          # bounded: both parked → fail fast, caller's cadence retries
    assert hits == ["https://primary", "https://fallback"]
    before = sc.stats["cooled"]
    with pytest.raises(Exception):
        await sc.rpc_call("getSlot", [])                   # nothing free → no HTTP at all
    assert hits == ["https://primary", "https://fallback"] and sc.stats["cooled"] > before


@pytest.mark.asyncio
async def test_quota_429_marks_primary_dead(monkeypatch):
    hits, _ = _wire(monkeypatch, {"https://primary": [_Resp(429, "Monthly credits exhausted — upgrade your plan")],
                                  "https://fallback": [_Resp(200, result="fb")]})
    assert (await sc.rpc_call("getSlot", []))["result"] == "fb"
    assert sc._primary_dead_until > time.time() + 100
    snap = sc.rpc_snapshot()
    assert snap["primary_quota_dead_s"] > 100 and snap["429_primary"] >= 1


@pytest.mark.asyncio
async def test_inflight_cap(monkeypatch):
    peak = {"n": 0, "cur": 0}

    class _Client:
        is_closed = False

        def __init__(self, *a, **kw):
            pass

        async def post(self, url, json=None, timeout=None):
            peak["cur"] += 1
            peak["n"] = max(peak["n"], peak["cur"])
            await asyncio.sleep(0.02)
            peak["cur"] -= 1
            return _Resp(200)

    monkeypatch.setattr(sc.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(sc, "_http", None)
    monkeypatch.setattr(sc, "_inflight", None)
    monkeypatch.setattr(sc, "_cool_until", {})
    monkeypatch.setattr(sc, "RPC_MAX_INFLIGHT", 3)
    monkeypatch.setattr(sc, "RPC_MAX_RPS", 0.0)
    await asyncio.gather(*[sc.rpc_call("getSlot", []) for _ in range(12)])
    assert peak["n"] == 3
