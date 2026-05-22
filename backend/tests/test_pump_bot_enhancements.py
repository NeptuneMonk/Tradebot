"""
Regression tests for the v2 enhancements:
  1. Mempool-level metric collection per launch (unique_buyers, sol_inflow,
     buy_count, curve_fill_pct, entered).
  2. Social trending score (social_score, social_sources).
  3. ClassifierRules.social_score_min field.
"""
import os
import time
import pytest
import requests

BASE_URL = os.environ.get(
    "REACT_APP_BACKEND_URL",
    "https://micro-stake-trader.preview.emergentagent.com",
).rstrip("/")
API = f"{BASE_URL}/api"


@pytest.fixture(scope="module")
def client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


# ----- New launch fields contract -----
class TestLaunchFieldsContract:
    def test_recent_launch_has_new_fields(self, client):
        """Each launch returned by /launches/recent must include the new fields
        with correct types."""
        deadline = time.time() + 60
        launches: list = []
        while time.time() < deadline:
            r = client.get(f"{API}/launches/recent", params={"limit": 30})
            assert r.status_code == 200, r.text
            launches = r.json()
            if launches:
                break
            time.sleep(3)
        assert launches, "No launches detected within 60s window"

        required_field_types = {
            "unique_buyers": int,
            "sol_inflow": (int, float),
            "buy_count": int,
            "curve_fill_pct": (int, float),
            "social_score": int,
            "social_sources": dict,
            "entered": bool,
        }
        for l in launches:
            for key, typ in required_field_types.items():
                assert key in l, f"launch missing field '{key}': {l}"
                assert isinstance(l[key], typ), (
                    f"launch field '{key}' wrong type: got {type(l[key]).__name__}, "
                    f"expected {typ}"
                )


# ----- Social score min rule -----
class TestClassifierSocialScoreMinRule:
    def test_get_rules_includes_social_score_min(self, client):
        r = client.get(f"{API}/classifier/rules")
        assert r.status_code == 200
        d = r.json()
        assert "social_score_min" in d, "social_score_min missing from rules"
        assert isinstance(d["social_score_min"], int)
        # Default should be 0 (disabled)
        # Note: previous tests may have mutated; just assert range
        assert 0 <= d["social_score_min"] <= 100

    def test_put_rules_persists_social_score_min(self, client):
        # Persist non-zero then re-fetch
        # First fetch existing to merge
        cur = client.get(f"{API}/classifier/rules").json()
        cur["social_score_min"] = 25
        r = client.put(f"{API}/classifier/rules", json=cur)
        assert r.status_code == 200, r.text
        assert r.json()["social_score_min"] == 25

        r2 = client.get(f"{API}/classifier/rules")
        assert r2.status_code == 200
        assert r2.json()["social_score_min"] == 25

        # Reset to 0 (disabled) so we don't suppress entries for later tests
        cur["social_score_min"] = 0
        r3 = client.put(f"{API}/classifier/rules", json=cur)
        assert r3.status_code == 200
        assert r3.json()["social_score_min"] == 0


# ----- Live metric population (proves on_trade wiring) -----
class TestLiveMetricPopulation:
    def test_some_launch_has_nonzero_metrics(self, client):
        """After ~75s of waiting, at least one launch in the recent feed should
        have observed buys (unique_buyers > 0 OR sol_inflow > 0 OR buy_count > 0).
        This proves the on_trade handler is updating the tracker."""
        deadline = time.time() + 90
        found = False
        last_summary = ""
        while time.time() < deadline and not found:
            r = client.get(f"{API}/launches/recent", params={"limit": 50})
            if r.status_code != 200:
                time.sleep(3)
                continue
            arr = r.json()
            with_buys = [
                l for l in arr
                if (l.get("unique_buyers", 0) > 0)
                or (l.get("sol_inflow", 0) > 0)
                or (l.get("buy_count", 0) > 0)
            ]
            last_summary = (
                f"total={len(arr)} with_buys={len(with_buys)} "
                f"max_buyers={max([l.get('unique_buyers', 0) for l in arr], default=0)} "
                f"max_inflow={max([l.get('sol_inflow', 0) for l in arr], default=0):.3f} "
                f"max_buy_count={max([l.get('buy_count', 0) for l in arr], default=0)}"
            )
            if with_buys:
                found = True
                break
            time.sleep(5)
        assert found, (
            f"No launch showed any trade activity within 90s. "
            f"on_trade handler may not be wired. Last snapshot: {last_summary}"
        )


# ----- Social score availability -----
class TestSocialScoreAvailability:
    def test_some_launch_has_nonzero_social_score(self, client):
        """Within a couple minutes of observation, at least ONE launch should
        receive social_score > 0 (DDG abstract / wiki / coingecko match)."""
        deadline = time.time() + 120
        last_summary = ""
        best_score = 0
        scored_count = 0
        while time.time() < deadline:
            r = client.get(f"{API}/launches/recent", params={"limit": 50})
            if r.status_code != 200:
                time.sleep(3)
                continue
            arr = r.json()
            scores = [int(l.get("social_score", 0) or 0) for l in arr]
            best_score = max(scores, default=0)
            scored_count = sum(1 for s in scores if s > 0)
            last_summary = (
                f"total={len(arr)} scored>0={scored_count} best={best_score}"
            )
            if best_score > 0:
                break
            time.sleep(5)
        assert best_score > 0, (
            f"No launch received any social_score>0 within 120s. "
            f"Social pipeline may be degraded. {last_summary}"
        )

    def test_social_sources_dict_structure(self, client):
        """social_sources, when populated, contains expected keys."""
        r = client.get(f"{API}/launches/recent", params={"limit": 50})
        assert r.status_code == 200
        arr = r.json()
        # Find any launch with populated sources
        populated = [l for l in arr if l.get("social_sources")]
        if not populated:
            pytest.skip("No launches with populated social_sources yet")
        sample = populated[0]["social_sources"]
        expected_keys = {
            "ddg_abstract", "ddg_heading", "ddg_related",
            "wikipedia_exists", "coingecko_match",
        }
        assert expected_keys.issubset(set(sample.keys())), (
            f"social_sources missing expected keys. got={set(sample.keys())}"
        )


# ----- Existing endpoints still work (smoke) -----
class TestExistingEndpointsSmoke:
    def test_wallet(self, client):
        r = client.get(f"{API}/wallet")
        assert r.status_code == 200

    def test_bot_status_listener_connected(self, client):
        r = client.get(f"{API}/bot/status")
        assert r.status_code == 200
        assert r.json().get("listener_connected") is True

    def test_bot_config(self, client):
        r = client.get(f"{API}/bot/config")
        assert r.status_code == 200

    def test_trades_active(self, client):
        r = client.get(f"{API}/trades/active")
        assert r.status_code == 200 and isinstance(r.json(), list)

    def test_trades_history(self, client):
        r = client.get(f"{API}/trades/history")
        assert r.status_code == 200 and isinstance(r.json(), list)

    def test_pl_summary(self, client):
        r = client.get(f"{API}/pl/summary")
        assert r.status_code == 200
        d = r.json()
        assert "series" in d and "daily_pnl_usd" in d and "cumulative_usd" in d
