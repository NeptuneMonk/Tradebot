"""Sticky-to-leader: a follower bounces X-Prefer-Leader calls (421) and ?leader_only=1 sockets (4409) instead of
relaying, so the browser can re-roll the load balancer; without the opt-in it relays / mirrors as before."""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

import server
from auth import get_current_user

server.app.dependency_overrides[get_current_user] = lambda: {"user_id": "u"}


class _Relay:
    def __init__(self):
        self.calls = 0
        self.stats = {}

    async def submit(self, request):
        self.calls += 1
        from fastapi.responses import JSONResponse
        return JSONResponse({"relayed": True}, headers={"X-Pod-Relayed": "1"})


@pytest.fixture
def follower(monkeypatch):
    lease = SimpleNamespace(is_leader=False, pod_id="pod-f-1", leader_alive=lambda: True)
    relay = _Relay()
    monkeypatch.setattr(server, "singleton", lease)
    monkeypatch.setattr(server, "relay", relay)
    monkeypatch.setattr(server, "validate_token_str", AsyncMock(return_value={"user_id": "u"}))
    for k in server.STICKY_STATS:
        server.STICKY_STATS[k] = 0
    return lease, relay


def test_follower_bounces_prefer_leader_with_421(follower):
    _, relay = follower
    c = TestClient(server.app)   # no lifespan: startup would build the real lease and undo the patches
    if True:
        r = c.get("/api/bot/status", headers={"X-Prefer-Leader": "1"})
    assert r.status_code == 421
    assert r.headers["X-Pod-Role"] == "follower" and r.headers["X-Pod-Id"] == "pod-f-1" and r.headers["X-Pod-Retry"] == "1"
    assert relay.calls == 0 and server.STICKY_STATS["http_bounced"] == 1


def test_follower_relays_without_the_header(follower):
    _, relay = follower
    c = TestClient(server.app)   # no lifespan: startup would build the real lease and undo the patches
    if True:
        r = c.get("/api/bot/status")
    assert r.status_code == 200 and r.json() == {"relayed": True} and relay.calls == 1
    assert server.STICKY_STATS["http_relayed"] == 1


def test_pods_endpoint_is_never_bounced(follower):
    lease, _ = follower
    lease.info = lambda: {"pod_id": "pod-f-1", "role": "follower"}
    c = TestClient(server.app)   # no lifespan: startup would build the real lease and undo the patches
    if True:
        r = c.get("/api/pods", headers={"X-Prefer-Leader": "1"})
    assert r.status_code == 200 and r.json()["sticky"]["http_bounced"] == 0


def test_leader_serves_prefer_leader_directly(monkeypatch):
    lease = SimpleNamespace(is_leader=True, pod_id="pod-L", leader_alive=lambda: True, info=lambda: {"role": "leader"})
    monkeypatch.setattr(server, "singleton", lease)
    monkeypatch.setattr(server, "relay", None)
    c = TestClient(server.app)   # no lifespan: startup would build the real lease and undo the patches
    if True:
        r = c.get("/api/pods", headers={"X-Prefer-Leader": "1"})
    assert r.status_code == 200 and r.headers["X-Pod-Role"] == "leader" and r.headers["X-Pod-Id"] == "pod-L"


def test_follower_ws_bounces_leader_only_with_4409(follower):
    from starlette.websockets import WebSocketDisconnect
    c = TestClient(server.app)   # no lifespan: startup would build the real lease and undo the patches
    if True:
        with pytest.raises(WebSocketDisconnect) as ei:
            with c.websocket_connect("/api/ws?leader_only=1&token=t") as ws:
                ws.receive_json()
    assert ei.value.code == 4409 and server.STICKY_STATS["ws_bounced"] == 1


def test_follower_ws_without_opt_in_serves_mirror_and_announces_role(follower, monkeypatch):
    monkeypatch.setattr(server, "bot_status", AsyncMock(return_value=SimpleNamespace(model_dump=lambda: {"enabled": False})))
    c = TestClient(server.app)   # no lifespan: startup would build the real lease and undo the patches
    if True:
        with c.websocket_connect("/api/ws?token=t") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "pod" and hello["data"] == {"role": "follower", "pod_id": "pod-f-1", "mirror": True}
            assert ws.receive_json()["type"] == "status"
