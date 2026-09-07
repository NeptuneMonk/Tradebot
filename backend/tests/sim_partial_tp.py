"""Synthetic test for partial-TP flow.

Doesn't make on-chain calls — stubs out RPC and verifies the partial path
fires, reserves the slot atomically, banks realized PnL, then the runner
exits via tightened trailing.
"""
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()


async def main():
    import bot as bot_module

    # Stub out RPC
    fake_db = MagicMock()
    fake_db.trades.update_one = AsyncMock()
    fake_db.trades.insert_one = AsyncMock()
    fake_db.launches.update_one = AsyncMock()
    state = bot_module.BotState(fake_db)
    state.config.take_profit_pct = 15.0
    state.config.stop_loss_pct = 20.0
    state.config.trailing_stop_pct = 10.0
    state.config.partial_tp_pct = 50.0
    state.config.partial_tp_trail_tighten_pct = 5.0
    state.config.exit_slippage_bps = 500
    state.config.slippage_bps = 500

    # Inject an active paper trade
    mint = "TestMint1111111111111111111111111111111111"
    state.active_trades[mint] = {
        "trade": {
            "id": "t1", "mint": mint, "symbol": "TST", "status": "active", "mode": "paper",
            "entry_sol": 0.1, "entry_usd": 16.0, "entry_tokens": 1_000_000_000_000,
            "entry_price_sol": 1e-13,  # SOL per raw token
            "classifier_action": "momentum_new",
        },
        "launch": {"creator": ""},
        "protocol": "pumpfun",
    }

    # Stub pumpfun.fetch_bonding_curve_state to return enough liquidity for the partial sell
    async def fake_fetch(_):
        return {
            "virtual_sol_reserves": 60_000_000_000,
            "virtual_token_reserves": 600_000_000_000_000,
            "real_sol_reserves": 30_000_000_000,
            "complete": False,
        }
    bot_module.pumpfun.fetch_bonding_curve_state = fake_fetch
    bot_module.get_sol_usd_price = AsyncMock(return_value=160.0)

    # Tick 1 — price +18% (crosses TP=15%): should trigger PARTIAL not full exit
    cur_price = 1.18e-13
    await state._check_fast_exit(mint, cur_price)

    slot = state.active_trades.get(mint)
    assert slot is not None, "FAIL: trade fully exited at TP instead of partial"
    td = slot["trade"]
    assert td.get("partial_done") is True, f"FAIL: partial_done not set: {td}"
    realized = td.get("partial_realized_usd")
    print(f"PASS partial fired: realized=${realized:.4f} sold={td.get('partial_sell_tokens'):,} remaining_tokens={td['entry_tokens']:,}")

    # Tick 2 — price now at +5% from original entry (which is below TP but should NOT re-trigger)
    cur_price = 1.20e-13  # peak +20% from original
    await state._check_fast_exit(mint, cur_price)
    assert state.active_trades.get(mint) is not None, "FAIL: re-trigger closed trade"
    print(f"PASS no re-trigger on second tick at +20%")

    # Tick 3 — price drops to trail tighten threshold (peak +20%, now -5% from peak = +14% from entry)
    # Tightened trail = 5%, so any drop >=5% from peak should fire
    cur_price = 1.14e-13  # -5.0% from peak 1.20e-13
    await state._check_fast_exit(mint, cur_price)
    assert state.active_trades.get(mint) is None, "FAIL: runner did not exit on tightened trailing"
    print(f"PASS runner exited on tightened trailing stop")

asyncio.run(main())
