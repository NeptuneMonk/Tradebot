"""WSS quota fallback: exhausted primary → public node, sticky until reset (feed OFF→ON)."""
import asyncio
import time
import json
import sys
import os
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")
os.environ.setdefault("SOLANA_RPC_URL", "https://paid.example/x")
os.environ.setdefault("SOLANA_WSS_URL", "wss://paid.example/x")

import solana_client as sc  # noqa: E402
from solana_client import WssRouter, PUBLIC_WSS_URL, wss_label  # noqa: E402

PAID = "wss://paid.example/x"
QUOTA = json.dumps({"jsonrpc": "2.0", "error": {"code": -32003, "message": "request limit reached"}, "id": 1})


def test_router_order_and_sticky_exhaustion():
    r = WssRouter(PAID, ["wss://alt.example/y"])
    assert r.urls == [PAID, "wss://alt.example/y", PUBLIC_WSS_URL]
    assert r.current() == PAID and not r.on_fallback()
    assert r.mark_exhausted(PAID) is True
    assert r.current() == "wss://alt.example/y" and r.on_fallback()
    assert r.mark_exhausted("wss://alt.example/y") is True
    assert r.current() == PUBLIC_WSS_URL
    assert r.mark_exhausted(PUBLIC_WSS_URL) is False      # nothing left → caller sleeps 5 min
    assert r.current() == PUBLIC_WSS_URL                  # last resort still gets retried
    r.reset()
    assert r.current() == PAID and not r.exhausted


def test_public_primary_has_no_duplicate():
    r = WssRouter(PUBLIC_WSS_URL, [])
    assert r.urls == [PUBLIC_WSS_URL]
    assert wss_label(PUBLIC_WSS_URL) == "public WSS" and wss_label(PAID) == "paid.example"


class _FakeWs:
    def __init__(self, msgs):
        self._msgs = list(msgs)
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, m):
        self.sent.append(m)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._msgs:
            await asyncio.sleep(3600)
        return self._msgs.pop(0)


def test_listener_switches_to_public_on_quota(monkeypatch):
    import listener as lmod
    router = WssRouter(PAID, [])
    monkeypatch.setattr(lmod, "wss_router", router)
    monkeypatch.setattr(sc, "wss_router", router)
    connects = []

    def fake_connect(url, **kw):
        connects.append(url)
        return _FakeWs([QUOTA] if url == PAID else [])

    monkeypatch.setattr(lmod, "websockets", types.SimpleNamespace(connect=fake_connect))
    monkeypatch.setitem(sys.modules, "helius_gate", types.SimpleNamespace(
        is_helius_paused=lambda: False, snapshot=lambda: {"manual": False, "auto_reason": None}))

    async def run():
        li = lmod.PumpFunListener(on_launch=None)
        li.start()
        for _ in range(60):
            await asyncio.sleep(0.1)
            if li.connected and li.via:
                break
        snap = (li.connected, li.via, dict(li.health()))
        li.stop()
        return snap

    connected, via, health = asyncio.run(run())
    assert connects[:2] == [PAID, PUBLIC_WSS_URL]
    assert connected and via == "public WSS"
    assert PAID in router.exhausted
    assert health["via"] == "public WSS"


def test_router_handshake_rejection_fails_over_then_recovers(monkeypatch):
    import solana_client as sc
    r = WssRouter(PUBLIC_WSS_URL, ["wss://helius.example/ws"])
    assert r.current() == PUBLIC_WSS_URL
    assert r.mark_rejected(PUBLIC_WSS_URL) is None                 # first 413: retry the same endpoint
    assert r.mark_rejected(PUBLIC_WSS_URL) == "wss://helius.example/ws"   # second: skip it, use the paid WSS
    assert r.current() == "wss://helius.example/ws" and r.on_fallback()
    r.rejected_until[PUBLIC_WSS_URL] -= sc.REJECT_SKIP_S + 1       # skip window over: public node retried
    assert r.current() == PUBLIC_WSS_URL
    r.mark_connected(PUBLIC_WSS_URL)
    assert not r.rejected_until and not r.on_fallback()


def test_router_all_rejected_knocks_on_least_recent():
    r = WssRouter(PUBLIC_WSS_URL, ["wss://helius.example/ws"])
    for _ in range(2):
        r.mark_rejected(PUBLIC_WSS_URL)
    for _ in range(2):
        r.mark_rejected("wss://helius.example/ws")
    assert r.current() == PUBLIC_WSS_URL
