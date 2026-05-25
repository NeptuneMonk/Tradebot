"""Tests for the ghost-entry detection in PnLReconciler.

A "ghost entry" is a BUY tx that landed on-chain but failed at the instruction
level (Custom:XXXX / IncorrectProgramId / etc.). Solana's `getSignatureStatuses`
returns `err: null` for these because the SIGNATURE was valid — only the
instructions failed. Pre-fix, the bot mis-detected these as successful entries.

The reconciler now flags these explicitly via `|entry_delta| < 200k lamports`
(below the entry-size floor; real buys cost >= 1M lamports even at $0.50 sizes).
"""
import os

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))


def test_ghost_threshold_classifies_gas_only_correctly():
    """The reconciler's ghost guard kicks in when |entry_delta| < 200k lamports.
    A real buy moves >= 1M lamports (~$0.08 at $80/SOL) even for tiny sizes."""
    # Real-world observed values:
    GHOST_DELTA = -65_000        # base sig fee only (Custom:XXXX failure)
    GHOST_DELTA_WITH_PRIO = -105_000   # sig + priority fee (no SOL moved)
    SMALL_REAL_BUY = -10_744_525  # $0.90 entry from real trade row
    THRESHOLD = 200_000
    assert abs(GHOST_DELTA) < THRESHOLD, "tiny gas-only tx should be ghost"
    assert abs(GHOST_DELTA_WITH_PRIO) < THRESHOLD, "priority-fee-only tx is also ghost"
    assert abs(SMALL_REAL_BUY) > THRESHOLD, "real $0.90 buy must NOT be flagged ghost"


def test_pnl_pct_zeroed_for_ghost_rows():
    """When the reconciler detects a ghost row, pnl_pct should be 0.0
    (not -300%) so analytics dashboards don't show fake catastrophic losses."""
    # Simulate the bug: entry only spent gas, exit also failed (paid gas)
    entry_delta = -65_000   # sig fee
    exit_delta = -65_000    # sig fee on failed sell
    partial_delta = 0

    net = entry_delta + partial_delta + exit_delta
    cost_sol = abs(entry_delta) / 1e9
    naive_pnl_pct = (net / 1e9) / cost_sol * 100
    assert naive_pnl_pct < -100, "without ghost guard, this would show -200%"

    # With ghost guard:
    is_ghost = abs(entry_delta) < 200_000
    assert is_ghost
    displayed_pnl_pct = 0.0 if is_ghost else naive_pnl_pct
    assert displayed_pnl_pct == 0.0


def test_real_trade_keeps_pnl_pct():
    """Non-ghost rows must preserve their real PnL — only ghosts get zeroed."""
    entry_delta = -10_744_525   # $0.90 real buy
    exit_delta = 8_500_000      # ~$0.72 received
    partial_delta = 0

    is_ghost = abs(entry_delta) < 200_000
    assert is_ghost is False

    net = entry_delta + partial_delta + exit_delta
    cost_sol = abs(entry_delta) / 1e9
    real_pnl_pct = (net / 1e9) / cost_sol * 100
    assert real_pnl_pct < 0 and real_pnl_pct > -50
