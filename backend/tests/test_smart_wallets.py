"""Smart wallets: learning grades, promotion, soft boost, hard trigger."""
import asyncio
import os
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from smart_wallets import SmartWallets, LAMPORTS_PER_SOL  # noqa: E402

CFG = dict(enabled=True, smart_wallet_enabled=True, smart_wallet_mode="both", smart_wallet_min_buy_sol=0.2,
           smart_wallet_max_per_hour=6, smart_wallet_hit_ttl_s=180, smart_wallet_soft_boost=25.0,
           smart_wallet_gate_relax_pct=50.0, smart_wallet_learn_enabled=True, smart_wallet_learn_early_s=120,
           smart_wallet_learn_horizon_s=300, smart_wallet_learn_min_gain_pct=50.0, smart_wallet_learn_min_wins=3,
           smart_wallet_learn_min_win_rate=0.6, band_new_min_age_min=0.0, band_new_max_age_min=15.0,
           band_seasoned_min_age_min=0.0, band_seasoned_max_age_min=60.0)


def _state(**over):
    cfg = SimpleNamespace(**{**CFG, **over})
    st = SimpleNamespace(config=cfg, tracking={}, active_trades={}, _pending_entry_mints=set(), entered_mints=set(),
                         stopping_gracefully=False, db=None, entered=[])

    async def _enter(launch, risk, action):
        st.entered.append((launch.mint, action))
    st._enter = _enter
    return st


def _bucket(start, creator="dev", protocol="pumpfun"):
    return {"start": start, "creator": creator, "protocol": protocol, "symbol": "TST", "last_price_sol": 0.0}


def test_grading_promotes_repeat_early_winner():
    sw = SmartWallets(_state())
    now = time.time()
    for i in range(3):
        b = _bucket(now - 400)
        sw.on_buy(f"m{i}", b, "dev", int(1 * LAMPORTS_PER_SOL), 1.0, now - 390)           # dev buy ignored
        sw.on_buy(f"m{i}", b, "alpha", int(0.5 * LAMPORTS_PER_SOL), 1.0, now - 380)        # early, price 1.0
        sw.on_buy(f"m{i}", b, "late", int(0.5 * LAMPORTS_PER_SOL), 1.2, now - 200)         # outside early window
        sw.on_buy(f"m{i}", b, "other", int(0.5 * LAMPORTS_PER_SOL), 1.8, now - 100)        # pushes peak to 1.8
        assert sw.grade_bucket(b, now) == 1                                                 # only alpha graded
        assert sw.grade_bucket(b, now) == 0                                                 # graded once
    assert "dev" not in sw.learned and "late" not in sw.learned
    assert sw.learned["alpha"]["wins"] == 3 and sw.kind_of("alpha") == "learned"
    assert sw.promoted_count() == 1


def test_miss_and_no_promotion_under_threshold():
    sw = SmartWallets(_state())
    now = time.time()
    b = _bucket(now - 400)
    sw.on_buy("m", b, "beta", int(0.5 * LAMPORTS_PER_SOL), 1.0, now - 390)
    sw.on_buy("m", b, "x", int(0.5 * LAMPORTS_PER_SOL), 1.2, now - 380)   # +20 % only
    sw.grade_bucket(b, now)
    assert sw.learned["beta"]["misses"] == 1 and sw.kind_of("beta") is None


def test_final_grade_on_eviction_needs_60s():
    sw = SmartWallets(_state())
    now = time.time()
    b = _bucket(now - 30)
    sw.on_buy("m", b, "w", int(0.5 * LAMPORTS_PER_SOL), 1.0, now - 20)
    assert sw.grade_bucket(b, now, final=True) == 0
    b["start"] = now - 70
    assert sw.grade_bucket(b, now, final=True) == 1


