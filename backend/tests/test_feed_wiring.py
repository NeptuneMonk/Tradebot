"""Feed toggles vs master Start/Stop: separate states, start/stop persist only {enabled}, PUT applies patch keys only."""
import asyncio
import os
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = os.environ.get("REACT_APP_BACKEND_URL") or [l.split("=", 1)[1].strip() for l in open("/app/frontend/.env") if l.startswith("REACT_APP_BACKEND_URL")][0]
H = {"Authorization": f"Bearer {open('/app/memory/.tok').read().strip()}"}
FEED = ["helius_tracker_enabled", "rh_feed_enabled", "rh_paper_enabled", "rh_live_trading", "scanner_enabled"]


def _cfg():
    return requests.get(f"{BASE}/api/bot/config", headers=H, timeout=20).json()


def test_status_exposes_desired_and_actual_separately():
    st = requests.get(f"{BASE}/api/bot/status", headers=H, timeout=20).json()
    for k in ("enabled", "helius_tracker_enabled", "listener_connected", "rh_feed_enabled", "rh_feed_alive", "rh_paper_enabled", "rh_live_trading", "scanner_enabled"):
        assert k in st, k
    assert isinstance(st["listener_connected"], bool) and isinstance(st["rh_feed_alive"], bool)


def test_start_stop_never_touch_feed_keys():
    before = _cfg()
    was_enabled = before["enabled"]
    try:
        r = requests.put(f"{BASE}/api/bot/config", json={"rh_feed_enabled": False}, headers=H, timeout=20)
        assert r.status_code == 200 and r.json()["rh_feed_enabled"] is False
        snap = {k: _cfg()[k] for k in FEED}
        assert requests.post(f"{BASE}/api/bot/start", headers=H, timeout=20).status_code == 200
        after = _cfg()
        assert after["enabled"] is True and after["rh_feed_enabled"] is False          # the reported bug
        assert {k: after[k] for k in FEED} == snap
        assert requests.post(f"{BASE}/api/bot/stop?mode=graceful", headers=H, timeout=20).status_code == 200
        after2 = _cfg()
        assert {k: after2[k] for k in FEED} == snap
    finally:
        requests.put(f"{BASE}/api/bot/config", json={k: before[k] for k in FEED}, headers=H, timeout=20)
        requests.post(f"{BASE}/api/bot/start" if was_enabled else f"{BASE}/api/bot/stop?mode=graceful", headers=H, timeout=20)
        if not was_enabled:
            requests.post(f"{BASE}/api/bot/abort", headers=H, timeout=20) if False else None


def test_put_config_applies_patch_keys_only():
    before = _cfg()
    try:
        requests.put(f"{BASE}/api/bot/config", json={"helius_tracker_enabled": False, "rh_feed_enabled": True}, headers=H, timeout=20)
        r = requests.put(f"{BASE}/api/bot/config", json={"min_trade_usd": before["min_trade_usd"]}, headers=H, timeout=20).json()
        assert r["helius_tracker_enabled"] is False and r["rh_feed_enabled"] is True     # omitted keys untouched, no defaults merged
        assert r["max_trade_usd"] == before["max_trade_usd"]
        r = requests.put(f"{BASE}/api/bot/config", json={"enabled": not before["enabled"]}, headers=H, timeout=20).json()
        assert r["enabled"] == before["enabled"]                                       # `enabled` is owned by start/stop
    finally:
        requests.put(f"{BASE}/api/bot/config", json={k: before[k] for k in FEED}, headers=H, timeout=20)


def test_helius_toggle_drives_gate_and_listener():
    import helius_gate
    import listener as L
    lst = L.PumpFunListener.__new__(L.PumpFunListener)
    lst._task, lst._ws, lst.connected, lst._stop = None, None, True, False

    class _WS:
        closed = False
        async def close(self): self.closed = True
    ws = _WS(); lst._ws = ws
    asyncio.run(lst.disconnect())
    assert lst.connected is False and ws.closed is True and lst._ws is None
    # server.sync_helius_feed: OFF pauses the gate + disconnects; ON unpauses + starts the task
    import server
    server.listener = lst
    started = []
    lst.start = lambda: started.append(True)
    asyncio.run(server.sync_helius_feed(False, True))
    assert helius_gate.is_helius_paused() is True and lst.connected is False
    asyncio.run(server.sync_helius_feed(True, False))
    assert helius_gate.is_helius_paused() is False and started == [True]


def test_start_with_helius_off_does_not_reconnect():
    before = _cfg()
    try:
        requests.put(f"{BASE}/api/bot/config", json={"helius_tracker_enabled": False}, headers=H, timeout=20)
        import time; time.sleep(2.5)
        assert requests.post(f"{BASE}/api/bot/start", headers=H, timeout=20).status_code == 200
        time.sleep(2.0)
        st = requests.get(f"{BASE}/api/bot/status", headers=H, timeout=20).json()
        assert st["enabled"] is True and st["helius_tracker_enabled"] is False and st["listener_connected"] is False
    finally:
        requests.put(f"{BASE}/api/bot/config", json={"helius_tracker_enabled": before["helius_tracker_enabled"]}, headers=H, timeout=20)
        if not before["enabled"]:
            requests.post(f"{BASE}/api/bot/stop?mode=graceful", headers=H, timeout=20)


def test_status_has_listener_health_and_start_kicks_listener():
    import time
    before = _cfg()
    try:
        requests.put(f"{BASE}/api/bot/config", json={"helius_tracker_enabled": True}, headers=H, timeout=20)
        r = requests.post(f"{BASE}/api/bot/start", headers=H, timeout=20).json()
        assert "listener" in r and {"connected", "last_error", "last_ok_ts", "last_attempt_ts", "task_alive"} <= set(r["listener"])
        assert r["listener"]["task_alive"] is True
        time.sleep(3)
        st = requests.get(f"{BASE}/api/bot/status", headers=H, timeout=20).json()
        for k in ("helius_paused", "listener_last_error", "listener_last_ok_ts", "listener_last_attempt_ts"):
            assert k in st, k
        assert st["helius_tracker_enabled"] is True and st["helius_paused"]["manual"] is False
        # either connected, or the payload says why (paused / error / connecting)
        assert st["listener_connected"] or st["listener_last_error"] or (st["listener_last_attempt_ts"] and time.time() - st["listener_last_attempt_ts"] < 15)
    finally:
        requests.put(f"{BASE}/api/bot/config", json={"helius_tracker_enabled": before["helius_tracker_enabled"]}, headers=H, timeout=20)
        if not before["enabled"]:
            requests.post(f"{BASE}/api/bot/stop?mode=graceful", headers=H, timeout=20)


def test_listener_reports_pause_reason_and_kick_resets_backoff():
    import helius_gate
    import listener as L
    lst = L.PumpFunListener.__new__(L.PumpFunListener)
    lst._task, lst._ws, lst._stop, lst.connected, lst._kick = None, None, False, False, False
    lst.last_error, lst.last_ok_ts, lst.last_attempt_ts = None, 0.0, 0.0
    lst.start = lambda: None
    lst.kick()
    assert lst._kick is True
    h = lst.health()
    assert h["connected"] is False and h["task_alive"] is False and h["last_ok_ts"] is None
    helius_gate.set_auto_paused(True, "live-doctor paused scalp + hunt · no open Solana position")
    assert helius_gate.snapshot()["auto_reason"].startswith("live-doctor")
    helius_gate.set_auto_paused(False)
