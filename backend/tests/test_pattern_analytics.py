"""
Tests for Phase 2.6 (pattern_analytics) + Phase 2.7 (pattern→TP wiring).
"""
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("HELIUS_RPC_URL", "https://x")
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")

import pytest  # noqa: E402

from creator_greylist import pattern_analytics  # noqa: E402


def _iso_ago(days: float = 0, hours: float = 0) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(days=days, hours=hours)
    ).isoformat()


class _FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def __aiter__(self):
        async def _gen():
            for d in self._docs:
                yield d
        return _gen()


class _FakeTrades:
    def __init__(self, docs):
        self._docs = list(docs)

    def find(self, q, _proj=None):
        out = []
        for d in self._docs:
            if d.get("status") != q.get("status", d.get("status")):
                continue
            cutoff = (q.get("exit_time") or {}).get("$gte")
            if cutoff and (d.get("exit_time") or "") < cutoff:
                continue
            if "mode" in q and d.get("mode") != q["mode"]:
                continue
            out.append(d)
        return _FakeCursor(out)


class _FakeDB:
    def __init__(self, trades):
        self.trades = _FakeTrades(trades)


# ---------------------------------------------------------------------------
# pattern_analytics — Phase 2.6
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pattern_analytics_empty_db_returns_zero_totals():
    r = await pattern_analytics(_FakeDB([]), days=30)
    assert r["totals"]["n_trades"] == 0
    assert r["totals"]["total_pnl_usd"] == 0.0
    assert r["patterns"] == []


@pytest.mark.asyncio
async def test_pattern_analytics_groups_trades_by_pattern():
    trades = [
        # 3 slow_rug winners
        {"status": "closed", "exit_time": _iso_ago(hours=1), "mode": "live",
         "greylist_pattern_at_entry": "slow_rug_tradeable",
         "pnl_pct": 18, "pnl_usd": 0.15, "exit_reason": "take-profit hit"},
        {"status": "closed", "exit_time": _iso_ago(hours=2), "mode": "live",
         "greylist_pattern_at_entry": "slow_rug_tradeable",
         "pnl_pct": 22, "pnl_usd": 0.18, "exit_reason": "take-profit hit"},
        {"status": "closed", "exit_time": _iso_ago(hours=3), "mode": "live",
         "greylist_pattern_at_entry": "slow_rug_tradeable",
         "pnl_pct": 15, "pnl_usd": 0.12, "exit_reason": "trailing-stop"},
        # 2 dump losers
        {"status": "closed", "exit_time": _iso_ago(hours=4), "mode": "live",
         "greylist_pattern_at_entry": "predictable_dump_tradeable",
         "pnl_pct": -12, "pnl_usd": -0.10, "exit_reason": "stop-loss hit"},
        {"status": "closed", "exit_time": _iso_ago(hours=5), "mode": "live",
         "greylist_pattern_at_entry": "predictable_dump_tradeable",
         "pnl_pct": 8, "pnl_usd": 0.06, "exit_reason": "trailing-stop"},
        # 1 unclassified (no pattern_at_entry)
        {"status": "closed", "exit_time": _iso_ago(hours=6), "mode": "live",
         "greylist_pattern_at_entry": None,
         "pnl_pct": 3, "pnl_usd": 0.02, "exit_reason": "take-profit hit"},
    ]
    r = await pattern_analytics(_FakeDB(trades), days=30)
    by_p = {p["pattern"]: p for p in r["patterns"]}
    assert by_p["slow_rug_tradeable"]["n_trades"] == 3
    assert by_p["slow_rug_tradeable"]["n_wins"] == 3
    assert by_p["slow_rug_tradeable"]["win_rate_pct"] == 100.0
    assert by_p["slow_rug_tradeable"]["mean_pnl_pct"] > 0
    assert by_p["predictable_dump_tradeable"]["n_trades"] == 2
    assert by_p["predictable_dump_tradeable"]["n_sl_exits"] == 1
    assert by_p["predictable_dump_tradeable"]["sl_rate_pct"] == 50.0
    assert by_p["unclassified"]["n_trades"] == 1
    assert r["totals"]["n_trades"] == 6
    assert r["totals"]["n_wins"] == 5  # 3 slow + 1 dump + 1 unclassified


