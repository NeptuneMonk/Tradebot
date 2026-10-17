# Iteration 41: EMERGENT_CHANGES safety batch (new BotConfig keys, live-arm refusal, clamps, import report, net_if_sold)
# Lives OUTSIDE backend/ so pytest edits don't trigger uvicorn --reload (which auto-disables the bot).
import os
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    for line in open("/app/frontend/.env"):
        if line.startswith("REACT_APP_BACKEND_URL="):
            BASE_URL = line.split("=", 1)[1].strip().rstrip("/")
TOKEN = open("/app/memory/.tok").read().strip()
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

NEW_KEYS = {"buy_fee_ladder_rungs": 1, "entry_balance_cache_s": 10, "fast_fail_add_at_pct": 5.0,
            "fast_fail_split_enabled": False, "land_late_watch_s": 45, "rh_protocol_fee_pct": 1.0,
            "rh_sell_fallback_slippage_pct": 20.0, "socials_fetch_blocking": False}


def get_cfg():
    r = requests.get(f"{BASE_URL}/api/bot/config", headers=H, timeout=30)
    assert r.status_code == 200, r.text
    return r.json()


def put_cfg(body):
    return requests.put(f"{BASE_URL}/api/bot/config", headers=H, json=body, timeout=30)


# --- new config keys
def test_new_keys_present():
    c = get_cfg()
    for k, v in NEW_KEYS.items():
        assert k in c, k
        assert c[k] == v, f"{k}={c[k]} expected {v}"
    assert c["live_trading"] is False
    assert c["rh_live_trading"] is False


# --- live-arm refusal (S)
def test_solana_live_arm_refused():
    before = get_cfg()
    r = put_cfg({"live_trading": True})
    assert r.status_code == 409, r.text
    assert "SOLANA_PRIVATE_KEY" in r.json().get("detail", "")
    after = get_cfg()
    assert after["live_trading"] is False
    diff = {k for k in before if before[k] != after.get(k)}
    assert not diff, f"config changed after refused arm: {diff}"


def test_rh_live_arm_refused():
    before = get_cfg()
    r = put_cfg({"rh_live_trading": True})
    assert r.status_code == 409, r.text
    assert "RH_WALLET_PRIVATE_KEY" in r.json().get("detail", "")
    after = get_cfg()
    assert after["rh_live_trading"] is False
    diff = {k for k in before if before[k] != after.get(k)}
    assert not diff, f"config changed after refused arm: {diff}"


# --- clamps (#9/#10/#11) + restore
def test_clamps_and_restore():
    try:
        r = put_cfg({"rh_protocol_fee_pct": 9, "entry_balance_cache_s": 500,
                     "rh_sell_fallback_slippage_pct": 0.1, "fast_fail_add_at_pct": 0.01})
        assert r.status_code == 200, r.text
        d = r.json()
        d = d.get("config", d)
        exp = {"rh_protocol_fee_pct": 5.0, "entry_balance_cache_s": 60,
               "rh_sell_fallback_slippage_pct": 1.0, "fast_fail_add_at_pct": 0.5}
        for k, v in exp.items():
            assert d[k] == v, f"response {k}={d[k]} expected {v}"
        g = get_cfg()
        for k, v in exp.items():
            assert g[k] == v, f"GET {k}={g[k]} expected {v}"
    finally:
        r = put_cfg({"rh_protocol_fee_pct": 1.0, "entry_balance_cache_s": 10,
                     "rh_sell_fallback_slippage_pct": 20.0, "fast_fail_add_at_pct": 5.0})
        assert r.status_code == 200
    g = get_cfg()
    for k in ("rh_protocol_fee_pct", "entry_balance_cache_s", "rh_sell_fallback_slippage_pct", "fast_fail_add_at_pct"):
        assert g[k] == NEW_KEYS[k]


# --- config import report (#12) + restart bot
def test_config_import_report():
    cfg = get_cfg()
    payload = dict(cfg)
    payload["zzz_unknown_key"] = 1
    payload["rh_protocol_fee_pct"] = 9
    try:
        r = requests.post(f"{BASE_URL}/api/config/import", headers=H, json={"config": payload}, timeout=60)
        assert r.status_code == 200, r.text
        rep = r.json()["report"]
        print("REPORT:", {k: v for k, v in rep.items() if k != "applied"})
        assert "zzz_unknown_key" in rep["ignored_unknown"]
        assert "rh_protocol_fee_pct" in rep["clamped"]
        assert r.json()["config"]["rh_protocol_fee_pct"] == 5.0
    finally:
        put_cfg({"rh_protocol_fee_pct": 1.0})
        s = requests.post(f"{BASE_URL}/api/bot/start", headers=H, timeout=60)
        print("start:", s.status_code, s.text[:200])
    assert get_cfg()["rh_protocol_fee_pct"] == 1.0
    st = requests.get(f"{BASE_URL}/api/bot/status", headers=H, timeout=30).json()
    assert st["enabled"] is True


# --- active trades net_if_sold
def test_active_trades_net_if_sold():
    r = requests.get(f"{BASE_URL}/api/trades/active", headers=H, timeout=30)
    assert r.status_code == 200, r.text
    rows = r.json()
    assert isinstance(rows, list)
    print("active rows:", len(rows))
    for row in rows:
        if row.get("current_price_sol") is None and row.get("unrealized_pnl_pct") is None:
            continue
        n = row.get("net_if_sold")
        assert n, f"missing net_if_sold on {row.get('mint')}"
        for k in ("gross_usd", "net_usd", "net_pnl_usd", "net_pnl_pct", "costs_usd", "slip_pct",
                  "protocol_pct", "exit_slip_cap_bps", "depth_sol"):
            assert k in n, k
        for k in ("slip", "protocol", "shave", "tx_fee", "entry_fee"):
            assert k in n["costs_usd"], k
        assert n["net_usd"] < n["gross_usd"]
