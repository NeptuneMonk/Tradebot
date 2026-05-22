"""
v3 backend tests for the Pump.fun Micro-Stake Trading Bot.
Covers:
  1. WebSocket push (/api/ws) — initial status snapshot, periodic status+wallet
     events, launch / launch_update events as they flow in.
  2. Creator history endpoint GET /api/creators/{addr} contract.
  3. Each launch in /api/launches/recent now exposes creator_tokens_* fields.
  4. Creator counter increments across multiple launches.
  5. Helius backfill best-effort (backfill_ok true OR graceful false).
  6. WebSocket does not block regular HTTP (concurrent serve).
  7. WebSocket clean disconnect handling.
"""
import os
import json
import time
import asyncio
import pytest
import requests
import websockets


BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
API = f"{BASE_URL}/api"
# Derive ws scheme/host from REACT_APP_BACKEND_URL
_WS_BASE = BASE_URL.replace("https://", "wss://").replace("http://", "ws://")
WS_URL = f"{_WS_BASE}/api/ws"


@pytest.fixture(scope="module")
def client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


# ---------------- WebSocket ----------------
class TestWebSocket:
    def _collect_events(self, duration_s: float = 12.0, max_events: int = 50):
        """Helper: connect, collect events for `duration_s`, return list."""
        async def _run():
            events = []
            async with websockets.connect(WS_URL, open_timeout=10) as ws:
                deadline = time.time() + duration_s
                while time.time() < deadline and len(events) < max_events:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        break
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    except asyncio.TimeoutError:
                        break
                    try:
                        events.append(json.loads(msg))
                    except Exception:
                        events.append({"type": "raw", "data": msg})
            return events
        return asyncio.run(_run())

    def test_ws_connect_and_initial_status(self):
        """On connect, server must immediately push a 'status' event."""
        async def _run():
            async with websockets.connect(WS_URL, open_timeout=10) as ws:
                msg = await asyncio.wait_for(ws.recv(), timeout=10)
                return json.loads(msg)
        first = asyncio.run(_run())
        assert isinstance(first, dict)
        assert first.get("type") == "status", f"first event was {first.get('type')}: {first}"
        data = first.get("data") or {}
        # Validate at least a few expected status fields
        for k in ("enabled", "listener_connected", "kill_switch_tripped"):
            assert k in data, f"status payload missing {k}: {data}"

    def test_ws_periodic_status_and_wallet(self):
        """Within ~10s we should see at least 1 more status AND 1 wallet event."""
        events = self._collect_events(duration_s=12.0)
        types = [e.get("type") for e in events]
        status_count = types.count("status")
        wallet_count = types.count("wallet")
        # Initial status + at least 1 periodic = >=2; periodic broadcaster runs every 3s
        assert status_count >= 2, f"expected >=2 status events, got types={types}"
        assert wallet_count >= 1, f"expected >=1 wallet event, got types={types}"
        # validate wallet payload structure on one of them
        wallet_evts = [e for e in events if e.get("type") == "wallet"]
        wd = wallet_evts[0].get("data") or {}
        for k in ("public_key", "sol_balance", "usd_balance", "sol_price_usd"):
            assert k in wd, f"wallet event missing {k}: {wd}"

    def test_ws_does_not_block_http(self, client):
        """While a WS client is connected, regular HTTP must still serve."""
        async def _run():
            async with websockets.connect(WS_URL, open_timeout=10) as ws:
                # Wait for the initial frame to confirm connection
                await asyncio.wait_for(ws.recv(), timeout=10)
                # Now hit HTTP API multiple times while WS is open
                results = []
                for _ in range(3):
                    r = client.get(f"{API}/bot/status", timeout=10)
                    results.append(r.status_code)
                    await asyncio.sleep(0.2)
                return results
        codes = asyncio.run(_run())
        assert all(c == 200 for c in codes), f"HTTP blocked by WS: {codes}"

    def test_ws_disconnect_cleanup_no_crash(self, client):
        """Open & close 3 WS connections back-to-back; server should not crash —
        verified by HTTP still responding 200 afterwards."""
        async def _run():
            for _ in range(3):
                async with websockets.connect(WS_URL, open_timeout=10) as ws:
                    try:
                        await asyncio.wait_for(ws.recv(), timeout=5)
                    except asyncio.TimeoutError:
                        pass
                # context exits -> close
                await asyncio.sleep(0.3)
        asyncio.run(_run())
        # Verify server still healthy
        r = client.get(f"{API}/bot/status", timeout=10)
        assert r.status_code == 200, f"server unhealthy after WS churn: {r.status_code} {r.text}"

    def test_ws_launch_events_optional(self):
        """Pump.fun launches are unpredictable; we accept zero in a short
        window. But if any 'launch' / 'launch_update' arrives, its shape must
        be correct."""
        events = self._collect_events(duration_s=20.0, max_events=200)
        launches = [e for e in events if e.get("type") in ("launch", "launch_update")]
        if not launches:
            pytest.skip("No launch events arrived in 20s window (acceptable)")
        sample = launches[0]
        d = sample.get("data") or {}
        if sample["type"] == "launch":
            for k in ("mint", "creator", "bonding_curve"):
                assert k in d, f"launch event missing {k}: {d}"
        else:  # launch_update
            for k in ("mint",):
                assert k in d, f"launch_update missing {k}: {d}"


