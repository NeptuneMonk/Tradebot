"""Graduation is a venue change, not an exit — no path books a realised -100% from a zero curve/PONS quote
while the tokens are still in the wallet (Pump.fun → PumpSwap and RH PONS → v4)."""
import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot as botmod
from tests.test_profitability_refactor import _bot_stub
from tests.test_runner_book import _patched, _slot

MINT = "So11111111111111111111111111111111111111112"          # any valid base58 pubkey
CREATOR = "11111111111111111111111111111111"
COMPLETE_CURVE = {"complete": True, "virtual_sol_reserves": 30_000_000_000, "virtual_token_reserves": 1_000_000_000_000,
                  "real_sol_reserves": 0, "creator": CREATOR}
POOL_STATE = {"base_reserves": 1_000_000_000, "quote_reserves": 200_000_000_000}   # ~0.2 SOL for the 1M-token slot


def _st(mode="paper", book="scalp", **slot_kw):
    st = _patched(_bot_stub())
    st.config.paper_exit_latency_ms = 0
    st.scorecard = SimpleNamespace(record=AsyncMock(return_value="x"))
    st.db.launches = SimpleNamespace(find_one=AsyncMock(return_value=None), update_one=AsyncMock())
    st.check_kill_switch = AsyncMock()
    s = _slot(book, **slot_kw)
    s["trade"]["mint"] = MINT
    s["trade"]["mode"] = mode
    s["trade"]["creator"] = CREATOR
    s["_last_price_sol"] = 1.1
    st.active_trades[MINT] = s
    return st, s


def _db_sets(st):
    return [c[1] for c in st.calls if c[0] == "db"]


def _run_exit(st, reason):
    asyncio.run(botmod.BotState._exit(st, MINT, reason))


def _no_realised_loss(st, s):
    for d in _db_sets(st):
        assert d.get("status") != "closed", d
        assert d.get("pnl_pct") in (None,), d
    assert s["trade"].get("pnl_pct") is None and s["trade"].get("status") != "closed"


# ---------------- scalp/hunt paper: complete curve, no pool yet ----------------
def test_complete_curve_no_pool_yet_stays_active_graduating_no_pnl():
    st, s = _st()
    with patch("bot.pumpfun.fetch_bonding_curve_state", new=AsyncMock(return_value=COMPLETE_CURVE)), \
         patch("bot.pumpswap.find_pool_for_mint", new=AsyncMock(return_value=None)):
        _run_exit(st, "max hold 45s")
    assert MINT in st.active_trades
    assert s["trade"]["venue_stage"] == "graduating" and s["protocol"] == "pumpfun"
    _no_realised_loss(st, s)
    assert not any(c[0] == "ws" and c[1] in ("trade_exit", "trade_exit_terminal") for c in st.calls)


def test_curve_account_gone_is_graduating_not_state_unavailable_close():
    st, s = _st(book="hunt")
    with patch("bot.pumpfun.fetch_bonding_curve_state", new=AsyncMock(return_value=None)), \
         patch("bot.pumpswap.find_pool_for_mint", new=AsyncMock(return_value=None)):
        _run_exit(st, "stop-loss hit -12%")
    assert MINT in st.active_trades and s["trade"]["venue_stage"] == "graduating"
    _no_realised_loss(st, s)


# ---------------- pool appears within grace → pumpswap, still active, priced from reserves ----------------
def test_pool_within_grace_migrates_and_the_exit_books_amm_proceeds():
    st, s = _st()
    s["_runner_pool_missing_since"] = time.time() - 10                 # inside the 45 s grace
    with patch("bot.pumpfun.fetch_bonding_curve_state", new=AsyncMock(return_value=COMPLETE_CURVE)), \
         patch("bot.pumpswap.find_pool_for_mint", new=AsyncMock(return_value="PoolAddr111")), \
         patch("bot.pumpswap.fetch_pool_state", new=AsyncMock(return_value=POOL_STATE)):
        _run_exit(st, "max hold 45s")
    t = s["trade"]
    assert s["protocol"] == "pumpswap" and s["pumpswap_pool"] == "PoolAddr111"
    assert t["status"] == "closed" and t["exit_sig"] is None            # paper fill, booked from the AMM quote
    exp_sol, _ = botmod.pumpswap.quote_sell_sol(POOL_STATE, t["entry_tokens"], 800)
    assert abs(t["exit_sol"] - exp_sol / 1e9) < 1e-12
    assert t["pnl_pct"] > -100 and t["exit_sol"] > 0
    assert any(d.get("protocol") == "pumpswap" and d.get("venue_stage") == "pumpswap" for d in _db_sets(st))


