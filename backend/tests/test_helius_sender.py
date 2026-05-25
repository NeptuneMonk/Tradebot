"""
Smoke test for helius_sender wrapper. Uses asyncio.run() instead of
pytest-asyncio so it slots into the existing sync-test harness.
"""
import asyncio
import base64
import os
from unittest.mock import patch, AsyncMock

import pytest

# Stub env BEFORE importing helius_sender (solana_client reads env at import)
os.environ.setdefault("HELIUS_RPC_URL", "https://x")
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")

import helius_sender as hs  # noqa: E402
from solders.keypair import Keypair  # noqa: E402


FAKE_BH = {"result": {"value": {"blockhash": "9zoz4M4DJxF6jE2BLk2pcQwTjeNqv4mpkVwH1L3ck7Bq"}}}


def _make_fake_client(captured: dict, response_json):
    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None):
            captured["url"] = url
            captured["body"] = json
            r = AsyncMock()
            r.raise_for_status = lambda: None
            r.json = lambda: response_json
            return r
    return FakeClient


def test_tip_transfer_ix_layout():
    kp = Keypair()
    ix = hs._build_tip_transfer_ix(kp.pubkey(), 200_000)
    assert ix.data[:4] == b"\x02\x00\x00\x00"
    assert int.from_bytes(ix.data[4:12], "little") == 200_000
    assert len(ix.accounts) == 2
    assert ix.accounts[0].is_signer and ix.accounts[0].is_writable
    assert (not ix.accounts[1].is_signer) and ix.accounts[1].is_writable
    assert str(ix.accounts[1].pubkey) in hs.TIP_ACCOUNTS


def test_send_via_sender_dual_mode_posts_correct_body():
    kp = Keypair()
    captured = {}
    FakeClient = _make_fake_client(
        captured, {"jsonrpc": "2.0", "id": "sender", "result": "FAKESIG"}
    )

    async def run():
        with patch.object(hs, "rpc_call", new=AsyncMock(return_value=FAKE_BH)), \
             patch.object(hs, "_poll_confirmation", new=AsyncMock(return_value=None)), \
             patch.object(hs.httpx, "AsyncClient", FakeClient):
            return await hs.send_via_sender(
                kp, [],
                priority_fee_microlamports=1_500_000,
                compute_unit_limit=400_000,
                mode="dual",
                confirm_timeout_s=10.0,
            )

    sig = asyncio.run(run())
    assert sig == "FAKESIG"
    assert captured["url"].endswith("/fast")
    assert "swqos_only" not in captured["url"]
    params = captured["body"]["params"]
    assert params[1] == {"encoding": "base64", "skipPreflight": True, "maxRetries": 0}
    raw = base64.b64decode(params[0])
    # 200_000 lamports in LE u64 = b'@\r\x03\x00\x00\x00\x00\x00'
    assert (200_000).to_bytes(8, "little") in raw


def test_send_via_sender_swqos_mode_uses_cheap_tip_and_swqos_url():
    kp = Keypair()
    captured = {}
    FakeClient = _make_fake_client(
        captured, {"jsonrpc": "2.0", "id": "sender", "result": "OK"}
    )

    async def run():
        with patch.object(hs, "rpc_call", new=AsyncMock(return_value=FAKE_BH)), \
             patch.object(hs, "_poll_confirmation", new=AsyncMock(return_value=None)), \
             patch.object(hs.httpx, "AsyncClient", FakeClient):
            await hs.send_via_sender(kp, [], mode="swqos", confirm_timeout_s=5.0)

    asyncio.run(run())
    assert "swqos_only=true" in captured["url"]
    raw = base64.b64decode(captured["body"]["params"][0])
    # 5_000 lamports LE u64
    assert (5_000).to_bytes(8, "little") in raw


def test_send_via_sender_propagates_sender_error():
    kp = Keypair()
    captured = {}
    FakeClient = _make_fake_client(
        captured,
        {"jsonrpc": "2.0", "id": "sender",
         "error": {"code": -32602, "message": "no tip"}},
    )

    async def run():
        with patch.object(hs, "rpc_call", new=AsyncMock(return_value=FAKE_BH)), \
             patch.object(hs.httpx, "AsyncClient", FakeClient):
            await hs.send_via_sender(kp, [], mode="swqos")

    with pytest.raises(RuntimeError, match="sender sendTransaction error"):
        asyncio.run(run())
