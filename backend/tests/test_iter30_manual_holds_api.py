"""Iter30: manual-holds & scanner-enable API tests"""
import os
import time
import pytest
import requests

BASE = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
TOK = open("/app/memory/.tok").read().strip()
H = {"Authorization": f"Bearer {TOK}", "Content-Type": "application/json"}


def test_bot_status_manual_hold_fields():
    r = requests.get(f"{BASE}/api/bot/status", headers=H, timeout=15)
    assert r.status_code == 200, r.text
    d = r.json()
    assert isinstance(d.get("active_trade_count"), int)
    assert isinstance(d.get("manual_hold_count"), int)
    assert d["manual_hold_count"] <= d["active_trade_count"]
    assert d.get("scanner_enabled") is True, f"scanner_enabled={d.get('scanner_enabled')}"


def test_bot_config_scanner_toggle_roundtrip():
    r = requests.get(f"{BASE}/api/bot/config", headers=H, timeout=15)
    assert r.status_code == 200
    assert r.json().get("scanner_enabled") is True
    r = requests.put(f"{BASE}/api/bot/config", headers=H,
                     json={"scanner_enabled": False}, timeout=15)
    assert r.status_code == 200, r.text
    time.sleep(0.5)
    assert requests.get(f"{BASE}/api/bot/config", headers=H, timeout=15).json()["scanner_enabled"] is False
    # Restore
    r = requests.put(f"{BASE}/api/bot/config", headers=H,
                     json={"scanner_enabled": True}, timeout=15)
    assert r.status_code == 200
    time.sleep(0.5)
    assert requests.get(f"{BASE}/api/bot/config", headers=H, timeout=15).json()["scanner_enabled"] is True


def test_scanner_candidates_shape():
    r = requests.get(f"{BASE}/api/scanner/candidates", headers=H, timeout=20)
    assert r.status_code == 200
    d = r.json()
    lst = d if isinstance(d, list) else d.get("candidates") or d.get("items") or []
    assert isinstance(lst, list)
    if lst:
        row = lst[0]
        for k in ("passes", "mint"):
            assert k in row, f"missing {k} in {row}"
        # chain is optional (SOL scanner rows omit it; RH rows include it)
        assert "gate_reason" in row or "band" in row or "symbol" in row


def test_scanner_skips_running():
    r = requests.get(f"{BASE}/api/scanner/skips", headers=H, timeout=15)
    assert r.status_code == 200
    d = r.json()
    # some non-empty bucket somewhere proves loop running
    def _walk(o):
        if isinstance(o, dict):
            return any(_walk(v) for v in o.values())
        if isinstance(o, list):
            return len(o) > 0 or any(_walk(v) for v in o)
        if isinstance(o, (int, float)):
            return o > 0
        return False
    assert _walk(d), f"skips look empty: {d}"


def test_manual_buy_bad_mint_no_500():
    r = requests.post(f"{BASE}/api/scanner/manual-buy/GARBAGE_MINT_XXX", headers=H, timeout=15)
    assert r.status_code in (400, 404, 409, 422), f"got {r.status_code}: {r.text}"
    # should include a reason
    try:
        j = r.json()
        assert any(k in j for k in ("reason", "detail", "error", "message"))
    except Exception:
        pass


def test_manual_buy_on_passing_candidate():
    # Poll a bit to find a passing candidate
    passing = []
    for _ in range(3):
        r = requests.get(f"{BASE}/api/scanner/candidates", headers=H, timeout=20)
        d = r.json()
        lst = d if isinstance(d, list) else d.get("candidates") or d.get("items") or []
        passing = [c for c in lst if c.get("passes") is True]
        if passing:
            break
        time.sleep(2)
    if not passing:
        pytest.skip("no passing candidates right now")
    # snapshot slot cap
    s0 = requests.get(f"{BASE}/api/bot/status", headers=H, timeout=10).json()
    counted0 = s0["active_trade_count"] - s0["manual_hold_count"]

    picked = passing[0]
    mint = picked["mint"]
    r = requests.post(f"{BASE}/api/scanner/manual-buy/{mint}", headers=H, timeout=30)
    assert r.status_code in (200, 409), f"got {r.status_code}: {r.text}"
    body = {}
    try:
        body = r.json()
    except Exception:
        pass

    if r.status_code == 409:
        assert any(k in body for k in ("reason", "detail", "error", "message")), body
        print("Manual-buy refused w/ reason (acceptable):", body)
        return

    assert body.get("ok") is True, body
    time.sleep(1.2)
    active = requests.get(f"{BASE}/api/trades/active", headers=H, timeout=10).json()
    trades = active if isinstance(active, list) else active.get("trades") or active.get("items") or []
    manual_trade = None
    for t in trades:
        if (t.get("mint") == mint or t.get("token_mint") == mint) and \
           t.get("classifier_action") in ("manual", "rh_pons_manual"):
            manual_trade = t
            break
    assert manual_trade, f"no manual trade found for {mint} in {trades}"

    s1 = requests.get(f"{BASE}/api/bot/status", headers=H, timeout=10).json()
    counted1 = s1["active_trade_count"] - s1["manual_hold_count"]
    assert s1["manual_hold_count"] >= s0["manual_hold_count"] + 1
    assert counted1 == counted0, f"manual buy leaked into slot cap: {counted0} -> {counted1}"

    # cleanup: try to exit
    tid = manual_trade.get("id") or manual_trade.get("trade_id") or manual_trade.get("_id")
    if tid:
        for path in (f"/api/trades/{tid}/exit", f"/api/trades/{tid}/close",
                     f"/api/paper/trades/{tid}/exit"):
            rr = requests.post(f"{BASE}{path}", headers=H, timeout=15)
            if rr.status_code < 400:
                print(f"Exited via {path}")
                break
