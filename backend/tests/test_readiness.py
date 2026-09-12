"""RH readiness: one answer for 'why is RH not trading' — every deployed-only failure mode has a named reason."""
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import readiness
from models import BotConfig


def _state(**cfg):
    base = dict(enabled=True, rh_feed_enabled=True, rh_paper_enabled=True, rh_live_trading=False)
    base.update(cfg)
    disc = SimpleNamespace(stats={"head": 0, "last_error": ""}, alive=lambda window_s=60.0: False, doctor_paused=lambda: None)
    return SimpleNamespace(config=BotConfig(**base), live_doctor=None, rh_paper=SimpleNamespace(live_kill_tripped=False, last_live_error=""),
                           rh_discovery=disc, process_started_ts=time.time())


def test_all_green_when_running_armed_and_fed():
    with patch.object(readiness.rh_discovery, "RH_RPC_URL", "https://rpc"):
        r = readiness.rh_readiness(_state())
    assert r["trading"] is True and r["reasons"] == [] and r["mode"] == "paper"


def test_stopped_after_restart_names_the_restart_and_the_flag():
    st = _state(enabled=False)
    st.auto_disabled_on_restart_at = "2026-06-01T00:00:00+00:00"
    with patch.object(readiness.rh_discovery, "RH_RPC_URL", "https://rpc"):
        r = readiness.rh_readiness(st)
    assert r["trading"] is False and r["checks"]["bot_enabled"] is False
    assert "auto-disabled after a backend restart" in r["reasons"][0] and "resume_on_restart" in r["reasons"][0]
    assert r["auto_disabled_on_restart_at"] == "2026-06-01T00:00:00+00:00"


def test_feed_off_not_armed_env_wallet_breaker_kill_each_get_a_reason(tmp_path):
    st = _state(rh_feed_enabled=False, rh_paper_enabled=False, rh_live_trading=True)
    st.live_doctor = SimpleNamespace(book_paused=lambda b: b == "rh_pons")
    st.rh_paper.live_kill_tripped = True
    with patch.object(readiness.rh_discovery, "RH_RPC_URL", ""), \
         patch.object(readiness.rh_wallet, "WALLET_PATH", tmp_path / "missing.json"), \
         patch.object(readiness.rh_wallet, "PASS_PATH", tmp_path / "missing.pass"):
        r = readiness.rh_readiness(st)
    joined = " | ".join(r["reasons"])
    for needle in ("RH feed OFF", "RH_RPC_URL is empty", "wallet files missing", "benched by the live-doctor breaker", "kill switch tripped"):
        assert needle in joined, needle
    assert r["mode"] == "live" and r["checks"]["rh_wallet_files"] is False and r["checks"]["rh_book_open"] is False


def test_dead_poller_reported_only_after_warmup():
    st = _state()
    with patch.object(readiness.rh_discovery, "RH_RPC_URL", "https://rpc"):
        assert readiness.rh_readiness(st)["trading"] is True          # fresh process: no verdict on the poller yet
        st.process_started_ts = time.time() - 300
        st.rh_discovery.stats["last_error"] = "429 rate limited"
        r = readiness.rh_readiness(st)
    assert r["trading"] is False and "RH poller not moving" in r["reasons"][0] and "429" in r["reasons"][0]
    st.rh_discovery.alive = lambda window_s=60.0: True
    with patch.object(readiness.rh_discovery, "RH_RPC_URL", "https://rpc"):
        assert readiness.rh_readiness(st)["trading"] is True


def test_fresh_wallet_on_this_boot_is_a_live_blocker_with_the_address():
    st = _state(rh_live_trading=True)
    with patch.object(readiness.rh_discovery, "RH_RPC_URL", "https://rpc"), \
         patch.object(readiness.rh_wallet, "CREATED_THIS_BOOT", True), \
         patch.object(readiness.rh_wallet, "address", lambda: "0xABCDEF0000000000000000000000000000001234"):
        r = readiness.rh_readiness(st)
    assert r["checks"]["rh_wallet_funded_key"] is False
    assert any("freshly generated on this boot (0xABCD…1234)" in x and "import" in x for x in r["reasons"])
    with patch.object(readiness.rh_discovery, "RH_RPC_URL", "https://rpc"), patch.object(readiness.rh_wallet, "CREATED_THIS_BOOT", False):
        assert readiness.rh_readiness(_state(rh_live_trading=True))["checks"]["rh_wallet_funded_key"] is True


def test_empty_rpc_url_sets_boot_error_on_discovery_stats():
    import rh_discovery
    disc = rh_discovery.RHDiscovery.__new__(rh_discovery.RHDiscovery)
    disc.stats = {}
    disc._task = None
    with patch.object(rh_discovery, "RH_RPC_URL", ""):
        disc.start()
    assert "RH_RPC_URL is empty" in disc.stats["boot_error"] and disc._task is None
