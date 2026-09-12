"""Held-bag watcher: a late pool is a second chance to ride (intent=hold → reattach), never an order to
market-sell (intent=exit sells only while auto_sell_held_bags is on). RH twin: lost bucket → rehydrate from v4."""
import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot as botmod
from tests.test_graduation_hold import COMPLETE_CURVE, MINT, _run_exit, _st
from tests.test_profitability_refactor import _bot_stub
from tests.test_runner_book import _patched, _slot

POOL = "PoolAddr111"
DEEP = {"base_reserves": 1_000_000_000, "quote_reserves": 85_000_000_000}      # 85 SOL — a real graduate
THIN = {"base_reserves": 1_000_000_000, "quote_reserves": 4_000_000_000}       # 4 SOL — dust / half-migrated


def _held_row(intent="hold", **kw):
    row = {"id": "h1", "mint": MINT, "symbol": "HELD", "mode": "paper", "status": "exit_failed_terminal", "book": "hunt", "chain": "solana",
           "protocol": "pumpfun", "venue_stage": "held_through_migrate", "held_intent": intent, "held_exit_reason": "stop-loss hit -12%" if intent == "exit" else None,
           "entry_tokens": 1_000_000, "entry_sol": 0.16, "entry_price_sol": 1.0, "entry_usd": 16.0, "exit_time": "2026-06-01T00:00:00+00:00",
           "exit_reason": "curve complete | held through graduation"}
    row.update(kw)
    return row


def _watcher(rows, pool_state, *, gate=False):
    st = _patched(_bot_stub())
    st.config.auto_sell_held_bags = gate
    st._held_rows = AsyncMock(return_value=rows)
    st._monitor_position = AsyncMock()
    st._exit = AsyncMock(side_effect=lambda mint, reason: st.active_trades.pop(mint, None) and st.calls.append(("exit", reason)))
    patches = [patch("bot.pumpswap.find_pool_for_mint", new=AsyncMock(return_value=POOL)),
               patch("bot.pumpswap.fetch_pool_state", new=AsyncMock(return_value=pool_state))]
    for p in patches:
        p.start()
    try:
        out = asyncio.run(st._held_bag_watch_once())
    finally:
        for p in patches:
            p.stop()
    return st, out


def _sets(st):
    return [c[1] for c in st.calls if c[0] == "db"]


def test_hold_intent_reattaches_to_active_on_pumpswap_and_sells_nothing():
    row = _held_row("hold")
    st, out = _watcher([row], DEEP)
    assert out["reattached"] == 1 and out["sold"] == 0
    assert row["status"] == "active" and row["protocol"] == "pumpswap" and row["pumpswap_pool"] == POOL and row["venue_stage"] == "pumpswap"
    assert row["exit_time"] is None and row["held_history"][0]["pool_sol"] == 85.0
    assert MINT in st.active_trades and st.active_trades[MINT]["protocol"] == "pumpswap"
    assert not any(c[0] == "exit" for c in st.calls)
    assert any(d.get("status") == "active" and d.get("protocol") == "pumpswap" for d in _sets(st))
    assert ("ws", "held_bag_reattached") in st.calls


def test_exit_intent_with_gate_off_stays_held_marks_pool_ready_no_tx():
    row = _held_row("exit")
    st, out = _watcher([row], DEEP, gate=False)
    assert out["ready"] == 1 and out["sold"] == 0 and out["reattached"] == 0
    assert row["status"] == "exit_failed_terminal" and row["held_pool_ready"] is True and row["held_pool_sol"] == 85.0 and row["pumpswap_pool"] == POOL
    assert MINT not in st.active_trades
    assert not any(c[0] == "exit" for c in st.calls)
    assert not any(d.get("status") == "active" for d in _sets(st))
    assert row["pnl_pct"] is None if "pnl_pct" in row else True


def test_exit_intent_with_gate_on_sells_through_the_normal_exit_path_with_retry_cap():
    row = _held_row("exit")
    st, out = _watcher([row], DEEP, gate=True)
    assert out["sold"] == 1 and row["held_retry_count"] == 1
    assert any(c[0] == "exit" and c[1].startswith("held-bag sell (attempt 1/3): stop-loss hit -12%") for c in st.calls)
    assert MINT not in st.active_trades                     # the stubbed _exit booked it
    # 3 retries burned → stop watching, still held, never -100
    row3 = _held_row("exit", held_retry_count=3)
    st3, out3 = _watcher([row3], DEEP, gate=True)
    assert out3["sold"] == 1 and row3["held_watch_done"] is True and row3["status"] == "exit_failed_terminal"
    assert not any(c[0] == "exit" for c in st3.calls)


