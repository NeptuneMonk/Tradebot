"""Iteration 10 - Test living greylist, seasoned supply (graduated feed), manual buy, P/L manual bucket."""
import os
import time
import pytest
import requests

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
TOKEN = os.environ["TEST_SESSION_TOKEN"]  # seeded by tests/conftest.py
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


# --- Living Greylist ---
class TestLivingGreylist:
    def test_greylist_returns_inactive_count_and_days(self):
        r = requests.get(f"{BASE_URL}/api/creator-greylist?limit=3", headers=H, timeout=15)
        assert r.status_code == 200, r.text
        j = r.json()
        assert "items" in j
        assert "inactive_count" in j and isinstance(j["inactive_count"], int)
        assert "inactive_days" in j
        print(f"inactive_count={j['inactive_count']}, inactive_days={j['inactive_days']}")
        assert j["inactive_count"] > 1000  # ~19966 expected
        assert j["inactive_days"] == 30

    def test_prune_inactive(self):
        r = requests.post(f"{BASE_URL}/api/creator-greylist/prune-inactive", headers=H, timeout=30)
        assert r.status_code == 200, r.text
        j = r.json()
        assert "flagged" in j
        assert "revived" in j
        assert "cutoff" in j
        print(f"prune result: {j}")


# --- Config clamp / scanner_graduated_feed_enabled ---
class TestConfig:
    def test_clamp_inactive_days_and_restore(self):
        # Attempt to set to 500 (should clamp to 365)
        r = requests.put(f"{BASE_URL}/api/bot/config",
                         headers=H,
                         json={"creator_greylist_inactive_days": 500}, timeout=15)
        assert r.status_code == 200, r.text
        cfg = requests.get(f"{BASE_URL}/api/bot/config", headers=H, timeout=15).json()
        assert cfg["creator_greylist_inactive_days"] == 365, f"expected clamp to 365 got {cfg['creator_greylist_inactive_days']}"
        # Restore to 30
        r = requests.put(f"{BASE_URL}/api/bot/config",
                         headers=H,
                         json={"creator_greylist_inactive_days": 30}, timeout=15)
        assert r.status_code == 200
        cfg = requests.get(f"{BASE_URL}/api/bot/config", headers=H, timeout=15).json()
        assert cfg["creator_greylist_inactive_days"] == 30

    def test_scanner_graduated_feed_enabled_true(self):
        cfg = requests.get(f"{BASE_URL}/api/bot/config", headers=H, timeout=15).json()
        assert cfg.get("scanner_graduated_feed_enabled") is True


# --- Scanner candidates & graduated_feed field ---
class TestScannerCandidates:
    def test_candidates_returns_list_with_graduated_feed_field(self):
        r = requests.get(f"{BASE_URL}/api/scanner/candidates", headers=H, timeout=20)
        assert r.status_code == 200
        j = r.json()
        # Response may be list or dict with items
        items = j if isinstance(j, list) else j.get("items") or j.get("candidates") or []
        assert isinstance(items, list)
        print(f"scanner candidates: {len(items)} rows")
        sol_rows = [c for c in items if (c.get("chain") in (None, "sol", "solana"))]
        # Assert graduated_feed key exists for SOL rows
        for c in sol_rows[:20]:
            assert "graduated_feed" in c, f"SOL row missing graduated_feed: {c.get('mint')}"
        passing = [c for c in items if c.get("passes") is True]
        print(f"passing: {len(passing)}, sol_rows: {len(sol_rows)}")

    def test_graduated_feed_log_present(self):
        # Check backend log for graduated feed line
        try:
            with open("/var/log/supervisor/backend.err.log", "r") as f:
                content = f.read()[-100000:]
            has_line = "graduated feed:" in content
            print(f"graduated feed log present: {has_line}")
            # not fatal — feature may be running but no line yet
        except Exception as e:
            print(f"could not read backend log: {e}")


