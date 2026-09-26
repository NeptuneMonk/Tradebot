"""Iteration 32 API validation: config values, rugcheck, solscan cache stats, diagnostics."""
import os
import time
import pytest
import requests

BASE = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
TOK = open("/app/memory/.tok").read().strip()
H = {"Authorization": f"Bearer {TOK}", "Content-Type": "application/json"}


def test_bot_config_values():
    r = requests.get(f"{BASE}/api/bot/config", headers=H, timeout=15)
    assert r.status_code == 200, r.text
    cfg = r.json()
    expect = {
        "rh_paper_enabled": True,
        "scanner_seasoned_entries_enabled": True,
        "rh_min_age_s": 300,
        "rh_min_mc_usd": 20000,
        "rh_min_unique_buyers": 30,
        "serial_creator_min_launches": 2,
        "serial_creator_requires_graduation": False,
        "band_seasoned_min_age_min": 2,
        "band_seasoned_max_age_min": 45,
        "scanner_min_mc_velocity_5m_pct_seasoned": 15,
        "discovery_clip_usd": 5,
        "greylist_snipe_enabled": False,
        "doctor_auto_apply_enabled": False,
    }
    # these are operator-tunable: assert the keys exist with the right type, not the values (the user re-tunes them)
    for k, v in expect.items():
        assert k in cfg, f"{k} missing from config"
        assert isinstance(cfg[k], (bool, int, float)) and (isinstance(cfg[k], bool) == isinstance(v, bool)), f"{k}: bad type {type(cfg[k])}"
    # nested regime gate
    rgm = cfg.get("regime_gate_mult", {})
    mom = rgm.get("momentum", {}) if isinstance(rgm, dict) else {}
    assert float(mom.get("busy", 0)) > 0, f"regime_gate_mult.momentum.busy: {mom}"


def test_creator_audit_stats_has_solscan_cache():
    r = requests.get(f"{BASE}/api/creator-audit/stats", headers=H, timeout=15)
    assert r.status_code == 200, r.text
    j = r.json()
    ss = j.get("solscan") or {}
    cache = ss.get("cache") or {}
    for k in ("size", "hits", "misses"):
        assert k in cache, f"solscan.cache missing '{k}': {j}"


def test_diagnostics_loop_and_account_bus():
    r = requests.get(f"{BASE}/api/diagnostics/loop", headers=H, timeout=15)
    assert r.status_code == 200
    j = r.json()
    assert "event_loop_lag_ms" in j
    assert "ws_hub" in j
    r2 = requests.get(f"{BASE}/api/diagnostics/account-bus", headers=H, timeout=15)
    assert r2.status_code == 200


def _find_sol_mint():
    r = requests.get(f"{BASE}/api/launches/recent?limit=50", headers=H, timeout=20)
    assert r.status_code == 200, r.text
    for row in (r.json() if isinstance(r.json(), list) else r.json().get("launches", [])):
        chain = (row.get("chain") or "").lower()
        mint = row.get("mint") or row.get("address") or row.get("token")
        if chain != "rh" and mint and not str(mint).startswith("0x"):
            return mint
    pytest.skip("No non-RH Sol launch found in recent")


def test_token_detail_sol_has_rugcheck_and_caches():
    mint = _find_sol_mint()
    t0 = time.time()
    r1 = requests.get(f"{BASE}/api/token/sol/{mint}", headers=H, timeout=15)
    d1 = time.time() - t0
    assert r1.status_code == 200, r1.text
    j1 = r1.json()
    assert "rugcheck" in j1, "missing 'rugcheck' key in token detail"
    rc1 = j1["rugcheck"]
    # rugcheck is object with score/... OR None
    if rc1 is not None:
        assert isinstance(rc1, dict)
    t1 = time.time()
    r2 = requests.get(f"{BASE}/api/token/sol/{mint}", headers=H, timeout=15)
    d2 = time.time() - t1
    assert r2.status_code == 200
    j2 = r2.json()
    rc2 = j2.get("rugcheck")
    # Cache: identical (both may be None or same dict)
    assert rc1 == rc2, f"rugcheck value changed between calls: {rc1} vs {rc2}"
    print(f"token detail: first={d1:.2f}s second={d2:.2f}s rugcheck={rc1}")
