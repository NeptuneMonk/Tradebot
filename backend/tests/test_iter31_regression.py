"""Iter31: regression + P0 diagnostics for ws_hub slim frames.

Runs against live backend via REACT_APP_BACKEND_URL. Uses conftest session.
Does NOT toggle live_trading / feeds. Does NOT start the bot.
"""
import os
import time
import json
import pytest
import requests
import websocket  # from websocket-client

BASE = os.environ.get("REACT_APP_BACKEND_URL", "https://micro-stake-trader.preview.emergentagent.com").rstrip("/")


def _tok():
    with open("/app/memory/.tok") as f:
        return f.read().strip()


HEADERS = {"Authorization": f"Bearer {_tok()}"}


# ---- Regression: core endpoints return 200 with expected shapes ----
def test_bot_status_shape():
    r = requests.get(f"{BASE}/api/bot/status", headers=HEADERS, timeout=15)
    assert r.status_code == 200
    d = r.json()
    for k in ("enabled", "active_trade_count"):
        assert k in d, f"missing {k}"


def test_bot_config_shape():
    r = requests.get(f"{BASE}/api/bot/config", headers=HEADERS, timeout=15)
    assert r.status_code == 200
    d = r.json()
    assert isinstance(d, dict) and len(d) > 5


def test_launches_recent_caps_30():
    r = requests.get(f"{BASE}/api/launches/recent?limit=30", headers=HEADERS, timeout=15)
    assert r.status_code == 200
    d = r.json()
    rows = d if isinstance(d, list) else d.get("launches", d.get("items", []))
    assert isinstance(rows, list)
    # /launches/recent returns SOL+RH combined; limit=30 means 30 per chain (up to 60 total)
    assert len(rows) <= 60


def test_trades_active():
    r = requests.get(f"{BASE}/api/trades/active", headers=HEADERS, timeout=15)
    assert r.status_code == 200
    d = r.json()
    assert isinstance(d, (list, dict))


def test_trades_history_capped_50():
    r = requests.get(f"{BASE}/api/trades/history?limit=50", headers=HEADERS, timeout=15)
    assert r.status_code == 200
    d = r.json()
    rows = d if isinstance(d, list) else d.get("trades", d.get("items", []))
    assert isinstance(rows, list)
    assert len(rows) <= 50


def test_scanner_candidates():
    r = requests.get(f"{BASE}/api/scanner/candidates", headers=HEADERS, timeout=15)
    assert r.status_code == 200


def test_pl_equity():
    r = requests.get(f"{BASE}/api/pl/equity", headers=HEADERS, timeout=15)
    assert r.status_code == 200


# ---- P1: ws_hub diagnostics ----
def test_diagnostics_loop_has_ws_hub():
    r = requests.get(f"{BASE}/api/diagnostics/loop", headers=HEADERS, timeout=15)
    assert r.status_code == 200
    d = r.json()
    assert "ws_hub" in d, f"no ws_hub key. got keys: {list(d.keys())}"
    hub = d["ws_hub"]
    for k in ("clients", "seen", "ident", "frames", "bytes"):
        assert k in hub, f"missing ws_hub.{k}"
    # Caps per P1
    assert hub["seen"] <= 200, f"seen cap violated: {hub['seen']}"
    assert hub["ident"] <= 1500, f"ident cap violated: {hub['ident']}"


# ---- P0/P1: WS handshake + slim frame shape ----
def test_ws_handshake_and_diagnostics_client_count():
    # Read pre-connect client count
    pre = requests.get(f"{BASE}/api/diagnostics/loop", headers=HEADERS, timeout=15).json()["ws_hub"]["clients"]

    ws_url = BASE.replace("https://", "wss://").replace("http://", "ws://") + "/api/ws"
    ws = websocket.create_connection(
        ws_url,
        header=[f"Authorization: Bearer {_tok()}"],
        cookie=f"session_token={_tok()}",
        timeout=15,
    )
    try:
        # collect a few frames within ~6s (status/wallet tick every ~3s)
        ws.settimeout(6)
        got = []
        end = time.time() + 6.5
        while time.time() < end:
            try:
                msg = ws.recv()
                if not msg:
                    continue
                obj = json.loads(msg)
                assert "type" in obj, f"frame missing type: {msg[:200]}"
                got.append(obj)
            except websocket.WebSocketTimeoutException:
                break
        assert len(got) >= 1, "no WS frames within 6s"

        # while socket open, client count should be >= pre+1
        mid = requests.get(f"{BASE}/api/diagnostics/loop", headers=HEADERS, timeout=15).json()["ws_hub"]["clients"]
        assert mid >= max(1, pre), f"clients did not grow: pre={pre} mid={mid}"

        # If any candidate/candidate_update frame arrived, check slim shape
        for f in got:
            if f.get("type") == "candidate":
                data = f.get("data", {})
                # slim frame must carry a seq
                assert "seq" in data, f"candidate missing seq: {data}"
            if f.get("type") == "candidate_update":
                data = f.get("data", {})
                assert "id" in data and "seq" in data and "p" in data, f"bad patch: {data}"
                assert isinstance(data["p"], dict)
    finally:
        ws.close()
