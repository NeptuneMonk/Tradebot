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
