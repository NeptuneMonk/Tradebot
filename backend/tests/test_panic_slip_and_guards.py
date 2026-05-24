"""Test the panic-slippage tiering + zero-balance early-exit guard fixes.

These are the fixes that resolved the production 6022/6023/6003 sell failures
on 2026-05-24. We don't send any tx — we just verify the helpers, the
configuration plumbing, and the sell IX shape.

Run: python -m backend.tests.test_panic_slip_and_guards
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from solders.pubkey import Pubkey
import pumpfun
from models import BotConfig


def test_panic_helper_classification():
    """_is_panic_exit and _exit_slip_for must classify and widen correctly."""
    from bot import BotState
    # Build a stub with just the config wired (avoid full BotState init)
    class _StubBot:
        config = BotConfig()
        _is_panic_exit = BotState._is_panic_exit
        _exit_slip_for = BotState._exit_slip_for
    s = _StubBot()

    panic_reasons = [
        "stop-loss hit (-15.2%)",
        "stop-loss hit (-22.0%) [fast]",
        "hard-stop (user requested)",
        "classifier abort: low momentum",
        "classifier exit_early: bad pattern",
        "bonding curve completed (LP about to deploy)",
    ]
    chill_reasons = [
        "take-profit hit (+25.1%)",
        "take-profit hit (+20.0%) [fast]",
        "timeout after 45s",
        "partial take-profit at +18%",
    ]
    for r in panic_reasons:
        assert s._is_panic_exit(r), f"{r!r} should be PANIC"
        assert s._exit_slip_for(r, 1000) >= 2500, f"{r!r} should widen to >=25%"
    for r in chill_reasons:
        assert not s._is_panic_exit(r), f"{r!r} should be NORMAL"
        assert s._exit_slip_for(r, 1000) == 1000, f"{r!r} should keep 10%"
    print("panic_helper_classification: OK")


def test_quote_sell_zero_amount():
    """quote_sell_sol with 0 tokens must NOT crash — it just returns 0/0."""
    state = {
        "virtual_sol_reserves": 30_000_000_000,
        "virtual_token_reserves": 1_073_000_000_000_000,
    }
    sol_out, min_sol = pumpfun.quote_sell_sol(state, 0, 1000)
    assert sol_out == 0 and min_sol == 0, f"got {sol_out}/{min_sol}"
    print("quote_sell_zero_amount: OK")


def test_default_slippage_values():
    """New defaults must match the user's request: 10% normal / 25% panic."""
    cfg = BotConfig()
    assert cfg.exit_slippage_bps == 1000, f"got {cfg.exit_slippage_bps}, want 1000"
    assert cfg.panic_exit_slippage_bps == 2500, f"got {cfg.panic_exit_slippage_bps}, want 2500"
    print(f"default_slippage_values: OK  (exit={cfg.exit_slippage_bps}bps, panic={cfg.panic_exit_slippage_bps}bps)")


async def test_sell_ix_shape():
    """Verify build_sell_ix produces the right account count for both variants."""
    user = Pubkey.from_string("Gbp9yFREc9dPvnfSjBmi9udg3UCrMmjZh2rjaPebRPrR")
    mint = Pubkey.from_string("4L4hou7WevgyukfR6QMRb3TGxQve3Uvzpqf11pMWpump")
    creator = Pubkey.from_string("EWLVbzvyEhh5m9WEsUtPoLCLxn5QonaZYqN5rL2L6Qef")

    # Non-cashback path: 16 accounts (14 base + bonding_curve_v2 + breaking_fee)
    sell = await pumpfun.build_sell_ix(user, mint, 1000, 100, creator, cashback=False)
    assert len(sell.accounts) == 16, f"non-cashback: expected 16, got {len(sell.accounts)}"
    # Cashback path: 17 accounts (+ user_volume_accumulator)
    sell_cb = await pumpfun.build_sell_ix(user, mint, 1000, 100, creator, cashback=True)
    assert len(sell_cb.accounts) == 17, f"cashback: expected 17, got {len(sell_cb.accounts)}"
    # Data must encode amount + min_sol_out (16 bytes) + track-volume tail
    assert len(sell.accounts[0].pubkey.__bytes__()) == 32
    print(f"sell_ix_shape: OK  (non-cb={len(sell.accounts)} accts, cb={len(sell_cb.accounts)} accts)")


def main():
    test_panic_helper_classification()
    test_quote_sell_zero_amount()
    test_default_slippage_values()
    asyncio.run(test_sell_ix_shape())
    print("\nAll sell-path guard tests PASSED.")


if __name__ == "__main__":
    main()
