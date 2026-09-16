"""RH feed gate labels: closed tokens read `traded`, post-gate skips (cost-gate, r-size…) surface and stick briefly."""
import os
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")

from rh_paper import RHPaperTrader  # noqa: E402


def _trader():
    cfg = types.SimpleNamespace(rh_max_positions=5, rh_min_age_s=0, rh_max_age_min=60, rh_min_curve_pct=0, rh_max_curve_pct=100,
                                rh_min_mc_usd=0, rh_max_mc_usd=1e12, rh_min_unique_buyers=0, rh_min_growth_pct=0,
                                scanner_recent_inflow_window_s=300, scanner_holder_velocity_window_s=60,
                                rh_min_new_buyers_1m=0, rh_min_inflow_usd=0, rh_max_last_trade_age_s=60,
                                reentry_enabled=True, reentry_max_attempts=2, reentry_window_seconds=300,
                                reentry_size_multiplier=0.5, reentry_min_wait_s=20, hot_token_pnl_pct=25.0, hot_reentry_size_mult=1.5)
    disc = types.SimpleNamespace(tracking={}, _dirty=set(), _quote_usd=lambda s: 3000.0)
    state = types.SimpleNamespace(config=cfg, rh_discovery=disc, search_regime_block=lambda: None)
    return RHPaperTrader(state)


def _bucket(now):
    return {"start": now - 10, "graduated": False, "quote_symbol": "ETH", "curve_fill_pct": 10.0, "usd_market_cap": 5000.0,
            "buyers": {"a"}, "last_price_quote": 2.0, "first_price_quote": 1.0, "buy_events": [], "last_trade_ms": int(now * 1000)}


def test_closed_token_follows_reentry_controls():
    tr, now = _trader(), time.time()
    tr.entered.add("0xabc")                                   # traded before — no longer a block by itself
    tr.reentry.record_exit("0xabc", -5.0, tr.state.config, now=now - 5)
    assert tr._gates("0xabc", _bucket(now), now) == "reentry-wait"
    assert tr._gates("0xabc", _bucket(now), now + 20) is None          # gates decide again, sized × reentry multiplier
    assert tr.reentry.check("0xabc", tr.state.config, now + 20) == (None, 0.5)
    tr.positions["0xdef"] = {}
    assert tr._gates("0xdef", _bucket(now), now) == "already-entered"


def test_post_gate_skip_is_visible_and_sticky():
    tr, now = _trader(), time.time()
    b = _bucket(now)
    tr.state.rh_discovery.tracking["0x1"] = b
    assert tr._gates("0x1", b, now) is None
    tr._block_entry("0x1", b, "cost-gate", now)
    assert b["gate_reason"] == "cost-gate" and "0x1" in tr.state.rh_discovery._dirty
    assert tr._gates("0x1", b, now + 1) == "cost-gate"
    assert tr._gates("0x1", b, now + 31) is None            # 30 s later the momentum gates decide again
    assert tr.stats["skip_reasons"]["curve:cost-gate"] == 1


def test_rh_curve_cost_ceiling_is_the_deterministic_one():
    import cost_gate as cg
    # $10 stake on a $2.7k-deep RH curve with $0.60 measured gas: 11.2% friction — a known toll under the 30% first target
    q = cg.quote(size_usd=10.0, r_usd=1.15, first_target_r=2.62, protocol="rh", entry_slip_bps=800, exit_slip_bps=800,
                 fee_usd_round_trip=0.6, ladder=False, depth_usd=2756.0)
    assert q["cost_gate_pass"] is True and 11.0 < q["expected_cost_pct"] < 11.5
    # Sol keeps the 8% ceiling
    q2 = cg.quote(size_usd=10.0, r_usd=1.15, first_target_r=2.62, protocol="pumpfun", entry_slip_bps=800, exit_slip_bps=800,
                  fee_usd_round_trip=0.6, ladder=False, depth_usd=2756.0)
    assert q2["cost_gate_pass"] is False and "8%" in q2["cost_gate_reason"]
    # RH still sits out when the toll passes 12%
    q3 = cg.quote(size_usd=1.5, r_usd=0.2, first_target_r=2.6, protocol="rh", entry_slip_bps=800, exit_slip_bps=800,
                  fee_usd_round_trip=0.18, ladder=False, depth_usd=2756.0)
    assert q3["cost_gate_pass"] is False and "12%" in q3["cost_gate_reason"]


def test_tracked_tokens_band_uses_rh_age_window_and_gates():
    import rh_discovery as rd
    tr, now = _trader(), time.time()
    cfg = tr.state.config
    cfg.rh_min_age_s, cfg.rh_max_age_min = 5, 10
    cfg.band_new_max_age_min, cfg.scanner_growth_lookback_s = 60, 60
    cfg.scanner_min_growth_pct_new, cfg.scanner_min_new_buyers_new = 0, 0
    disc = rd.RHDiscovery(tr.state)
    disc._quote_usd = lambda s: 3000.0
    tr.state.rh_discovery = disc
    tr.state.rh_paper = tr
    for tok, age in (("0xyoung", 2), ("0xok", 120), ("0xold", 11 * 60)):
        b = _bucket(now); b["start"] = now - age
        b.update(symbol=tok, name=tok, launch_id="rh:" + tok, curve="c" + tok, price_samples=[], mc_samples=[], buy_count=1, pool_live=False)
        disc.tracking[tok] = b
    rows = {r["mint"]: r for r in disc.candidates_snapshot()}
    assert set(rows) == {"0xok"}                       # rh_min_age_s … rh_max_age_min, not the Sol New-band window
    assert rows["0xok"]["passes"] is True and rows["0xok"]["gate_reason"] == "pass"
    tr.positions["0xok"] = {}
    rows = {r["mint"]: r for r in disc.candidates_snapshot()}
    assert rows["0xok"]["passes"] is False and rows["0xok"]["gate_reason"] == "already-entered"
