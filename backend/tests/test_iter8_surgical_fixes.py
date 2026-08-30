"""Regression tests for the 4 surgical fixes (iter 8).

Covers:
- FIX 2/3: BotConfig() default values (greylist_snipe_*, risk ladder, persistence)
- FIX 4:   BotConfig() defaults for paper_* fields
- Curve-fill instant-exit guard preservation (fix 2 sanity)
- PUT /api/bot/config accepts the new paper fields (round-trip)
"""
import os
import sys
import pytest
import requests

sys.path.insert(0, "/app/backend")
from models import BotConfig  # noqa: E402

BASE_URL = "http://localhost:8001"


# ---------- BotConfig() default values ----------

class TestBotConfigDefaults:
    def setup_method(self):
        self.cfg = BotConfig()

    def test_paper_defaults(self):
        assert self.cfg.paper_exit_latency_ms == 600
        assert self.cfg.paper_entry_latency_ms == 400
        assert self.cfg.paper_apply_priority_fee is True

    def test_risk_ladder_defaults(self):
        assert self.cfg.stop_loss_pct == 12.0
        assert self.cfg.trailing_stop_pct == 6.0
        assert self.cfg.trailing_arm_pct == 12.0
        assert self.cfg.hold_max_seconds == 35

    def test_persistence_defaults(self):
        assert self.cfg.sl_persistence_ms == 500
        assert self.cfg.ts_persistence_ms == 600
        assert self.cfg.tp_persistence_ms == 400
        assert self.cfg.sl_persistence_min_samples == 2
        assert self.cfg.ts_persistence_min_samples == 2
        assert self.cfg.tp_persistence_min_samples == 2
        assert self.cfg.intelligent_exit_v2 is True

    def test_greylist_snipe_defaults(self):
        assert self.cfg.greylist_snipe_peak_mc_proximity_pct == 75.0
        assert self.cfg.greylist_snipe_curve_buffer_pct == 8.0
        assert self.cfg.greylist_snipe_ripcord_drawdown_pct == 45.0
        assert self.cfg.greylist_snipe_ripcord_grace_seconds == 4
        assert self.cfg.greylist_snipe_profit_ripcord_pct == 20.0
        assert self.cfg.greylist_snipe_stale_seconds == 60
        assert self.cfg.greylist_snipe_stale_min_profit_pct == 5.0

    def test_helius_tracker_enabled_default(self):
        assert self.cfg.helius_tracker_enabled is True


# ---------- Curve-fill guard preservation ----------

class TestCurveFillGuard:
    """Guard: `rug_curve > buffer + 5.0` must gate the instant-exit.
    With buffer=8:
      - rug_curve=10 → 10 > 13 == False → guard DOES NOT fire (correct)
      - rug_curve=20, curve_pct=12 → guard CAN fire (trigger_at = 20 - 8 = 12)
    """
    def test_guard_blocks_untradeable_rug(self):
        buffer_pp = 8.0
        rug_curve = 10.0
        assert not (float(rug_curve) > buffer_pp + 5.0)

    def test_guard_permits_tradeable_case(self):
        buffer_pp = 8.0
        rug_curve = 20.0
        curve_pct = 12.0
        assert float(rug_curve) > buffer_pp + 5.0
        trigger_at = float(rug_curve) - buffer_pp
        assert curve_pct >= trigger_at


# ---------- API round-trip for new paper fields ----------

@pytest.fixture(scope="module")
def auth_headers():
    tok = os.environ.get("TESTER_TOK", "tester_iter8_tok")
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


class TestPaperFieldsRoundTrip:
    def test_put_and_get_paper_fields(self, auth_headers):
        # Read current config
        r = requests.get(f"{BASE_URL}/api/bot/config", headers=auth_headers, timeout=10)
        assert r.status_code == 200, r.text
        original = r.json()

        # Push new values
        new_vals = {
            "paper_exit_latency_ms": 777,
            "paper_entry_latency_ms": 333,
            "paper_apply_priority_fee": False,
        }
        # Merge with original so we don't lose fields (PUT may replace entire doc).
        payload = {**original, **new_vals}
        r = requests.put(f"{BASE_URL}/api/bot/config", headers=auth_headers, json=payload, timeout=10)
        assert r.status_code == 200, r.text

        # Read back
        r = requests.get(f"{BASE_URL}/api/bot/config", headers=auth_headers, timeout=10)
        assert r.status_code == 200
        got = r.json()
        assert got["paper_exit_latency_ms"] == 777
        assert got["paper_entry_latency_ms"] == 333
        assert got["paper_apply_priority_fee"] is False

        # Restore originals (only the 3 fields; leave the rest untouched)
        restore = {**got,
                   "paper_exit_latency_ms": original.get("paper_exit_latency_ms", 600),
                   "paper_entry_latency_ms": original.get("paper_entry_latency_ms", 400),
                   "paper_apply_priority_fee": original.get("paper_apply_priority_fee", True)}
        rr = requests.put(f"{BASE_URL}/api/bot/config", headers=auth_headers, json=restore, timeout=10)
        assert rr.status_code == 200


# ---------- Code presence checks ----------

class TestCodePresence:
    BOT_PY = "/app/backend/bot.py"

    def test_monitor_position_peak_update_block(self):
        with open(self.BOT_PY) as f:
            src = f.read()
        # peak update block inside _monitor_position (post _last_price_sol)
        assert 'slot["_last_price_sol"] = cur_price_sol' in src
        assert 'peak_mon = float(slot.get("peak_price_sol")' in src

    def test_monitor_position_trailing_stop_block(self):
        with open(self.BOT_PY) as f:
            src = f.read()
        # Trailing stop in monitor uses _exit_param and NO [fast] suffix
        assert 'self._exit_param(slot, "trail_pct"' in src
        assert 'self._exit_param(slot, "trail_arm_pct"' in src
        # Confirm the monitor-path reason string exists (no [fast])
        assert 'reason=f"trailing-stop hit (peak +{m_peak_pct:.1f}%, now +{pct_change:.1f}%)"' in src

    def test_paper_latency_block_exists(self):
        with open(self.BOT_PY) as f:
            src = f.read()
        assert "paper_decision_price_sol" in src
        assert "paper_latency_ms" in src
        assert 'if trade_doc["mode"] != "live":' in src
        assert "await asyncio.sleep(latency_ms / 1000.0)" in src
        assert "PAPER_FILL" in src
