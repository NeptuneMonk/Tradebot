"""Phase 3b reputation adapter: dark when unset, tier normalisation, FARMER master gate, hunt allow-tiers, caching."""
import asyncio
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

import reputation
from reputation import ReputationClient, normalize_tier, parse


def test_dark_until_url_is_set(monkeypatch):
    monkeypatch.setenv("REPUTATION_BASE_URL", "")
    assert reputation.configured() is False
    monkeypatch.setenv("REPUTATION_BASE_URL", "https://rep.example/api/{mint}")
    assert reputation.configured() is True


def test_tier_normalisation_and_parse(monkeypatch):
    monkeypatch.delenv("REPUTATION_TIER_PATH", raising=False)
    monkeypatch.delenv("REPUTATION_FAKE_CHART_PATH", raising=False)
    assert normalize_tier("Proven Dev") == "PROVEN" and normalize_tier("crazy") == "CRAZY" and normalize_tier("meh") == "UNKNOWN"
    assert parse({"data": {"dev": {"tier": "Farmer", "fakeChart": "true"}}}) == {"tier": "FARMER", "fake_chart": True, "raw_tier": "Farmer"}
    monkeypatch.setenv("REPUTATION_TIER_PATH", "result.0.grade")
    assert parse({"result": [{"grade": "GOOD"}]})["tier"] == "GOOD"


def test_gate_rules():
    c = ReputationClient()
    assert c.gate({"tier": "FARMER", "fake_chart": False, "ok": True}, "scalp") == "reputation:FARMER"      # master gate, every book
    assert c.gate({"tier": "GOOD", "fake_chart": True, "ok": True}, "scalp") == "reputation:fake-chart"
    assert c.gate({"tier": "GOOD", "fake_chart": False, "ok": True}, "scalp") is None
    assert c.gate({"tier": "UNKNOWN", "fake_chart": False, "ok": False}, "scalp") is None                    # scalp: miss is not a skip
    assert c.gate({"tier": "UNKNOWN", "fake_chart": False, "ok": False}, "hunt") == "reputation:UNKNOWN"      # hunt: hard skip on miss
    for t in ("CRAZY", "PROVEN", "GOOD"):
        assert c.gate({"tier": t, "fake_chart": False, "ok": True}, "hunt") is None
    assert c.stats["skips_farmer"] == 2 and c.stats["skips_hunt"] == 1


def test_lookup_caches_and_sends_key(monkeypatch):
    monkeypatch.setenv("REPUTATION_BASE_URL", "https://rep.example/api")
    monkeypatch.setenv("REPUTATION_API_KEY", "k123")
    monkeypatch.delenv("REPUTATION_TIER_PATH", raising=False)
    seen = []

    def handler(req):
        seen.append((str(req.url), req.headers.get("authorization")))
        if req.url.path.endswith("/MINTA"):
            return httpx.Response(200, json={"tier": "proven", "fake_chart": False})
        return httpx.Response(404)
    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))

    async def run():
        c = ReputationClient()
        r1 = await c.lookup("MINTA", "dev1"); r2 = await c.lookup("MINTA", "dev1")
        assert r1["tier"] == "PROVEN" and r1["ok"] and r2 == r1 and c.stats["lookups"] == 1 and c.stats["hits"] == 1
        assert seen[0] == ("https://rep.example/api/MINTA", "Bearer k123")
        miss = await c.lookup("MINTB", None)
        assert miss["tier"] == "UNKNOWN" and miss["ok"] is False and c.stats["misses"] == 1
        await c.lookup("MINTB", None)
        assert c.stats["lookups"] == 2                      # negative result cached too
        assert c.snapshot()["configured"] is True
    asyncio.run(run())


def test_reputation_family_token_shape(monkeypatch):
    """Real payload shape from reputation.family /api/token/{mint} (captured 2026-10-04) with the documented env paths."""
    monkeypatch.setenv("REPUTATION_TIER_PATH", "dev.rank")
    monkeypatch.setenv("REPUTATION_FAKE_CHART_PATH", "fake")
    doc = {"mint": "3jWq…pump", "platform": "pump", "fake": False, "fakeReason": None,
           "copyOf": {"mint": "AfUz…pump", "ticker": True}, "socials": {"x": None},
           "dev": {"rank": "FARMER", "score": 5, "launches": 17, "graduated": 0}}
    assert parse(doc) == {"tier": "FARMER", "fake_chart": False, "raw_tier": "FARMER"}
    assert parse({**doc, "fake": True, "dev": {"rank": "GOOD"}})["fake_chart"] is True
    assert parse({**doc, "dev": {"rank": "UNKNOWN"}})["tier"] == "UNKNOWN"
    assert parse(None) == {"tier": "UNKNOWN", "fake_chart": False, "raw_tier": None}     # 404 body is `null`


def test_dev_wallet_rank_decides_before_the_coin_is_indexed(monkeypatch):
    """Fresh launches 404 on /api/token for minutes; /api/dev/{creator} answers instantly — the gate must use it."""
    import asyncio
    import reputation as rep_mod
    monkeypatch.setenv("REPUTATION_BASE_URL", "https://reputation.family/api/token/{mint}")
    monkeypatch.delenv("REPUTATION_DEV_URL", raising=False)
    c = rep_mod.ReputationClient()
    calls = []

    class _R:
        def __init__(self, code, body=None): self.status_code, self._b = code, body
        def json(self): return self._b

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None):
            calls.append(url)
            if "/api/dev/" in url:
                w = url.rsplit("/", 1)[-1]
                return _R(200, {"dev": {"wallet": w, "rank": {"FARM": "FARMER", "GOOD": "PROVEN"}.get(w[:4], "UNKNOWN"), "launches": 3}})
            return _R(404)                                     # coin not indexed yet
    monkeypatch.setattr(rep_mod.httpx, "AsyncClient", _Client)
    r = asyncio.run(c.lookup("MintA", creator="FARMxxxx"))
    assert r["tier"] == "FARMER" and r["ok"] and r["source"] == "dev" and r["fake_chart"] is False
    assert c.gate(r, "scalp") == "reputation:FARMER"
    r2 = asyncio.run(c.lookup("MintB", creator="GOODyyyy"))
    assert r2["tier"] == "PROVEN" and c.gate(r2, "hunt") is None
    r3 = asyncio.run(c.lookup("MintC", creator="NEWzzzzz"))
    assert r3["tier"] == "UNKNOWN" and not r3["ok"] and c.gate(r3, "hunt") == "reputation:UNKNOWN"
    assert calls[0].endswith("/api/dev/FARMxxxx") and calls[1].endswith("/api/token/MintA")
    n = len(calls)
    asyncio.run(c.lookup("MintD", creator="FARMxxxx"))         # same farmer wallet again: dev tier served from the wallet cache
    assert not any("/api/dev/" in u for u in calls[n:])
    assert c.stats["dev_hits"] == 3 and c.stats["misses"] == 4
