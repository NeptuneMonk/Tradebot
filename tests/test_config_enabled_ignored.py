"""Test: PUT /api/bot/config must ignore 'enabled' key (only start/stop controls it)."""
import os
import time
import requests

BASE = "https://micro-stake-trader.preview.emergentagent.com"
TOKEN = "test_session_1788909052723"
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


def get_status():
    return requests.get(f"{BASE}/api/bot/status", headers=H, timeout=15).json()


def get_config():
    return requests.get(f"{BASE}/api/bot/config", headers=H, timeout=15).json()


def test_enabled_key_in_config_body_is_ignored():
    # Confirm paper mode before starting
    cfg = get_config()
    assert cfg["live_trading"] is False, "SAFETY: live_trading must be false"
    assert cfg["rh_live_trading"] is False, "SAFETY: rh_live_trading must be false"

    # Start bot
    r = requests.post(f"{BASE}/api/bot/start", headers=H, timeout=15)
    assert r.status_code == 200, f"start failed: {r.status_code} {r.text}"
    time.sleep(1)

    st = get_status()
    assert st["enabled"] is True, f"bot should be enabled after start, got {st}"

    # PUT config with enabled=false + rh_feed_enabled=true — enabled must be ignored
    r = requests.put(
        f"{BASE}/api/bot/config",
        headers=H,
        json={"enabled": False, "rh_feed_enabled": True},
        timeout=15,
    )
    assert r.status_code == 200, f"config PUT failed: {r.status_code} {r.text}"
    body = r.json()
    assert body.get("enabled") is True, f"response.enabled should stay true, got {body.get('enabled')}"
    assert body.get("rh_feed_enabled") is True

    time.sleep(0.5)
    st2 = get_status()
    assert st2["enabled"] is True, f"status.enabled should stay true, got {st2}"

    # Restore rh_feed_enabled=false
    r = requests.put(
        f"{BASE}/api/bot/config", headers=H, json={"rh_feed_enabled": False}, timeout=15
    )
    assert r.status_code == 200
    assert r.json().get("rh_feed_enabled") is False

    # Hard stop to restore
    r = requests.post(f"{BASE}/api/bot/stop?mode=hard", headers=H, timeout=15)
    assert r.status_code == 200, f"stop failed: {r.status_code} {r.text}"
    time.sleep(1)
    st3 = get_status()
    assert st3["enabled"] is False, f"bot should be stopped, got {st3}"


if __name__ == "__main__":
    test_enabled_key_in_config_body_is_ignored()
    print("PASS")
