"""RH sequencer feed: a 403/429 with Retry-After (Cloudflare 1-hour IP block) must be slept out, never hammered."""
import asyncio
import os
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rh_feed  # noqa: E402


class _FakeInvalidStatus(Exception):
    def __init__(self, status, headers, body=b""):
        super().__init__(f"server rejected WebSocket connection: HTTP {status}")
        self.response = SimpleNamespace(headers=headers, body=body)


@pytest.mark.asyncio
async def test_feed_honours_retry_after_and_minimum_reject_backoff(monkeypatch):
    sleeps: list[float] = []
    attempts = {"n": 0}

    class _Conn:
        async def __aenter__(self):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise _FakeInvalidStatus(403, {"retry-after": "3401"}, b'{"error":{"code":403,"message":"Blocked for 1 hour"}}')
            raise _FakeInvalidStatus(429, {})

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(rh_feed.websockets, "connect", lambda *a, **k: _Conn())

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(rh_feed.asyncio, "sleep", fake_sleep)
    feed = rh_feed.RHSequencerFeed(SimpleNamespace(config=SimpleNamespace(rh_feed_enabled=True, rh_seq_feed_enabled=True), rh_discovery=None))
    with pytest.raises(asyncio.CancelledError):
        await feed._loop()
    # sleeps[0] is the 3 s boot delay; the 403 with Retry-After sleeps ~3401 s (±20 % jitter), the plain 429 ≥ 45 s
    assert 3401 * 0.8 <= sleeps[1] <= 3401 * 1.2
    assert sleeps[2] >= rh_feed.REJECT_BACKOFF_S * 0.8
    assert feed.stats["blocked_until"] > time.time() + 3000
    assert "429" in feed.stats["last_error"]          # last_error reflects the most recent attempt