# ---------------- Creator history endpoint ----------------
class TestCreatorEndpoint:
    REQUIRED_FIELDS = (
        "tokens_created", "tokens_graduated", "tokens_failed", "tokens_active",
        "recent_mints",
    )

    def _wait_for_launches(self, client, min_count=1, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = client.get(f"{API}/launches/recent", params={"limit": 50})
            if r.status_code == 200:
                arr = r.json()
                if len(arr) >= min_count:
                    return arr
            time.sleep(3)
        return []

    def test_creator_unknown_returns_default(self, client):
        """Unknown creator should return zeroed defaults, not 500/404."""
        fake = "1nva1idCreatorAddre55XXXXXXXXXXXXXXXXXXXXXX"
        r = client.get(f"{API}/creators/{fake}")
        assert r.status_code == 200, r.text
        d = r.json()
        for k in self.REQUIRED_FIELDS:
            assert k in d, f"missing {k}: {d}"
        assert d["tokens_created"] == 0
        assert d["tokens_failed"] == 0
        assert d["tokens_graduated"] == 0
        assert d["tokens_active"] == 0

    def test_creator_from_recent_launch_full_contract(self, client):
        launches = self._wait_for_launches(client, min_count=1, timeout=90)
        assert launches, "No launches available within 90s"
        creator = launches[0]["creator"]
        r = client.get(f"{API}/creators/{creator}")
        assert r.status_code == 200, r.text
        d = r.json()
        # Required contract fields
        contract = (
            "tokens_created", "tokens_graduated", "tokens_failed", "tokens_active",
            "recent_mints", "first_seen", "last_seen",
            "backfill_attempted",
        )
        for k in contract:
            assert k in d, f"creator doc missing {k}: keys={list(d.keys())}"
        assert isinstance(d["tokens_created"], int) and d["tokens_created"] >= 1
        assert isinstance(d["recent_mints"], list)
        assert launches[0]["mint"] in d["recent_mints"]
        # If backfill succeeded, prior_* numeric fields exist
        if d.get("backfill_ok"):
            for k in ("prior_pump_txs", "prior_distinct_mints", "prior_creates_estimate"):
                assert k in d, f"backfill_ok=true but missing {k}"
                assert isinstance(d[k], int)

    def test_some_creator_has_helius_backfill_attempted(self, client):
        """At least one creator from /launches/recent should show
        backfill_attempted=true. backfill_ok may be False due to 429 — accept that."""
        launches = self._wait_for_launches(client, min_count=5, timeout=90)
        assert launches, "No launches available"
        attempted_count = 0
        ok_count = 0
        sampled = launches[:10]
        for L in sampled:
            r = client.get(f"{API}/creators/{L['creator']}")
            if r.status_code != 200:
                continue
            d = r.json()
            if d.get("backfill_attempted"):
                attempted_count += 1
            if d.get("backfill_ok"):
                ok_count += 1
        assert attempted_count >= 1, (
            f"No creator had backfill_attempted=true (Helius key may be missing). "
            f"sampled={len(sampled)}"
        )
        # ok_count >= 0 is fine (Helius can 429); just log
        print(f"backfill_attempted={attempted_count}/{len(sampled)} ok={ok_count}")


# ---------------- Launch fields contract ----------------
class TestLaunchCreatorFields:
    def test_launches_include_creator_counts(self, client):
        deadline = time.time() + 60
        arr = []
        while time.time() < deadline:
            r = client.get(f"{API}/launches/recent", params={"limit": 30})
            assert r.status_code == 200
            arr = r.json()
            if arr:
                break
            time.sleep(3)
        assert arr, "No launches in 60s"
        required = ("creator_tokens_created", "creator_tokens_failed", "creator_tokens_graduated")
        for L in arr:
            for k in required:
                assert k in L, f"launch missing {k}: keys={list(L.keys())}"
                assert isinstance(L[k], int), f"{k} wrong type: {type(L[k]).__name__}"
            assert L["creator_tokens_created"] >= 1, (
                f"creator_tokens_created should be >=1 on its own launch: {L}"
            )


# ---------------- Creator history growth ----------------
class TestCreatorHistoryGrowth:
    def test_repeat_creator_increments_counter(self, client):
        """Find a creator that has appeared in multiple recent launches; their
        tokens_created from /api/creators/{addr} should equal at least the
        count of distinct mints we observed for them in the feed.

        If no creator has launched >1 token in the recent feed (Pump.fun
        launches are dominated by single-shot creators), we skip rather
        than fail."""
        r = client.get(f"{API}/launches/recent", params={"limit": 100})
        assert r.status_code == 200
        arr = r.json()
        from collections import defaultdict
        by_creator = defaultdict(set)
        for L in arr:
            by_creator[L["creator"]].add(L["mint"])
        repeats = {c: mints for c, mints in by_creator.items() if len(mints) >= 2}
        if not repeats:
            pytest.skip("No creator launched >1 token in the recent window")
        # Pick the one with most mints
        creator, mints = max(repeats.items(), key=lambda kv: len(kv[1]))
        r = client.get(f"{API}/creators/{creator}")
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["tokens_created"] >= len(mints), (
            f"creator {creator} has tokens_created={d['tokens_created']} but "
            f"recent feed shows {len(mints)} distinct mints"
        )
        # recent_mints should include at least some of them
        overlap = set(d.get("recent_mints", [])) & mints
        assert overlap, f"recent_mints {d.get('recent_mints')} has no overlap with {mints}"


# ---------------- Regression smoke (28 prior tests covered separately, here a fast smoke) ----------------
class TestRegressionSmoke:
    def test_wallet(self, client):
        r = client.get(f"{API}/wallet"); assert r.status_code == 200
        assert "public_key" in r.json()

    def test_bot_status(self, client):
        r = client.get(f"{API}/bot/status"); assert r.status_code == 200
        assert "listener_connected" in r.json()

    def test_bot_config_safety_caps(self, client):
        payload = {
            "enabled": False, "live_trading": False,
            "min_trade_usd": 0.05, "max_trade_usd": 10.0,
            "slippage_bps": 10, "daily_kill_switch_usd": 500.0,
            "priority_fee_microlamports": 500000,
            "hold_max_seconds": 30, "take_profit_pct": 25.0, "stop_loss_pct": 30.0,
        }
        r = client.put(f"{API}/bot/config", json=payload)
        assert r.status_code == 200
        d = r.json()
        assert d["max_trade_usd"] == 5.0
        assert d["min_trade_usd"] == 0.10
        assert d["slippage_bps"] == 50
        assert d["daily_kill_switch_usd"] == 100

    def test_bot_start_stop_reset(self, client):
        assert client.post(f"{API}/bot/reset-kill-switch").status_code == 200
        assert client.post(f"{API}/bot/start").status_code == 200
        assert client.post(f"{API}/bot/stop").status_code == 200

    def test_classifier_rules_includes_social_score_min(self, client):
        r = client.get(f"{API}/classifier/rules"); assert r.status_code == 200
        assert "social_score_min" in r.json()

    def test_launches_recent(self, client):
        r = client.get(f"{API}/launches/recent"); assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_trades_active(self, client):
        r = client.get(f"{API}/trades/active"); assert r.status_code == 200

    def test_trades_history(self, client):
        r = client.get(f"{API}/trades/history"); assert r.status_code == 200

    def test_pl_summary(self, client):
        r = client.get(f"{API}/pl/summary"); assert r.status_code == 200
        d = r.json(); assert "series" in d and "daily_pnl_usd" in d and "cumulative_usd" in d