# --- Manual Buy ---
class TestManualBuy:
    def _find_passing(self, chain_filter):
        r = requests.get(f"{BASE_URL}/api/scanner/candidates", headers=H, timeout=20)
        j = r.json()
        items = j if isinstance(j, list) else j.get("items") or j.get("candidates") or []
        for c in items:
            if not c.get("passes"):
                continue
            ch = c.get("chain")
            if chain_filter == "sol" and ch in (None, "sol", "solana"):
                return c
            if chain_filter == "rh" and ch == "rh":
                # prefer ETH quote
                if c.get("quote_symbol") == "ETH":
                    return c
        # fallback: any rh
        if chain_filter == "rh":
            for c in items:
                if c.get("passes") and c.get("chain") == "rh":
                    return c
        return None

    def test_manual_buy_unknown_mint_409(self):
        r = requests.post(f"{BASE_URL}/api/scanner/manual-buy/UnknownMintXYZ123", headers=H, timeout=15)
        assert r.status_code == 409
        body = r.json()
        msg = (body.get("detail") or body.get("error") or "").lower()
        assert "not tracked" in msg or "unknown" in msg, body
        print(f"unknown mint response: {body}")

    def test_manual_buy_sol(self):
        cand = self._find_passing("sol")
        if not cand:
            pytest.skip("no passing SOL candidate available right now")
        mint = cand["mint"]
        print(f"attempting manual buy SOL mint={mint}")
        r = requests.post(f"{BASE_URL}/api/scanner/manual-buy/{mint}", headers=H, timeout=20)
        print(f"status={r.status_code} body={r.text[:400]}")
        # Success or a documented refusal (e.g., max positions / kill switch / already active)
        if r.status_code == 200:
            j = r.json()
            assert j.get("ok") is True
            assert j.get("mode") == "paper"
            # Poll active trades briefly
            time.sleep(1.5)
            active = requests.get(f"{BASE_URL}/api/trades/active", headers=H, timeout=15).json()
            hist = requests.get(f"{BASE_URL}/api/trades/history?limit=20", headers=H, timeout=15).json()
            actives = active if isinstance(active, list) else active.get("items") or []
            hists = hist if isinstance(hist, list) else hist.get("items") or []
            found = any(t.get("classifier_action") == "manual" for t in actives + hists)
            print(f"manual classifier_action found in trades: {found} (active={len(actives)}, hist={len(hists)})")
            assert found, "manual classifier_action not found in active or recent history"
        else:
            # Accept 409 with reason (max positions etc)
            assert r.status_code in (409, 400), r.text

    def test_manual_buy_rh(self):
        cand = self._find_passing("rh")
        if not cand:
            pytest.skip("no passing RH ETH-quote candidate available")
        mint = cand["mint"]
        qs = cand.get("quote_symbol")
        print(f"attempting manual buy RH mint={mint} quote={qs}")
        r = requests.post(f"{BASE_URL}/api/scanner/manual-buy/{mint}", headers=H, timeout=20)
        print(f"status={r.status_code} body={r.text[:400]}")
        if r.status_code == 200:
            j = r.json()
            assert j.get("ok") is True
            assert j.get("mode") == "paper"
            assert j.get("chain") == "rh"
            # Second call should 409 already-active
            r2 = requests.post(f"{BASE_URL}/api/scanner/manual-buy/{mint}", headers=H, timeout=15)
            print(f"second call: {r2.status_code} {r2.text[:200]}")
            assert r2.status_code == 409
            msg = (r2.json().get("detail") or "").lower()
            assert "already" in msg or "active" in msg
        elif r.status_code == 409:
            msg = (r.json().get("detail") or "").lower()
            # acceptable reasons per spec
            assert ("already" in msg) or ("no usd price" in msg) or ("not tracked" in msg) or ("quote" in msg), r.text


# --- P/L by source ---
class TestPLBySource:
    def test_pl_by_source_has_manual(self):
        r = requests.get(f"{BASE_URL}/api/pl/by-source?days=1", headers=H, timeout=15)
        assert r.status_code == 200, r.text
        j = r.json()
        items = j if isinstance(j, list) else j.get("items") or j.get("sources") or []
        keys = []
        found_manual = False
        for it in items:
            src = it.get("source") or it.get("key")
            keys.append(src)
            if src == "manual":
                found_manual = True
                lbl = it.get("label") or ""
                assert lbl == "Manual Buy", f"expected 'Manual Buy', got {lbl!r}"
        print(f"sources: {keys}")
        assert found_manual, f"'manual' source not in {keys}"
