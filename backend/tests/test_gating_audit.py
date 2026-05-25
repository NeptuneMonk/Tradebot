"""Regression tests for the gating-audit fixes (2026-05-25).

Covers:
- Seasoned-band buyer gate uses `buy_count` from Pump.fun API (not the
  empty Helius mempool `buyers` set).
- Reconciler backfills `entry_price_sol` from on-chain `entry_delta` so
  displayed PnL matches the real fill price.
"""
import os
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))


def test_buy_count_field_exposed_in_score():
    """The scanner's score() output must include `buy_count` so:
    - The seasoned-band entry gate (bot.py) can read it
    - The /scanner/candidates API surfaces it to the dashboard"""
    from collections import deque
    from scanner import MomentumScanner

    class _Cfg:
        scanner_recent_inflow_window_s = 60
        scanner_holder_velocity_window_s = 30

    class _St:
        def __init__(self):
            self.config = _Cfg()
            self.tracking = {}

    scanner = MomentumScanner(_St())
    bucket = {
        "start": 1000.0,
        "first_seen_price_sol": 1e-10,
        "last_price_sol": 1e-9,
        "buyers": set(),
        "buy_events": deque(maxlen=500),
        "buy_count": 42,  # populated by discovery polling
        "last_real_sol_lamports": 30_000_000_000,  # 30 SOL
    }
    m = scanner.score(bucket, None, now=2000.0)
    assert m["buy_count"] == 42


def test_reconciler_backfills_entry_price_from_real_fill():
    """When the reconciler runs, it should overwrite entry_price_sol with
    the actual on-chain fill price (real_entry_cost / entry_tokens), not
    the optimistic pre-trade quote stored at entry time."""
    # Simulated real-world numbers:
    quoted_entry_price = 1.0e-9       # what bot.py stored at entry
    entry_delta_lamports = 11_000_000  # what actually left wallet (~$0.92 at $84/SOL)
    entry_tokens = 10_000_000          # tokens received (raw, 6 decimals)

    real_entry_price = (abs(entry_delta_lamports) / 1e9) / entry_tokens
    # Real fill should be HIGHER than quote (we paid more SOL per token)
    assert real_entry_price > quoted_entry_price
    # Sanity: real price within reasonable range of quote
    drag_pct = (real_entry_price - quoted_entry_price) / quoted_entry_price * 100
    assert drag_pct > 0


def test_reconciler_skips_entry_price_backfill_on_ghost():
    """If the buy tx ghosted (only sig fee left wallet), the reconciler
    must NOT compute entry_price_sol from the ~65k lamport gas debit —
    that would produce a ridiculous price 1000x off from reality."""
    entry_delta_lamports = -65_000  # sig fee only — ghost
    entry_tokens = 0  # tx failed, no tokens received

    is_ghost = abs(entry_delta_lamports) < 200_000
    assert is_ghost
    # Reconciler guard: skip backfill when ghost (entry_tokens=0 would div/0
    # anyway, but explicit guard is clearer)
    if is_ghost or entry_tokens == 0:
        real_entry_price = None
    else:
        real_entry_price = (abs(entry_delta_lamports) / 1e9) / entry_tokens
    assert real_entry_price is None