def test_failed_gated_sell_parks_the_bag_again_with_exit_intent():
    row = _held_row("exit")
    st = _patched(_bot_stub())
    st.config.auto_sell_held_bags = True
    st._held_rows = AsyncMock(return_value=[row])
    st._monitor_position = AsyncMock()
    st._exit = AsyncMock(return_value=None)                  # sell did not land: slot left in active_trades
    with patch("bot.pumpswap.find_pool_for_mint", new=AsyncMock(return_value=POOL)), \
         patch("bot.pumpswap.fetch_pool_state", new=AsyncMock(return_value=DEEP)):
        asyncio.run(st._held_bag_watch_once())
    assert MINT not in st.active_trades
    assert row["status"] == "exit_failed_terminal" and row["venue_stage"] == "held_through_migrate"
    assert row["held_intent"] == "exit" and row["held_exit_reason"] == "stop-loss hit -12%" and row["held_retry_count"] == 1
    assert row["pnl_pct"] is None and row["held_next_check_ts"] > time.time()


def test_thin_pool_is_not_a_pool_backoff_only():
    row = _held_row("hold")
    st, out = _watcher([row], THIN)
    assert out["waiting"] == 1 and out["reattached"] == 0
    assert row["status"] == "exit_failed_terminal" and row["held_pool_misses"] == 1 and row["held_next_check_ts"] > time.time()
    assert MINT not in st.active_trades


def test_no_pool_yet_backs_off_and_respects_next_check():
    row = _held_row("hold")
    st = _patched(_bot_stub())
    st._held_rows = AsyncMock(return_value=[row])
    with patch("bot.pumpswap.find_pool_for_mint", new=AsyncMock(return_value=None)):
        out = asyncio.run(st._held_bag_watch_once())
    assert out["waiting"] == 1 and row["held_next_check_ts"] > time.time()
    with patch("bot.pumpswap.find_pool_for_mint", new=AsyncMock(return_value=POOL)) as fp:
        out2 = asyncio.run(st._held_bag_watch_once())
    assert out2["checked"] == 0 and fp.await_count == 0        # still inside the backoff window


def test_legacy_gave_up_row_is_exit_intent_and_gate_off_means_no_tx():
    row = _held_row("exit"); row.pop("held_intent"); row.pop("held_exit_reason"); row["venue_stage"] = None
    row["exit_reason"] = "GAVE UP after 3 sell retries — manual recovery needed: stop-loss hit (-20.5%) [fast]"
    st, out = _watcher([row], DEEP, gate=False)
    assert out["ready"] == 1 and row["status"] == "exit_failed_terminal" and not any(c[0] == "exit" for c in st.calls)


def test_live_row_with_zero_balance_stops_watching():
    row = _held_row("hold", mode="live")
    st = _patched(_bot_stub())
    st._held_rows = AsyncMock(return_value=[row])
    st._held_token_balance = AsyncMock(return_value=0)
    out = asyncio.run(st._held_bag_watch_once())
    assert out["gone"] == 1 and row["held_watch_done"] is True and row["status"] == "exit_failed_terminal"


def test_active_or_pending_mints_are_skipped():
    row = _held_row("hold")
    st = _patched(_bot_stub())
    st._held_rows = AsyncMock(return_value=[row])
    st.active_trades[MINT] = _slot("hunt")
    out = asyncio.run(st._held_bag_watch_once())
    assert out["checked"] == 0


# ---------------- intent is stamped at park time ----------------
def test_park_from_exit_path_stamps_exit_intent_and_reason():
    st, s = _st()
    s["_runner_pool_missing_since"] = time.time() - 120
    with patch("bot.pumpfun.fetch_bonding_curve_state", new=AsyncMock(return_value=COMPLETE_CURVE)), \
         patch("bot.pumpswap.find_pool_for_mint", new=AsyncMock(return_value=None)):
        _run_exit(st, "stop-loss hit -12%")
    t = s["trade"]
    assert t["held_intent"] == "exit" and t["held_exit_reason"].startswith("exit 'stop-loss hit -12%'") and t["held_parked_at"]