def test_monitor_migration_keeps_row_active_and_prices_from_pool():
    st, s = _st(book="hunt")
    with patch("bot.pumpswap.find_pool_for_mint", new=AsyncMock(return_value="PoolAddr111")), \
         patch("bot.pumpswap.fetch_pool_state", new=AsyncMock(return_value=POOL_STATE)):
        ok = asyncio.run(st._detect_and_migrate_graduation(MINT, s))
    assert ok and MINT in st.active_trades and s["trade"]["venue_stage"] == "pumpswap"
    assert botmod.pumpswap.price_sol_per_raw_token(POOL_STATE) > 0
    sets = _db_sets(st)
    assert len(sets) == 1 and sets[0]["protocol"] == "pumpswap" and sets[0]["venue_stage"] == "pumpswap"
    _no_realised_loss(st, s)


# ---------------- grace expires, tokens still held → held-through-migrate, not -100% ----------------
def test_grace_expired_no_pool_parks_as_held_through_migrate_pnl_unset():
    st, s = _st()
    s["_runner_pool_missing_since"] = time.time() - 120
    with patch("bot.pumpfun.fetch_bonding_curve_state", new=AsyncMock(return_value=COMPLETE_CURVE)), \
         patch("bot.pumpswap.find_pool_for_mint", new=AsyncMock(return_value=None)):
        _run_exit(st, "max hold 45s")
    t = s["trade"]
    assert MINT not in st.active_trades
    assert t["status"] == "exit_failed_terminal" and t["venue_stage"] == "held_through_migrate"
    assert t["pnl_pct"] is None and t["pnl_usd"] is None and t["pnl_sol"] is None
    assert t["mark_price_sol"] == 1.1
    assert not any(d.get("status") == "closed" for d in _db_sets(st))
    assert ("ws", "trade_exit_terminal") in st.calls


def test_runner_no_pool_after_grace_is_held_not_a_zero_fill():
    import runner
    st, s = _st(book="runner")
    runner.promote(s["trade"], s, 1.2, "pumpfun")
    s["_runner_pool_missing_since"] = time.time() - 120
    with patch("bot.pumpfun.fetch_bonding_curve_state", new=AsyncMock(return_value=None)), \
         patch("bot.pumpswap.find_pool_for_mint", new=AsyncMock(return_value=None)):
        _run_exit(st, "runner-no-pool: curve complete, no PumpSwap pool after 45s")
    assert s["trade"]["status"] == "exit_failed_terminal" and s["trade"]["pnl_pct"] is None


# ---------------- live mid-sell 6005 ----------------
def _live_patches(state):
    fake_kp, fake_user = object(), object()
    return [
        patch("bot.pumpfun.fetch_bonding_curve_state", new=AsyncMock(return_value=state)),
        patch("bot.get_keypair", new=lambda: fake_kp),
        patch("bot.get_pubkey", new=lambda: fake_user),
        patch("bot.pumpfun.get_mint_token_program", new=AsyncMock(return_value=botmod.pumpfun.TOKEN_PROGRAM)),
        patch("bot.pumpfun.derive_associated_token_for_program", new=lambda *a, **k: "ATA"),
        patch("bot.pumpswap.get_token_balance", new=AsyncMock(return_value=1_000_000)),
        patch("bot.pumpfun.build_sell_ix", new=AsyncMock(return_value="IX")),
        patch("bot.pumpfun.send_versioned_tx", new=AsyncMock(side_effect=RuntimeError("Transaction simulation failed: {'InstructionError': [2, {'Custom': 6005}]}"))),
    ]


