"""Sells must land: a slow tx keeps the position ACTIVE with its signatures watched (never parked), a landed
revert escalates to the emergency AMM sell and only a liquidity failure parks the row; tokens gone + sale found
on-chain books the real exit."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot as botmod
import landing
from tests.test_profitability_refactor import _bot_stub
from tests.test_runner_book import _patched, _slot

MINT = "So11111111111111111111111111111111111111112"
CREATOR = "11111111111111111111111111111111"
CURVE = {"complete": False, "virtual_sol_reserves": 30_000_000_000, "virtual_token_reserves": 1_000_000_000_000,
         "real_sol_reserves": 5_000_000_000, "creator": CREATOR}


def _st():
    st = _patched(_bot_stub())
    st.config.paper_exit_latency_ms = 0
    st.config.exit_max_landed_reverts = 3        # these tests exercise the 3-revert escalation (default is now 5)
    st.scorecard = SimpleNamespace(record=AsyncMock(return_value="x"))
    st.db.launches = SimpleNamespace(find_one=AsyncMock(return_value=None), update_one=AsyncMock())
    st.check_kill_switch = AsyncMock()
    st._watch_late_sell = AsyncMock()
    st.scan_wake = asyncio.Event()
    s = _slot("scalp")
    s["trade"].update(mint=MINT, mode="live", creator=CREATOR)
    s["_last_price_sol"] = 1.1
    st.active_trades[MINT] = s
    return st, s


def _patches(balance=1_000_000):
    return [
        patch("bot.pumpfun.fetch_bonding_curve_state", new=AsyncMock(return_value=CURVE)),
        patch("bot.get_keypair", new=lambda: object()),
        patch("bot.get_pubkey", new=lambda: SimpleNamespace(__str__=lambda self: "WALLET")),
        patch("bot.pumpfun.get_mint_token_program", new=AsyncMock(return_value=botmod.pumpfun.TOKEN_PROGRAM)),
        patch("bot.pumpswap.get_associated_token_address", new=lambda *a, **k: "ATA"),
        patch("bot.pumpswap.get_token_balance", new=AsyncMock(return_value=balance)),
        patch("bot.pumpfun.build_sell_ix", new=AsyncMock(return_value="IX")),
    ]


def _run(st, reason, ps):
    for p in ps:
        p.start()
    try:
        asyncio.run(botmod.BotState._exit(st, MINT, reason))
    finally:
        for p in ps:
            p.stop()


def test_slow_sell_stays_active_and_is_watched():
    st, s = _st()
    ps = _patches() + [patch("bot.landing.land_tx", new=AsyncMock(side_effect=landing.NotLanded(["S1", "S2", "S3"], 15.0)))]
    _run(st, "stop-loss hit -12%", ps)
    t = s["trade"]
    assert MINT in st.active_trades and t["status"] == "active"
    assert t["pending_exit_sigs"] == ["S1", "S2", "S3"] and t.get("exit_landed_failures", 0) == 0
    assert st._watch_late_sell.await_count == 1 and st._watch_late_sell.await_args.args[1] == ["S1", "S2", "S3"]
    assert not any(c[0] == "ws" and c[1] == "trade_exit_terminal" for c in st.calls)


def test_slow_sell_never_parks_however_many_times():
    st, s = _st()
    ps = _patches() + [patch("bot.landing.land_tx", new=AsyncMock(side_effect=landing.NotLanded(["S"], 15.0)))]
    for _ in range(6):
        _run(st, "stop-loss hit -12%", ps)
    assert MINT in st.active_trades and s["trade"]["status"] == "active" and s["trade"]["exit_retries"] == 6


def test_landed_reverts_escalate_to_emergency_then_park_as_liquidity():
    st, s = _st()
    st._attempt_emergency_pumpswap_sell = AsyncMock(return_value=None)
    err = landing.LandedButFailed("SIGX", {"InstructionError": [2, {"Custom": 6023}]})
    ps = _patches() + [patch("bot.landing.land_tx", new=AsyncMock(side_effect=err))]
    _run(st, "stop-loss hit -12%", ps)
    _run(st, "stop-loss hit -12%", ps)
    assert MINT in st.active_trades and s["trade"]["exit_landed_failures"] == 2
    assert st._attempt_emergency_pumpswap_sell.await_count == 0
    _run(st, "stop-loss hit -12%", ps)
    t = s["trade"]
    assert st._attempt_emergency_pumpswap_sell.await_count == 1
    assert MINT not in st.active_trades and t["status"] == "exit_failed_terminal" and t["venue_stage"] == "sell-reverted"
    assert t["pnl_sol"] < 0 and t["pnl_provisional"] is True and t["held_intent"] == "exit"   # provisional −stake P&L (EMERGENT_CHANGES #1.7)


def test_emergency_fill_after_reverts_books_closed():
    st, s = _st()
    st._attempt_emergency_pumpswap_sell = AsyncMock(return_value=("EM", 150_000_000, {}))
    s["trade"]["exit_landed_failures"] = 2
    err = landing.LandedButFailed("SIGX", {"InstructionError": [2, {"Custom": 6023}]})
    ps = _patches() + [patch("bot.landing.land_tx", new=AsyncMock(side_effect=err))]
    _run(st, "stop-loss hit -12%", ps)
    t = s["trade"]
    assert t["status"] == "closed" and t["exit_sig"] == "EM" and abs(t["exit_sol"] - 0.15) < 1e-9


def test_zero_balance_with_sale_on_chain_books_real_exit():
    st, s = _st()
    st._find_sale_on_chain = AsyncMock(return_value=("LATE", 120_000_000))
    ps = _patches(balance=0) + [patch("bot.landing.land_tx", new=AsyncMock(side_effect=AssertionError("must not send")))]
    _run(st, "stop-loss hit -12%", ps)
    t = s["trade"]
    assert t["status"] == "closed" and t["exit_sig"] == "LATE" and abs(t["exit_sol"] - 0.12) < 1e-9
    assert t["pnl_reconciled"] is False and MINT not in st.active_trades


def test_zero_balance_without_sale_closes_at_minus_entry_not_zero():
    st, s = _st()
    st._find_sale_on_chain = AsyncMock(return_value=None)
    ps = _patches(balance=0) + [patch("bot.landing.land_tx", new=AsyncMock(side_effect=AssertionError("must not send")))]
    _run(st, "stop-loss hit -12%", ps)
    t = s["trade"]
    assert t["status"] == "closed" and t["pnl_sol"] == -t["entry_sol"] and t["pnl_reconciled"] is False


def test_exit_fee_ladder_floor_cap_and_panic():
    st, _ = _st()
    st.config.exit_priority_floor_microlamports = 1_000_000
    st.config.exit_priority_cap_microlamports = 20_000_000
    st.config.panic_exit_priority_microlamports = 3_000_000
    assert botmod.BotState._exit_fee_ladder(st, 500_000, False)[0] == 1_000_000
    assert botmod.BotState._exit_fee_ladder(st, 500_000, True)[0] == 3_000_000
    assert botmod.BotState._exit_fee_ladder(st, 500_000, True)[-1] == 20_000_000