def test_park_from_monitor_or_runner_no_pool_stamps_hold_intent():
    st, s = _st(book="hunt")
    s["_runner_pool_missing_since"] = time.time() - 120
    asyncio.run(st._graduation_hold_or_wait(MINT, s, "curve complete, waiting for PumpSwap pool"))
    assert s["trade"]["held_intent"] == "hold" and s["trade"]["held_exit_reason"] is None
    assert botmod.BotState._held_intent_for("runner-no-pool: curve complete, no PumpSwap pool after 45s") == "hold"
    assert botmod.BotState._held_intent_for("trailing-stop hit") == "exit"
    assert botmod.BotState._held_intent_for("max hold 45s") == "exit"


def test_slot_from_doc_restores_grace_clock():
    st = _bot_stub()
    slot = st._slot_from_doc({"mint": MINT, "protocol": "pumpfun", "venue_stage": "graduating", "graduating_since": 123.0, "entry_price_sol": 1.0})
    assert slot["_runner_pool_missing_since"] == 123.0 and slot["_curve_complete"] is True and slot["protocol"] == "pumpfun"


# ---------------- RH twin: lost bucket → rehydrate from the v4 pool ----------------
def _rh_pos():
    import rh_paper as rp
    from tests.test_rh_paper import TOKEN, make_state
    st = make_state()
    t = {"id": "rh1", "mint": TOKEN, "symbol": "TST", "chain": "rh", "mode": "paper", "status": "active", "entry_price_quote": 2e-9,
         "entry_usd": 10.0, "entry_quote": 0.004, "entry_tokens": 2_000_000.0, "quote_symbol": "ETH", "curve": "0x" + "c" * 40}
    st.rh_paper.positions[TOKEN] = {"trade": t, "peak_price": 2e-9, "_last_price": 2e-9, "opened": time.time() - 300}
    st.rh_paper.exit = AsyncMock(side_effect=lambda token, reason, **k: st.rh_paper.positions.pop(token, None))
    return st, TOKEN, rp


def test_rh_lost_bucket_rehydrates_from_pool_and_stays_active():
    st, TOKEN, rp = _rh_pos()
    pos = st.rh_paper.positions[TOKEN]
    with patch.object(rp.rh_dex, "spot_price", new=AsyncMock(return_value=2.4e-9)):
        asyncio.run(st.rh_paper._monitor(time.time()))
    b = st.rh_discovery.tracking[TOKEN]
    assert b["graduated"] is True and b["rehydrated"] is True and b["last_price_quote"] == 2.4e-9 and b["symbol"] == "TST"
    assert pos["trade"]["venue"] == "pool" and TOKEN in st.rh_paper.positions
    st.rh_paper.exit.assert_not_called()
    assert TOKEN in st.rh_discovery._pool_watch_tokens()      # pool swaps now price it


def test_rh_no_pool_waits_out_the_grace_before_tracking_lost():
    st, TOKEN, rp = _rh_pos()
    pos = st.rh_paper.positions[TOKEN]
    now = time.time()
    with patch.object(rp.rh_dex, "spot_price", new=AsyncMock(return_value=0.0)):
        asyncio.run(st.rh_paper._monitor(now))
        assert TOKEN in st.rh_paper.positions and st.rh_paper.exit.await_count == 0 and "_tracking_lost_since" in pos
        pos["_tracking_lost_since"] = now - rp.TRACKING_LOST_GRACE_S - 1
        pos["_rehydrate_next"] = 0.0
        asyncio.run(st.rh_paper._monitor(now))
        for _ in range(3):
            asyncio.run(asyncio.sleep(0))
    assert st.rh_paper.exit.await_count == 1 and st.rh_paper.exit.await_args.args[1] == "tracking_lost"


def test_rh_live_sell_in_flight_is_never_tracking_lost():
    st, TOKEN, rp = _rh_pos()
    st.rh_paper.positions[TOKEN]["_live_sell_inflight"] = True
    with patch.object(rp.rh_dex, "spot_price", new=AsyncMock(return_value=0.0)) as sp:
        asyncio.run(st.rh_paper._monitor(time.time() + 10_000))
    assert sp.await_count == 0 and st.rh_paper.exit.await_count == 0 and TOKEN in st.rh_paper.positions