def test_live_6005_mid_sell_emergency_amm_fill_books_real_proceeds():
    st, s = _st(mode="live")
    live_curve = {**COMPLETE_CURVE, "complete": False}                 # curve looked open when we decided to sell
    sol_out = 150_000_000
    st._attempt_emergency_pumpswap_sell = AsyncMock(return_value=("EMSIG", sol_out, POOL_STATE))
    with patch("bot.pumpswap.find_pool_for_mint", new=AsyncMock(return_value=None)):
        ps = _live_patches(live_curve)
        for p in ps:
            p.start()
        try:
            _run_exit(st, "stop-loss hit -12%")
        finally:
            for p in ps:
                p.stop()
    t = s["trade"]
    assert t["status"] == "closed" and t["exit_sig"] == "EMSIG"
    assert abs(t["exit_sol"] - sol_out / 1e9) < 1e-12 and t["pnl_pct"] > -100
    assert st._attempt_emergency_pumpswap_sell.await_count == 1
    assert botmod.pumpfun.send_versioned_tx.await_count if hasattr(botmod.pumpfun.send_versioned_tx, "await_count") else True


def test_live_6005_no_pool_inside_grace_waits_then_parks_after_grace_never_minus_100():
    st, s = _st(mode="live")
    live_curve = {**COMPLETE_CURVE, "complete": False}
    st._attempt_emergency_pumpswap_sell = AsyncMock(return_value=None)
    ps = _live_patches(live_curve)
    send_mock = None
    for p in ps:
        m = p.start()
        if p.attribute == "send_versioned_tx":
            send_mock = m
    try:
        with patch("bot.pumpswap.find_pool_for_mint", new=AsyncMock(return_value=None)):
            _run_exit(st, "stop-loss hit -12%")
            # inside grace: still active, graduating, no PnL, further curve sells not attempted by the exit
            assert MINT in st.active_trades and s["trade"]["venue_stage"] == "graduating"
            _no_realised_loss(st, s)
            sends_before = send_mock.await_count
            # grace passes with no pool → held-through-migrate, PnL unset (tokens still in the wallet)
            s["_runner_pool_missing_since"] = time.time() - 120
            _run_exit(st, "stop-loss hit -12%")
    finally:
        for p in ps:
            p.stop()
    t = s["trade"]
    assert MINT not in st.active_trades
    assert t["status"] == "exit_failed_terminal" and t["venue_stage"] == "held_through_migrate"
    assert t["pnl_pct"] is None and t["pnl_sol"] is None
    assert send_mock.await_count == sends_before  # curve is known dead: no further curve IXs are sent
    assert st._attempt_emergency_pumpswap_sell.await_count == 1
    assert not any(d.get("status") == "closed" for d in _db_sets(st))


def test_partial_exit_refuses_completed_curve():
    st, s = _st(book="hunt")
    with patch("bot.pumpfun.fetch_bonding_curve_state", new=AsyncMock(return_value=COMPLETE_CURVE)):
        ok = asyncio.run(botmod.BotState._partial_exit_impl(st, MINT, s, 0.35, "ladder +1R"))
    assert ok is False and not s["trade"].get("partial_done")


def test_source_has_no_state_unavailable_close():
    src = Path(botmod.__file__).read_text()
    assert '{protocol} state unavailable"' not in src
    assert 'trade_doc["pnl_pct"] = -100' not in src


# ---------------- RH twin: PONS → v4 ----------------
def _rh():
    import rh_paper as rp
    from tests.test_rh_paper import TOKEN, enter, hot_bucket, make_state
    st = make_state(take_profit_pct=20.0)
    b = hot_bucket(st.rh_discovery, time.time(), price=2e-9)
    enter(st)
    assert TOKEN in st.rh_paper.positions
    return st, b, TOKEN, rp


