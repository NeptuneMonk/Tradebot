"""50% of each winning close, batched until 0.01 SOL."""
from tailbot.quotes import constant_product_buy
from tailbot.safety import parse_mint
from tailbot.sweep import SWEEP_WALLET, apply_win
import struct


def test_half_profit_stays_and_half_batches():
    pending, taken, batched = _step(0.0, 0.01)
    assert taken == 0.005 and pending == 0.005 and batched is None
    pending, taken, batched = _step(pending, 0.02)
    assert taken == 0.01
    assert batched == 0.015
    assert pending == 0.0
    # a loss does not sweep and does not eat the pending bucket
    pending, taken, batched = _step(0.004, -0.2)
    assert taken == 0 and pending == 0.004 and batched is None
    assert SWEEP_WALLET == "EG9FyiBDGgRTTJjsiB26dtBq9j5L41S8nukZRsvpMXGp"


def _step(pending, profit):
    got = apply_win(pending, profit, sweep_pct=50, min_batch_sol=0.01)
    return got.pending_sol, got.taken_sol, got.batched_sol


def test_paper_fill_is_worse_than_spot():
    # 30 SOL virtual quote, large token reserve, 0.1 SOL in. Fee + extra slip haircut the tokens.
    got = constant_product_buy(30_000_000_000, 1_000_000_000_000_000, 100_000_000, fee_bps=100, extra_slip_bps=100)
    assert got["tokens_out"] > 0
    assert got["entry_price_sol_per_raw"] > got["spot_price_sol_per_raw"]
    assert got["fee_pct"] == 1.0
    assert got["slip_pct"] == 1.0


def test_mint_freeze_and_transfer_fee():
    clean = bytearray(82)
    clean[44] = 6
    assert parse_mint(bytes(clean)).ok
    frozen = bytearray(clean)
    struct.pack_into("<I", frozen, 46, 1)
    parsed = parse_mint(bytes(frozen))
    assert parsed.freeze and parsed.reason == "freeze authority"
    fee = bytearray(166)
    fee[44] = 6
    fee[165] = 1
    fee += struct.pack("<HH", 1, 4) + b"\x00\x00\x00\x00"
    parsed = parse_mint(bytes(fee))
    assert parsed.transfer_fee and not parsed.ok