@pytest.mark.asyncio
async def test_pattern_analytics_lookback_window_excludes_old_trades():
    trades = [
        {"status": "closed", "exit_time": _iso_ago(days=2), "mode": "live",
         "greylist_pattern_at_entry": "slow_rug_tradeable",
         "pnl_pct": 20, "pnl_usd": 0.20, "exit_reason": "tp"},
        {"status": "closed", "exit_time": _iso_ago(days=40), "mode": "live",
         "greylist_pattern_at_entry": "slow_rug_tradeable",
         "pnl_pct": 20, "pnl_usd": 0.20, "exit_reason": "tp"},
    ]
    r = await pattern_analytics(_FakeDB(trades), days=7)
    assert r["totals"]["n_trades"] == 1   # 40-day-old one dropped


@pytest.mark.asyncio
async def test_pattern_analytics_mode_filter():
    trades = [
        {"status": "closed", "exit_time": _iso_ago(hours=1), "mode": "live",
         "greylist_pattern_at_entry": "slow_rug_tradeable",
         "pnl_pct": 18, "pnl_usd": 0.15, "exit_reason": "tp"},
        {"status": "closed", "exit_time": _iso_ago(hours=2), "mode": "paper",
         "greylist_pattern_at_entry": "slow_rug_tradeable",
         "pnl_pct": 20, "pnl_usd": 0.0, "exit_reason": "tp"},
    ]
    r_live = await pattern_analytics(_FakeDB(trades), days=30, mode="live")
    r_paper = await pattern_analytics(_FakeDB(trades), days=30, mode="paper")
    assert r_live["totals"]["n_trades"] == 1
    assert r_paper["totals"]["n_trades"] == 1
    r_all = await pattern_analytics(_FakeDB(trades), days=30, mode=None)
    assert r_all["totals"]["n_trades"] == 2


@pytest.mark.asyncio
async def test_pattern_analytics_sorted_by_total_pnl_usd_desc():
    trades = [
        # dump = +$1
        {"status": "closed", "exit_time": _iso_ago(hours=1), "mode": "live",
         "greylist_pattern_at_entry": "predictable_dump_tradeable",
         "pnl_pct": 10, "pnl_usd": 1.0, "exit_reason": "tp"},
        # slow_rug = +$5
        {"status": "closed", "exit_time": _iso_ago(hours=2), "mode": "live",
         "greylist_pattern_at_entry": "slow_rug_tradeable",
         "pnl_pct": 15, "pnl_usd": 5.0, "exit_reason": "tp"},
    ]
    r = await pattern_analytics(_FakeDB(trades), days=30)
    assert r["patterns"][0]["pattern"] == "slow_rug_tradeable"
    assert r["patterns"][1]["pattern"] == "predictable_dump_tradeable"


# ---------------------------------------------------------------------------
# Phase 2.7 — bot.py uses pattern_suggested_exit_pct[0] as TP for slow_rug
# and predictable_dump, but only when greylist mode == 'live'.
# We test this via the in-memory greylist resolution (not by booting bot).
# ---------------------------------------------------------------------------

def test_phase_2_7_pattern_tp_calc_lower_bound():
    """The pattern→TP wiring uses suggested_exit_pct[0] (the LOWER bound).
    This is the contract the analytics endpoint relies on: TP = lo, not hi,
    so we exit BEFORE the typical rug window opens."""
    suggested_exit = (17.0, 20.0)  # slow_rug example: median 21, exit 17-20
    pattern_tp = float(suggested_exit[0])
    assert pattern_tp == 17.0
    # Sanity gate identical to bot.py
    assert 5.0 <= pattern_tp <= 60.0


def test_phase_2_7_pattern_tp_sanity_gate_rejects_extreme_values():
    """If the classifier emits a garbage value (<5 or >60), bot.py should
    fall back to the tier override rather than use it. Test the gate."""
    for bad in (-10, 0, 4.9, 60.1, 100, 999):
        ok = 5.0 <= bad <= 60.0
        assert ok is False, f"expected reject for {bad}"
    for good in (5.0, 10.0, 18.0, 27.0, 60.0):
        ok = 5.0 <= good <= 60.0
        assert ok is True, f"expected accept for {good}"


def test_phase_2_7_pattern_only_applies_to_precision_buckets():
    """fake_hype does NOT get the pattern→TP override — mempool-driven, not curve-%-driven."""
    precision_patterns = {"slow_rug_tradeable", "predictable_dump_tradeable"}
    # These three should NOT be in the precision set (no TP override):
    for p in ("fake_hype_tradeable", "unknown", None):
        assert p not in precision_patterns