def test_soft_boost_and_gate_relax():
    st = _state(smart_wallet_mode="soft")
    sw = SmartWallets(st)
    sw.manual["top"] = {"label": "t"}
    now = time.time()
    b = _bucket(now - 60)
    g = {"min_growth_pct": 10.0, "min_inflow_sol": 2.0, "min_new_buyers": 4, "min_liquidity_sol": 5.0}
    assert sw.relax_gates(g, b, now) == g and sw.rank_boost(b, now) == 0
    sw.on_buy("m", b, "top", int(0.1 * LAMPORTS_PER_SOL), 1.0, now)           # below min buy: ignored
    assert sw.fresh_hits(b, now) == 0
    sw.on_buy("m", b, "top", int(0.5 * LAMPORTS_PER_SOL), 1.0, now)
    assert sw.fresh_hits(b, now) == 1 and sw.rank_boost(b, now) == 25.0
    r = sw.relax_gates(g, b, now)
    assert r["min_growth_pct"] == 5.0 and r["min_inflow_sol"] == 1.0 and r["min_new_buyers"] == 2 and r["min_liquidity_sol"] == 5.0
    assert sw.fresh_hits(b, now + 400) == 0                                   # hit expired
    assert st.entered == []                                                   # soft never fires


def test_hard_trigger_fires_once_in_window_and_rate_caps():
    async def run():
        st = _state(smart_wallet_mode="hard", smart_wallet_max_per_hour=1)
        sw = SmartWallets(st)
        sw.manual["top"] = {"label": "t"}
        now = time.time()
        old = _bucket(now - 20 * 60)                                            # outside the 15-min new band
        sw.on_buy("old", old, "top", int(1 * LAMPORTS_PER_SOL), 1.0, now)
        assert sw.stats["out_of_window"] == 1 and st.entered == []
        b = _bucket(now - 60)
        sw.on_buy("m1", b, "top", int(1 * LAMPORTS_PER_SOL), 1.0, now)
        assert b.get("_sw_fired")
        sw.on_buy("m1", b, "top", int(1 * LAMPORTS_PER_SOL), 1.0, now + 1)   # in flight: no second task
        await asyncio.sleep(0)
        assert st.entered == [("m1", "smart_wallet")]
        assert sw.recent[0]["result"].startswith("parked")                    # stub _enter never opens → parked for a manual buy
        assert "_sw_fired" not in b                                           # refusal re-arms the mint
        b2 = _bucket(now - 60)
        sw.on_buy("m2", b2, "top", int(1 * LAMPORTS_PER_SOL), 1.0, now + 2)  # rate cap 1/h
        await asyncio.sleep(0)
        assert len(st.entered) == 1 and sw.stats["rate_capped"] == 1
    asyncio.run(run())


def test_disabled_still_learns_but_never_triggers():
    st = _state(smart_wallet_enabled=False, smart_wallet_mode="hard")
    sw = SmartWallets(st)
    sw.manual["top"] = {"label": "t"}
    now = time.time()
    b = _bucket(now - 60)
    sw.on_buy("m", b, "top", int(1 * LAMPORTS_PER_SOL), 1.0, now)
    assert "smart_hits" not in b and st.entered == [] and "top" in b["_sw_early"]


def test_refused_or_capped_trigger_parks_token_for_manual_buy():
    async def run():
        st = _state(smart_wallet_mode="hard", smart_wallet_max_per_hour=1, smart_wallet_park_s=300)
        sw = SmartWallets(st)
        sw.manual["top"] = {"label": "Aurelius"}
        now = time.time()
        b = _bucket(now - 60)
        b["entry_refusal"] = {"reason": "hunt-cap", "ts": now}
        sw.on_buy("m1", b, "top", int(1 * LAMPORTS_PER_SOL), 1.0, now)
        await asyncio.sleep(0)
        p = sw.parked(b)
        assert p and p["reason"] == "hunt-cap" and p["label"] == "Aurelius" and 290 < p["left_s"] <= 300
        assert sw.recent[0]["result"] == "parked: hunt-cap"
        b2 = _bucket(now - 60)
        sw.on_buy("m2", b2, "top", int(1 * LAMPORTS_PER_SOL), 1.0, now)
        assert sw.parked(b2)["reason"] == "rate-cap"
        b2["smart_parked"]["until"] = now - 1
        assert sw.parked(b2) is None and "smart_parked" not in b2        # expired parks clear themselves
    asyncio.run(run())
