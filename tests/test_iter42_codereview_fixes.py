# Iteration 42: code-review fixes ba5782d — wallet key env-only, exit fee budget clamp, live-arm refusal, latency medians, net_if_sold, exit fees
# Lives OUTSIDE backend/ so pytest edits don't trigger uvicorn --reload (which auto-disables the bot).
import os
import requests
from datetime import datetime, timezone

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    for line in open("/app/frontend/.env"):
        if line.startswith("REACT_APP_BACKEND_URL="):
            BASE_URL = line.split("=", 1)[1].strip().rstrip("/")
TOKEN = open("/app/memory/.tok").read().strip()
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
OLD_PK = "8h7RHbmqfAnAN5NZeAX9B9z8TKubkAqPBqdiHKTnn2ue"


def get_cfg():
    r = requests.get(f"{BASE_URL}/api/bot/config", headers=H, timeout=30)
    assert r.status_code == 200, r.text
    return r.json()


def put_cfg(body):
    return requests.put(f"{BASE_URL}/api/bot/config", headers=H, json=body, timeout=30)


def _ts(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return None


# --- wallet: export endpoint removed, import confirm check
def test_export_private_key_removed():
    r = requests.get(f"{BASE_URL}/api/wallet/export-private-key?confirm=EXPORT", headers=H, timeout=30)
    print("export status", r.status_code, r.text[:200])
    assert r.status_code in (404, 405)
    assert "secret" not in r.text.lower() and "private_key" not in r.text.lower()


def test_import_confirm_check():
    r = requests.post(f"{BASE_URL}/api/wallet/import", headers=H, json={"secret": "x", "confirm": "nope"}, timeout=30)
    print("import status", r.status_code, r.text[:200])
    assert r.status_code == 400
    assert "IMPORT" in r.text


def test_wallet_new_paper_key():
    r = requests.get(f"{BASE_URL}/api/wallet", headers=H, timeout=30)
    assert r.status_code == 200, r.text
    d = r.json()
    print("wallet", {k: d[k] for k in list(d)[:8]})
    assert isinstance(d.get("public_key"), str) and len(d["public_key"]) >= 32
    assert d["public_key"] != OLD_PK
    assert "secret" not in str(d).lower() or "secret_key" not in d


# --- exit fee budget clamp
def test_exit_fee_budget_clamp_and_restore():
    c = get_cfg()
    assert c["exit_fee_budget_pct_of_position"] == 5.0
    mx = float(c["exit_fee_max_pct_of_position"])
    enabled_before = c.get("enabled")
    try:
        r = put_cfg({"exit_fee_budget_pct_of_position": 50})
        assert r.status_code == 200, r.text
        assert get_cfg()["exit_fee_budget_pct_of_position"] == 25.0
        r = put_cfg({"exit_fee_budget_pct_of_position": 0.5})
        assert r.status_code == 200, r.text
        assert get_cfg()["exit_fee_budget_pct_of_position"] == mx
    finally:
        put_cfg({"exit_fee_budget_pct_of_position": 5.0})
    c2 = get_cfg()
    assert c2["exit_fee_budget_pct_of_position"] == 5.0
    assert c2.get("enabled") == enabled_before


# --- live-arm refusal
def test_live_trading_refused():
    r = put_cfg({"live_trading": True})
    print("live", r.status_code, r.text[:200])
    assert r.status_code == 409
    assert "SOLANA_PRIVATE_KEY" in r.text
    assert get_cfg()["live_trading"] is False


# --- costs landing latency medians
def test_costs_landing_latency():
    r = requests.get(f"{BASE_URL}/api/costs/landing?days=7", headers=H, timeout=60)
    assert r.status_code == 200, r.text
    lat = r.json().get("latency")
    print("latency", lat)
    assert isinstance(lat, dict)
    for k in ("n", "signal_to_decision_ms", "decision_to_sent_ms", "sent_to_landed_ms",
              "decision_to_landed_ms", "paper_decision_to_fill_ms"):
        assert k in lat, k
    if lat["n"] and lat["paper_decision_to_fill_ms"] is not None:
        assert isinstance(lat["paper_decision_to_fill_ms"], int) and lat["paper_decision_to_fill_ms"] >= 0


# --- active trades net_if_sold
def test_active_net_if_sold():
    r = requests.get(f"{BASE_URL}/api/trades/active", headers=H, timeout=30)
    assert r.status_code == 200
    rows = r.json()
    print("active rows", len(rows))
    for d in rows:
        n = d.get("net_if_sold")
        print(d["mint"][:8], d.get("book"), d.get("unrealized_pnl_pct"), n)
        if n:
            assert n["net_usd"] < n["gross_usd"]


# --- history exit fees + latency stamps
def test_history_exit_fee_and_latency():
    r = requests.get(f"{BASE_URL}/api/trades/history?limit=60", headers=H, timeout=30)
    assert r.status_code == 200
    rows = r.json()
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    t2130, t2150 = today + 21.5 * 3600, today + (21 * 60 + 50) * 60
    bad = []
    for d in rows:
        if d.get("mode") != "paper" or d.get("chain") == "rh":
            continue
        ex, en = _ts(d.get("exit_time")), _ts(d.get("entry_time"))
        if not ex or ex < t2130:
            continue
        lat = d.get("latency")
        print(d["mint"][:8], d.get("book"), "entry", d.get("entry_time"), "exit", d.get("exit_time"),
              "exit_fee_sol", d.get("exit_fee_sol"), "latency", lat)
        if not (d.get("exit_fee_sol") or 0) > 0:
            bad.append(("exit_fee", d["mint"][:8]))
        if en and en >= t2150:
            if not (isinstance(lat, dict) and lat.get("t_decision") and (lat.get("decision_to_landed_ms") or -1) >= 0):
                bad.append(("latency", d["mint"][:8]))
    assert not bad, bad