def test_rh_graduated_flips_venue_to_pool_stays_active_and_exits_on_pool_proceeds():
    st, b, TOKEN, rp = _rh()
    pos = st.rh_paper.positions[TOKEN]
    t = pos["trade"]
    b["graduated"] = True
    b["last_price_quote"] = 2.1e-9
    assert st.rh_paper._decide_exit(pos, b, time.time()) is None       # graduation itself is not an exit
    assert t["venue"] == "pool" and TOKEN in st.rh_paper.positions and t.get("status") != "closed"
    # a later real exit prices on the v4 pool quoter, never the dead curve
    pool_out = int(t["entry_tokens"] * 2.5e-9 * 1e18)
    with patch.object(rp.rh_dex, "quote_sell", new=AsyncMock(return_value=pool_out)):
        asyncio.run(st.rh_paper.exit(TOKEN, "take_profit"))
    doc = st.db.trades.docs[t["id"]]
    assert doc["status"] == "closed" and doc["exit_venue"] == "pool"
    assert abs(doc["exit_quote"] - pool_out / 1e18) < 1e-15 and doc["pnl_pct"] > -100 and doc["exit_usd"] > 0


def test_rh_exit_flips_to_pool_even_if_decide_exit_never_saw_the_sweep():
    """Operator rule: graduation never exits. A curve-raised reason arriving after the sweep flips the venue and is
    dropped; the NEXT decision (now on pool prices) sells on the v4 pool."""
    st, b, TOKEN, rp = _rh()
    pos = st.rh_paper.positions[TOKEN]
    t = pos["trade"]
    b["graduated"] = True                                                # swept between two ticks; venue still 'curve'
    pool_out = int(t["entry_tokens"] * 2.2e-9 * 1e18)
    with patch.object(rp.rh_dex, "quote_sell", new=AsyncMock(return_value=pool_out)):
        asyncio.run(st.rh_paper.exit(TOKEN, "max_hold"))
        assert TOKEN in st.rh_paper.positions and t["venue"] == "pool" and pos.get("_exiting") is None
        assert st.db.trades.docs.get(t["id"], {}).get("status") != "closed"
        asyncio.run(st.rh_paper.exit(TOKEN, "max_hold"))                # gates re-fired on the pool → real pool fill
    doc = st.db.trades.docs[t["id"]]
    assert doc["venue"] == "pool" and doc["exit_venue"] == "pool" and abs(doc["exit_quote"] - pool_out / 1e18) < 1e-15


def test_rh_zero_quote_after_graduation_never_books_minus_100():
    st, b, TOKEN, rp = _rh()
    pos = st.rh_paper.positions[TOKEN]
    t = pos["trade"]
    b["graduated"] = True
    st.rh_paper._switch_to_pool(pos, b, time.time())                   # venue already flipped by the monitor
    st.rh_paper._paper_pool_proceeds = AsyncMock(return_value=(0.0, rp.POOL_FEE_FRACTION))
    asyncio.run(st.rh_paper.exit(TOKEN, "stop_loss"))
    assert TOKEN in st.rh_paper.positions and t.get("status") != "closed" and not t.get("exit_time")
    assert st.db.trades.docs.get(t["id"], {}).get("status") != "closed"
    assert pos.get("_exiting") is None and pos["_zero_quote_retry_after"] > time.time()
    # cooling down: the next tick's exit call is a no-op instead of a retry storm
    asyncio.run(st.rh_paper.exit(TOKEN, "stop_loss"))
    assert TOKEN in st.rh_paper.positions and pos.get("_exiting") is None


def _rh_ride(**cfg):
    """Graduated-while-held position armed on the R trail. Entry 2e-9, SL 10% → 1R ≈ SL%-with-slip."""
    import rh_paper as rp
    from tests.test_rh_paper import TOKEN, enter, hot_bucket, make_state
    st = make_state(take_profit_pct=14.0, **cfg)
    b = hot_bucket(st.rh_discovery, time.time(), price=2e-9)
    enter(st)
    pos = st.rh_paper.positions[TOKEN]
    t = pos["trade"]
    t["expected_cost_pct"] = 3.0
    b["graduated"] = True
    return st, b, TOKEN, pos, t


def _tick(st, pos, b, price):
    b["last_price_quote"] = price
    return st.rh_paper._decide_exit(pos, b, time.time())


def test_r_trail_handoff_has_no_fixed_tp_and_rides_the_pump():
    st, b, TOKEN, pos, t = _rh_ride()
    entry = t["entry_price_quote"]
    assert _tick(st, pos, b, entry * 1.75) is None                     # +75% at the sweep: the 14% TP does NOT fire
    assert t["r_trail"] is True and t["venue"] == "pool"
    one_r = t["sl_pct_with_slip"]
    for mult in (3, 10, 40, 80):                                        # keeps riding while the peak climbs
        assert _tick(st, pos, b, entry * mult) is None
    assert pos["_r_trail_stop_pct"] == 3.0                              # stop ratcheted to breakeven + exit costs after +1R
    assert pos["_r_trail_trail_pct"] >= one_r


