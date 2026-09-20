"""Explorer caching (P2): a Solscan cache hit never touches HTTP; errors are negative-cached; RugCheck cache works the same."""
import asyncio

import pytest

import solscan
import rugcheck


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, str(body)

    def json(self):
        return self._body


def _client_factory(calls, status=200, body=None):
    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, **k):
            calls.append(url)
            return _Resp(status, body if body is not None else {"success": True, "data": {"ok": 1}})
    return _Client


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    solscan._cache.clear(); rugcheck._cache.clear()
    monkeypatch.setattr(solscan, "API_KEY", "test-key")
    monkeypatch.setattr(solscan, "_plan_blocked_until", 0.0)


def test_solscan_cache_hit_skips_http(monkeypatch):
    calls = []
    monkeypatch.setattr(solscan.httpx, "AsyncClient", _client_factory(calls))
    a = asyncio.run(solscan.account_detail("W1"))
    b = asyncio.run(solscan.account_detail("W1"))
    assert a == b == {"ok": 1} and len(calls) == 1
    asyncio.run(solscan.account_detail("W2"))
    assert len(calls) == 2


def test_solscan_error_is_negative_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(solscan.httpx, "AsyncClient", _client_factory(calls, status=429, body={"success": False, "errors": {"message": "quota"}}))
    for _ in range(3):
        with pytest.raises(solscan.SolscanError):
            asyncio.run(solscan.token_meta("M1"))
    assert len(calls) == 1


def test_solscan_cache_cap(monkeypatch):
    for i in range(solscan.CACHE_CAP + 10):
        solscan._cache_put(("p", (("a", str(i)),)), {"i": i}, 60)
    assert len(solscan._cache) == solscan.CACHE_CAP


def test_rugcheck_summary_cached(monkeypatch):
    calls = []
    body = {"score": 1200, "score_normalised": 35, "rugged": False, "risks": [{"name": "Low liquidity", "level": "warn", "description": "x", "score": 100}]}
    monkeypatch.setattr(rugcheck.httpx, "AsyncClient", _client_factory(calls, body=body))
    monkeypatch.setattr(rugcheck, "_last_call", 0.0)
    a = asyncio.run(rugcheck.summary("MINT"))
    b = asyncio.run(rugcheck.summary("MINT"))
    assert a == b and a["score_normalised"] == 35 and a["risks"][0]["level"] == "warn" and len(calls) == 1