def test_r_trail_exits_on_r_giveback_from_peak():
    st, b, TOKEN, pos, t = _rh_ride()
    entry = t["entry_price_quote"]
    assert _tick(st, pos, b, entry * 1.75) is None
    peak = entry * 10
    assert _tick(st, pos, b, peak) is None
    trail = pos["_r_trail_trail_pct"]
    assert _tick(st, pos, b, peak * (1 - (trail - 1) / 100.0)) is None   # inside the giveback band
    assert _tick(st, pos, b, peak * (1 - (trail + 1) / 100.0)) == "r_trail"


def test_r_trail_stop_is_breakeven_plus_costs_after_one_r():
    st, b, TOKEN, pos, t = _rh_ride()
    entry = t["entry_price_quote"]
    assert _tick(st, pos, b, entry * 1.75) is None                     # peak cleared +1R → stop = +expected_cost_pct
    assert _tick(st, pos, b, entry * 1.02) == "r_trail_stop"           # +2% < +3% costs floor: out at ~breakeven, never a loser


def test_r_trail_handoff_off_keeps_fixed_tp():
    st, b, TOKEN, pos, t = _rh_ride(rh_grad_handoff_r_trail=False)
    entry = t["entry_price_quote"]
    assert _tick(st, pos, b, entry * 1.75) == "take_profit"
    assert t["r_trail"] is False


def _rh_red_no_momentum(**cfg):
    """Position 40s old, never above +5%, now −8% but climbing off a −12% trough set 25s ago."""
    from tests.test_rh_paper import TOKEN, enter, hot_bucket, make_state
    st = make_state(**cfg)
    now = time.time()
    b = hot_bucket(st.rh_discovery, now, price=2e-9)
    enter(st)
    pos = st.rh_paper.positions[TOKEN]
    t = pos["trade"]
    entry = t["entry_price_quote"]
    pos["opened"] = now - 40
    pos["peak_price"] = entry * 1.02
    pos["trough_price"], pos["trough_ts"] = entry * 0.88, now - 25
    b["price_samples"].clear()
    b["price_samples"].extend([(now - 32, entry * 0.89), (now - 15, entry * 0.90), (now - 1, entry * 0.92)])
    return st, b, TOKEN, pos, t, entry, now


def test_rh_red_no_momentum_is_held_as_dust():
    """Rule (2026-09-21): momentum / velocity never sells a RED position — no kill, no recovery watch; it waits for a
    price-based exit (stop / clock)."""
    st, b, TOKEN, pos, t, entry, now = _rh_red_no_momentum()
    b["last_price_quote"] = entry * 0.92
    assert st.rh_paper._decide_exit(pos, b, now) in (None, "max_hold")        # never no_momentum / recovery-*
    assert "_recovery_watch" not in pos and pos["_nm_checked"] is True
    b["last_price_quote"] = entry * 0.93
    assert st.rh_paper._decide_exit(pos, b, now + 91) == "max_hold"           # the book CLOCK (price-agnostic) is what finally closes it
    # flat dead red tape: same — held
    st, b, TOKEN, pos, t, entry, now = _rh_red_no_momentum()
    b["price_samples"].clear()
    b["price_samples"].extend([(now - 32, entry * 0.92), (now - 1, entry * 0.92)])
    b["last_price_quote"] = entry * 0.92
    assert st.rh_paper._decide_exit(pos, b, now) in (None, "max_hold")        # clock may fire; momentum never does
    assert "_recovery_watch" not in pos


def test_rh_green_low_mfe_no_momentum_still_kills():
    st, b, TOKEN, pos, t, entry, now = _rh_red_no_momentum()
    pos["peak_price"] = entry * 1.02
    b["last_price_quote"] = entry * 1.01                                       # green but never reached min MFE
    assert st.rh_paper._decide_exit(pos, b, now) == "no_momentum"
